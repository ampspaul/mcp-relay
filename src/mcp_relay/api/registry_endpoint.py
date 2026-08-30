"""GET /registry — lists all configured MCP servers, their available tools, and blocked tools."""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse

from ..registry.server_registry import _blocked, _registered, _server_configs, _tool_metadata
from ..resilience.circuit_breaker import _breakers
from ..transport.session_pool import _pool

_SAFE_FIELDS = {"name", "url", "enabled", "tool_prefix", "description"}


def _safe_server_info(cfg: dict) -> dict:
    info: dict = {k: v for k, v in cfg.items() if k in _SAFE_FIELDS}
    info.setdefault("enabled", True)
    info.setdefault("tool_prefix", "")
    return info


def _available_tool(proxy_name: str) -> dict:
    meta = _tool_metadata.get(proxy_name, {})
    return {
        "name": proxy_name,
        "description": meta.get("description", ""),
        "parameters": meta.get("parameters", []),
    }


def _blocked_tool(proxy_name: str) -> dict:
    meta = _tool_metadata.get(proxy_name, {})
    return {
        "name": proxy_name,
        "description": meta.get("description", ""),
    }


def registry_handler(_: Request) -> JSONResponse:
    servers = []
    total_available = 0
    total_blocked = 0
    enabled_count = 0

    for cfg in _server_configs:
        name = cfg["name"]
        if cfg.get("enabled", True):
            enabled_count += 1

        available = [_available_tool(n) for n in sorted(_registered.get(name, set()))]
        blocked = [_blocked_tool(n) for n in sorted(_blocked.get(name, set()))]
        total_available += len(available)
        total_blocked += len(blocked)

        entry = _safe_server_info(cfg)
        entry["tools"] = {
            "available": available,
            "blocked": blocked,
        }
        entry["counts"] = {
            "available": len(available),
            "blocked": len(blocked),
        }
        cb = _breakers.get(name)
        entry["circuit_breaker"] = {
            "state": cb.state.value if cb else "closed",
            "failure_count": cb.failure_count if cb else 0,
        }
        srv_session = _pool.get_server_session(name)
        entry["session"] = {
            "status": srv_session.status if srv_session else "not_started",
        }
        servers.append(entry)

    return JSONResponse(
        {
            "servers": servers,
            "summary": {
                "total_servers": len(_server_configs),
                "enabled_servers": enabled_count,
                "available_tools": total_available,
                "blocked_tools": total_blocked,
            },
        }
    )
