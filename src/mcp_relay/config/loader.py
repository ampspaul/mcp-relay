"""YAML config loading and validation for remote server definitions."""

from __future__ import annotations

import logging
from pathlib import Path

import anyio
import yaml

from ..transform.response_shaper import validate_response_shape

logger = logging.getLogger(__name__)

_VALID_TRANSPORTS = {"sse", "streamable_http"}
_VALID_AUTH_TYPES = {
    "none",
    "api_key_query",
    "api_key_url_path",
    "api_key_header",
    "bearer",
    "oauth2_client_credentials",
}
_OAUTH2_REQUIRED = {"token_url", "client_id", "client_secret"}


def validate_servers(servers: list[dict]) -> None:
    """Raise ValueError with a clear message on the first config error found."""
    for i, srv in enumerate(servers):
        label = f"servers[{i}]"

        if not srv.get("name"):
            raise ValueError(f"{label}: missing required field 'name'")
        label = f"server {srv['name']!r}"

        if not srv.get("url"):
            raise ValueError(f"{label}: missing required field 'url'")

        transport = srv.get("transport", "sse")
        if transport not in _VALID_TRANSPORTS:
            raise ValueError(
                f"{label}: invalid transport {transport!r} — "
                f"must be one of {sorted(_VALID_TRANSPORTS)}"
            )

        auth = srv.get("auth") or {}
        auth_type = auth.get("type", "none")
        if auth_type not in _VALID_AUTH_TYPES:
            raise ValueError(
                f"{label}: invalid auth.type {auth_type!r} — "
                f"must be one of {sorted(_VALID_AUTH_TYPES)}"
            )

        if auth_type in {"api_key_query", "api_key_url_path", "api_key_header", "bearer"}:
            if auth.get("value") is None:
                raise ValueError(
                    f"{label}: auth.type={auth_type!r} requires 'auth.value'"
                )

        if auth_type == "oauth2_client_credentials":
            missing = _OAUTH2_REQUIRED - set(auth)
            if missing:
                raise ValueError(
                    f"{label}: auth.type='oauth2_client_credentials' requires "
                    f"{sorted(missing)}"
                )

        rl = srv.get("rate_limit") or {}
        rpd = rl.get("requests_per_day")
        if rpd is not None and (not isinstance(rpd, int) or rpd < 0):
            raise ValueError(
                f"{label}: rate_limit.requests_per_day must be a non-negative integer"
            )

        rs = srv.get("response_shape")
        if rs is not None:
            if not isinstance(rs, dict):
                raise ValueError(f"{label}: response_shape must be a mapping")
            validate_response_shape(rs, label)


async def load_servers(config_path: Path) -> list[dict]:
    if not config_path.exists():
        logger.warning("[config] remote_servers.yaml not found at %s", config_path)
        return []

    def _read() -> list[dict]:
        with config_path.open() as fh:
            data = yaml.safe_load(fh) or {}
        return data.get("servers") or []

    servers: list[dict] = await anyio.to_thread.run_sync(_read)
    logger.info("[config] loaded %d server(s)", len(servers))
    return servers


async def load_security_policies(config_path: Path) -> dict:
    if not config_path.exists():
        return {}

    def _read() -> dict:
        with config_path.open() as fh:
            return yaml.safe_load(fh) or {}

    return await anyio.to_thread.run_sync(_read)
