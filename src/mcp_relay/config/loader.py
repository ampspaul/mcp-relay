"""YAML config loading for remote server definitions."""
from __future__ import annotations
import logging
from pathlib import Path
import anyio
import yaml

logger = logging.getLogger(__name__)


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
