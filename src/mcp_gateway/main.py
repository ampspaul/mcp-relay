"""
mcp-relay entrypoint.

The container runtime sets PORT; we bind on 0.0.0.0 so the container is
reachable from outside. The SSE endpoint is at /sse (FastMCP default).

Run locally:
    python -m src.mcp_gateway.main

Run in a container:
    command: ["python", "-m", "src.mcp_gateway.main"]
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from src.mcp_gateway.server import mcp, init_remote_servers

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)

# httpx logs full request URLs at INFO level — suppress to WARNING to prevent
# API keys embedded in query params from appearing in container logs.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


def _health(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "service": "mcp-relay"})


@asynccontextmanager
async def _lifespan(_app: Starlette):
    """Discover and register remote MCP server tools before accepting traffic."""
    await init_remote_servers()
    yield


def build_app() -> Starlette:
    """Compose the MCP SSE app with a /health probe endpoint."""
    sse = mcp.sse_app()
    return Starlette(
        routes=[
            Route("/health", _health),
            Mount("/", app=sse),
        ],
        lifespan=_lifespan,
    )


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    logger.info("[mcp_relay] starting SSE server on 0.0.0.0:%d", port)
    uvicorn.run(build_app(), host="0.0.0.0", port=port)
