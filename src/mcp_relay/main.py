"""mcp-relay entrypoint.

Run locally:
    python -m mcp_relay.main

Run in a container:
    command: ["python", "-m", "mcp_relay.main"]
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from starlette.applications import Starlette
from starlette.routing import Mount, Route

from .api.health import health
from .api.metrics_endpoint import metrics_handler
from .api.registry_endpoint import registry_handler
from .auth.secret_resolver import resolve_secret_refs
from .config.loader import load_security_policies
from .middleware.authentication import BearerAuthMiddleware
from .observability.logging import configure as configure_logging
from .registry.server_registry import _server_configs
from .server import init_remote_servers, mcp
from .transport.session_pool import _pool

logging.basicConfig(level=logging.INFO)
configure_logging()
logger = logging.getLogger(__name__)

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

_POLICIES_PATH = Path(__file__).parent.parent.parent / "config" / "security_policies.yaml"


async def _load_inbound_tokens() -> set[str]:
    policies = await load_security_policies(_POLICIES_PATH)
    inbound = policies.get("inbound_auth") or {}
    auth_type = inbound.get("type", "none")

    if auth_type == "none":
        return set()

    if auth_type != "bearer":
        raise ValueError(
            f"[auth] unknown inbound_auth.type {auth_type!r} — must be 'none' or 'bearer'"
        )

    raw_tokens: list[str] = inbound.get("tokens") or []
    if not raw_tokens:
        raise ValueError(
            "[auth] inbound_auth.type=bearer but no tokens configured in security_policies.yaml"
        )

    resolved = await resolve_secret_refs(raw_tokens)
    tokens = {t for t in resolved if t}
    logger.info("[auth] inbound bearer auth enabled — %d token(s) loaded", len(tokens))
    return tokens


@asynccontextmanager
async def _lifespan(_app: Starlette):
    await init_remote_servers()
    await _pool.start(_server_configs)
    logger.info("[mcp_relay] session pool started (%d server(s))", len(_server_configs))

    policies = await load_security_policies(_POLICIES_PATH)
    interval = int(policies.get("tool_refresh_interval_seconds", 0))
    refresh_task: asyncio.Task | None = None
    if interval > 0:
        from .registry.tool_refresher import refresh_loop

        refresh_task = asyncio.create_task(refresh_loop(mcp, interval))
        logger.info("[mcp_relay] tool refresh loop started (interval=%ds)", interval)

    try:
        yield
    finally:
        if refresh_task:
            refresh_task.cancel()
            try:
                await refresh_task
            except asyncio.CancelledError:
                pass
        await _pool.stop()
        logger.info("[mcp_relay] session pool stopped")


def build_app() -> Starlette:
    return Starlette(
        routes=[
            Route("/health", health),
            Route("/metrics", metrics_handler),
            Route("/registry", registry_handler),
            Mount("/", app=mcp.sse_app()),
        ],
        lifespan=_lifespan,
    )


async def build_app_async() -> Starlette:
    tokens = await _load_inbound_tokens()
    app = build_app()
    if tokens:
        app.add_middleware(BearerAuthMiddleware, valid_tokens=tokens)
    return app


if __name__ == "__main__":
    import asyncio

    import uvicorn

    port = int(os.environ.get("PORT", 8080))
    logger.info("[mcp_relay] starting SSE server on 0.0.0.0:%d", port)

    async def _serve():
        app = await build_app_async()
        config = uvicorn.Config(app, host="0.0.0.0", port=port)
        server = uvicorn.Server(config)
        await server.serve()

    asyncio.run(_serve())
