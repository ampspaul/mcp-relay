from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse

from ..registry.server_registry import _server_configs
from ..resilience.circuit_breaker import _breakers
from ..transport.session_pool import _pool


def _upstream_status(name: str, enabled: bool) -> dict:
    if not enabled:
        return {"name": name, "status": "disabled", "session": "n/a", "circuit_breaker": "n/a"}

    srv = _pool.get_server_session(name)
    session_status = srv.status if srv else "not_started"

    cb = _breakers.get(name)
    cb_state = cb.state.value if cb else "closed"

    if session_status == "connected" and cb_state == "closed":
        status = "ok"
    elif session_status == "connecting":
        status = "connecting"
    else:
        status = "degraded"

    return {
        "name": name,
        "status": status,
        "session": session_status,
        "circuit_breaker": cb_state,
    }


def health(_: Request) -> JSONResponse:
    upstreams = [_upstream_status(cfg["name"], cfg.get("enabled", True)) for cfg in _server_configs]

    enabled_statuses = {u["status"] for u in upstreams if u["status"] != "disabled"}
    overall = "ok" if enabled_statuses <= {"ok"} else "degraded"

    return JSONResponse(
        {
            "status": overall,
            "service": "mcp-relay",
            "upstreams": upstreams,
        }
    )
