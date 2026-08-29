"""FastMCP server instance for mcp-relay."""
import logging
from mcp.server.fastmcp import FastMCP

logger = logging.getLogger(__name__)

mcp = FastMCP(
    name="mcp-relay",
    instructions=(
        "Unified MCP relay gateway. "
        "Tools are discovered from configured remote MCP servers at startup. "
        "Call any registered tool by name — the relay routes and authenticates automatically."
    ),
)


async def init_remote_servers() -> None:
    logger.info("[mcp_relay] starting remote server registration...")
    from .registry.server_registry import register_all
    count = await register_all(mcp)
    logger.info("[mcp_relay] ready — %d remote tool(s) registered across all servers", count)
