"""Load server config, call remote tools, and register proxy callables on the FastMCP instance."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

import anyio
import yaml

from ..config.loader import load_security_policies, validate_servers
from ..observability import metrics
from ..resilience import rate_limiter
from ..resilience.circuit_breaker import CircuitOpenError, get_circuit_breaker
from ..security import api_key_redactor, pii_redactor, prompt_injection
from ..transform import response_shaper
from ..transport.session import safe_exc_msg
from ..transport.session_pool import _pool
from .proxy_builder import build
from .tool_discovery import discover

logger = logging.getLogger(__name__)

# src/mcp_relay/registry/ → src/mcp_relay/ → src/ → project root → config/
_CONFIG_DIR = Path(__file__).parent.parent.parent.parent / "config"
_CONFIG_PATH = _CONFIG_DIR / "remote_servers.yaml"
_POLICIES_PATH = _CONFIG_DIR / "security_policies.yaml"

# Global defaults loaded from security_policies.yaml at startup
_default_timeout: float = 30.0
_default_cb_config: dict = {}

# Tracks proxy tool names registered per server — used by the refresh loop and registry endpoint
_registered: dict[str, set[str]] = {}
# Tracks proxy tool names that exist upstream but are blocked by security_policies.yaml
_blocked: dict[str, set[str]] = {}
# Snapshot of server configs loaded at startup (secret:: refs are NOT resolved — safe to read)
_server_configs: list[dict] = []
# Rich tool metadata keyed by proxy_name — description + parsed parameter list
_tool_metadata: dict[str, dict] = {}


def _parse_parameters(input_schema: dict) -> list[dict]:
    props: dict = input_schema.get("properties") or {}
    required: set[str] = set(input_schema.get("required") or [])
    return [
        {
            "name": pname,
            "type": pschema.get("type", "string"),
            "required": pname in required,
            "description": pschema.get("description", ""),
        }
        for pname, pschema in props.items()
    ]


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
    logger.info(
        "[registry] %s: calling tool=%s args_keys=%s", name, tool_name, list(arguments.keys())
    )
    metrics.increment("tool_calls_total", server=name, tool=tool_name)
    t0 = time.perf_counter()

    try:
        if server_cfg.get("sanitize_input"):
            arguments = pii_redactor.sanitize_args(arguments)

        timeout = float(server_cfg.get("tool_call_timeout_seconds", _default_timeout))
        cb_cfg = {**_default_cb_config, **(server_cfg.get("circuit_breaker") or {})}
        circuit = get_circuit_breaker(name, cb_cfg)

        async def _upstream():
            # Rate limit check is intentionally inside _upstream so the quota
            # is only consumed when the circuit breaker actually allows the
            # call through.  If the circuit is OPEN, this coroutine never runs.
            await rate_limiter.check(server_cfg)
            return await _pool.call_tool(server_cfg, tool_name, arguments)

        try:
            result = await circuit.call(_upstream(), timeout=timeout)
        except CircuitOpenError as exc:
            metrics.increment("circuit_open_total", server=name)
            logger.warning(
                "[registry] %s: circuit breaker open, rejecting tool=%s", name, tool_name
            )
            raise RuntimeError(str(exc)) from None
        except TimeoutError:
            metrics.increment("tool_timeout_total", server=name)
            logger.error("[registry] %s: tool=%s timed out after %.1fs", name, tool_name, timeout)
            raise RuntimeError(f"[{name}] tool call timed out after {timeout}s") from None
        except RuntimeError:
            raise
        except Exception as exc:
            msg = safe_exc_msg(exc)
            logger.error("[registry] %s: tool call failed (tool=%s): %s", name, tool_name, msg)
            metrics.increment("tool_errors_total", server=name, type="transport")
            raise RuntimeError(
                f"[{name}] remote tool {tool_name!r} transport error: {msg}"
            ) from None

        if getattr(result, "isError", False):
            error_text = "unknown error"
            if result.content and hasattr(result.content[0], "text"):
                error_text = result.content[0].text
            safe_text = api_key_redactor.redact(error_text)
            logger.error(
                "[registry] %s: tool=%s returned isError=True: %s", name, tool_name, safe_text[:200]
            )
            metrics.increment("tool_errors_total", server=name, type="tool_error")
            raise RuntimeError(f"[{name}] remote tool {tool_name!r} failed: {safe_text}")

        if not result.content:
            return {}

        first = result.content[0]
        if hasattr(first, "text"):
            raw = first.text
            if server_cfg.get("sanitize_output"):
                raw = api_key_redactor.redact(raw)
            if server_cfg.get("redact_pii"):
                raw = pii_redactor.redact(raw)
            if server_cfg.get("pii_scan_enabled", False) and (
                pii_model := server_cfg.get("pii_scan_model")
            ):
                raw = await pii_redactor.llm_redact(raw, pii_model)
            if server_cfg.get("injection_detection", False):
                prompt_injection.check(raw, name)
            shape_cfg = server_cfg.get("response_shape")
            try:
                parsed = json.loads(raw)
                rate_limiter.check_response(server_cfg, parsed)
                if shape_cfg is not None:
                    parsed = response_shaper.shape(parsed, shape_cfg)
                return parsed
            except (json.JSONDecodeError, TypeError):
                # Non-JSON plain text: still honour max_chars if configured.
                if shape_cfg and shape_cfg.get("max_chars"):
                    raw = response_shaper.truncate_text(raw, shape_cfg["max_chars"])
                return raw

        if hasattr(first, "data"):
            return first.data
        return {}

    finally:
        metrics.observe("tool_call_duration_seconds", time.perf_counter() - t0, server=name)


async def register_all(mcp: Any) -> int:
    global _default_timeout, _default_cb_config

    configs = await _load_servers()
    validate_servers(configs)
    _server_configs.clear()
    _server_configs.extend(configs)

    policies = await load_security_policies(_POLICIES_PATH)
    _default_timeout = float(policies.get("tool_call_timeout_seconds", 30.0))
    _default_cb_config = dict(policies.get("circuit_breaker") or {})
    blocklist: set[str] = set(policies.get("tool_blocklist") or [])
    if blocklist:
        logger.info("[registry] tool blocklist active: %d tool(s) blocked", len(blocklist))

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
        server_blocklist: set[str] = set(cfg.get("tool_blocklist") or [])
        effective_blocklist = blocklist | server_blocklist
        tools = await discover(cfg)

        if not tools:
            logger.warning("[registry] %s: no tools registered (discovery returned 0)", name)
            continue

        registered_this_server: set[str] = set()
        blocked_this_server: set[str] = set()
        registered_this = 0
        for tool in tools:
            proxy_name = f"{prefix}{tool.name}" if prefix else tool.name
            if tool.name in effective_blocklist:
                source = (
                    "remote_servers.yaml"
                    if tool.name in server_blocklist
                    else "security_policies.yaml"
                )
                logger.info("[registry] %s: tool %r blocked by %s", name, tool.name, source)
                blocked_this_server.add(proxy_name)
                _tool_metadata[proxy_name] = {
                    "description": tool.description or "",
                }
                continue

            if proxy_name in registered_names:
                logger.error(
                    "[registry] %s: tool name collision — %r already registered. "
                    "Set tool_prefix to avoid ambiguity. Skipping.",
                    name,
                    proxy_name,
                )
                continue

            schema: dict = tool.inputSchema or {}
            proxy_fn = build(cfg, tool.name, schema, call_tool)
            proxy_fn.__name__ = proxy_name
            proxy_fn.__qualname__ = proxy_name
            mcp.add_tool(proxy_fn, name=proxy_name, description=tool.description or "")
            registered_names.add(proxy_name)
            registered_this_server.add(proxy_name)
            _tool_metadata[proxy_name] = {
                "description": tool.description or "",
                "parameters": _parse_parameters(schema),
            }
            logger.debug("[registry] registered proxy: %s → %s@%s", proxy_name, tool.name, name)
            total += 1
            registered_this += 1

        _registered[name] = registered_this_server
        _blocked[name] = blocked_this_server
        metrics.gauge("tools_registered", float(registered_this), server=name)
        logger.info(
            "[registry] %s: %d proxy tool(s) registered (prefix=%r)", name, registered_this, prefix
        )

    if enabled_count > 0 and total == 0:
        raise RuntimeError(
            "[registry] startup failed: all enabled remote servers returned 0 tools. "
            "Check secret backend configuration and server connectivity."
        )

    return total
