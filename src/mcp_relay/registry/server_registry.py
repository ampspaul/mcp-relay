"""Load server config, call remote tools, and register proxy callables on the FastMCP instance."""
from __future__ import annotations
import json
import logging
from pathlib import Path
from typing import Any

import anyio
import yaml

from ..transport.session import open_session, safe_exc_msg
from ..security import secret_redactor, pii_redactor, prompt_injection
from ..resilience import rate_limiter
from .tool_discovery import discover
from .proxy_builder import build

logger = logging.getLogger(__name__)

# src/mcp_relay/registry/ → src/mcp_relay/ → src/ → project root → config/
_CONFIG_PATH = Path(__file__).parent.parent.parent.parent / "config" / "remote_servers.yaml"


async def _load_servers() -> list[dict]:
    if not _CONFIG_PATH.exists():
        logger.warning("[registry] remote_servers.yaml not found at %s", _CONFIG_PATH)
        return []

    def _read() -> list[dict]:
        with _CONFIG_PATH.open() as fh:
            data = yaml.safe_load(fh) or {}
        return data.get("servers") or []

    servers: list[dict] = await anyio.to_thread.run_sync(_read)
    logger.info("[registry] loaded %d server(s) from remote_servers.yaml", len(servers))
    return servers


async def call_tool(server_cfg: dict, tool_name: str, arguments: dict) -> Any:
    name = server_cfg["name"]
    logger.info("[registry] %s: calling tool=%s args_keys=%s", name, tool_name, list(arguments.keys()))
    rate_limiter.check(server_cfg)
    if server_cfg.get("sanitize_input"):
        arguments = pii_redactor.sanitize_args(arguments)

    try:
        async with open_session(server_cfg) as session:
            result = await session.call_tool(tool_name, arguments)
    except RuntimeError:
        raise
    except Exception as exc:
        msg = safe_exc_msg(exc)
        logger.error("[registry] %s: tool call failed (tool=%s): %s", name, tool_name, msg)
        raise RuntimeError(f"[{name}] remote tool {tool_name!r} transport error: {msg}") from None

    if getattr(result, "isError", False):
        error_text = "unknown error"
        if result.content and hasattr(result.content[0], "text"):
            error_text = result.content[0].text
        safe_text = secret_redactor.redact(error_text)
        logger.error("[registry] %s: tool=%s returned isError=True: %s", name, tool_name, safe_text[:200])
        raise RuntimeError(f"[{name}] remote tool {tool_name!r} failed: {safe_text}")

    if not result.content:
        return {}

    first = result.content[0]
    if hasattr(first, "text"):
        raw = first.text
        if server_cfg.get("sanitize_output"):
            raw = secret_redactor.redact(raw)
        if server_cfg.get("redact_pii"):
            raw = pii_redactor.redact(raw)
        if server_cfg.get("pii_scan_enabled", False) and (pii_model := server_cfg.get("pii_scan_model")):
            raw = await pii_redactor.llm_redact(raw, pii_model)
        if server_cfg.get("injection_detection", False):
            prompt_injection.check(raw, name)
        try:
            parsed = json.loads(raw)
            rate_limiter.check_response(server_cfg, parsed)
            return parsed
        except (json.JSONDecodeError, TypeError):
            return raw

    if hasattr(first, "data"):
        return first.data
    return {}


async def register_all(mcp: Any) -> int:
    configs = await _load_servers()
    total = 0
    enabled_count = 0
    registered_names: set[str] = set()

    for cfg in configs:
        name = cfg["name"]
        if not cfg.get("enabled", True):
            logger.info("[registry] %s: disabled — skipping", name)
            continue

        enabled_count += 1
        prefix = cfg.get("tool_prefix", "")
        tools = await discover(cfg)

        if not tools:
            logger.warning("[registry] %s: no tools registered (discovery returned 0)", name)
            continue

        registered_this = 0
        for tool in tools:
            proxy_name = f"{prefix}{tool.name}" if prefix else tool.name
            if proxy_name in registered_names:
                logger.error(
                    "[registry] %s: tool name collision — %r already registered. "
                    "Set tool_prefix to avoid ambiguity. Skipping.",
                    name, proxy_name,
                )
                continue

            schema: dict = tool.inputSchema or {}
            proxy_fn = build(cfg, tool.name, schema, call_tool)
            proxy_fn.__name__ = proxy_name
            proxy_fn.__qualname__ = proxy_name
            mcp.add_tool(proxy_fn, name=proxy_name, description=tool.description or "")
            registered_names.add(proxy_name)
            logger.debug("[registry] registered proxy: %s → %s@%s", proxy_name, tool.name, name)
            total += 1
            registered_this += 1

        logger.info("[registry] %s: %d proxy tool(s) registered (prefix=%r)", name, registered_this, prefix)

    if enabled_count > 0 and total == 0:
        raise RuntimeError(
            "[registry] startup failed: all enabled remote servers returned 0 tools. "
            "Check secret backend configuration and server connectivity."
        )

    return total
