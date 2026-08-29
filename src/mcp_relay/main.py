"""mcp-relay entrypoint.

Run locally:
    python -m mcp_relay.main

Run in a container:
    command: ["python", "-m", "mcp_relay.main"]
"""
from __future__ import annotations
import logging
import os
from contextlib import asynccontextmanager
from starlette.applications import Starlette
from starlette.routing import Mount, Route
from .server import mcp, init_remote_servers
from .api.health import health

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


@asynccontextmanager
async def _lifespan(_app: Starlette):
    await init_remote_servers()
    yield


def build_app() -> Starlette:
    return Starlette(
        routes=[
            Route("/health", health),
            Mount("/", app=mcp.sse_app()),
        ],
        lifespan=_lifespan,
    )


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    logger.info("[mcp_relay] starting SSE server on 0.0.0.0:%d", port)
    uvicorn.run(build_app(), host="0.0.0.0", port=port)
