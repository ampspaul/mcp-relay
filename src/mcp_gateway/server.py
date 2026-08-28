"""
mcp-relay — FastMCP server instance.

Remote MCP servers are registered via remote_servers.yaml at startup — no code
changes needed when adding a new upstream server. Local tools (if any) can
still be added with the @mcp.tool() decorator below.
"""
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

# ── Local tools (add @mcp.tool() decorated functions here if needed) ──────────
# (none — all tools come from remote_servers.yaml via the proxy connector)


# ── Startup hook — called by main.py lifespan ─────────────────────────────────

async def init_remote_servers() -> None:
    """Connect to all enabled remote MCP servers and register their tools."""
    logger.info("[mcp_relay] starting remote server registration...")
    from src.mcp_gateway.remote_mcp import register_remote_servers
    count = await register_remote_servers(mcp)
    logger.info("[mcp_relay] ready — %d remote tool(s) registered across all servers", count)
