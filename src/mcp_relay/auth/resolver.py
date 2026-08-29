"""Resolves authenticated (url, headers) for a configured remote server."""
from __future__ import annotations
import logging
from urllib.parse import parse_qs, quote, urlencode, urlparse, urlunparse
from . import credential_cache
from .oauth2 import fetch_token
from .secret_resolver import resolve_secret_refs

logger = logging.getLogger(__name__)


def _mask_url(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.query:
        return url
    masked = {k: ["***"] for k in parse_qs(parsed.query)}
    return urlunparse(parsed._replace(
        query=urlencode(masked, doseq=True, quote_via=lambda s, *_: s)
    ))


def _mask_url_path_segment(url: str, secret_value: str) -> str:
    if not secret_value:
        return url
    return url.replace(secret_value, "***")


async def resolve_connection(server_cfg: dict) -> tuple[str, dict[str, str]]:
    name: str = server_cfg.get("name", "<unnamed>")

    cached = credential_cache.get_cached(name)
    if cached:
        return cached

    url: str = server_cfg["url"]
    auth: dict = await resolve_secret_refs(server_cfg.get("auth", {}))
    auth_type: str = auth.get("type", "none")
    headers: dict[str, str] = {}

    logger.info("[auth] resolving connection for %s (auth_type=%s)", name, auth_type)

    if auth_type == "none":
        pass
    elif auth_type == "api_key_query":
        param = auth.get("param_name", "apikey")
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}{param}={quote(auth['value'], safe='')}"
        logger.debug("[auth] %s: api_key_query → %s", name, _mask_url(url))
    elif auth_type == "api_key_url_path":
        placeholder = auth.get("placeholder", "{api_key}")
        secret_value = auth["value"]
        if placeholder not in url:
            raise ValueError(f"[auth] {name!r}: placeholder {placeholder!r} not found in url")
        url = url.replace(placeholder, quote(secret_value, safe=""))
        logger.debug("[auth] %s: api_key_url_path → %s", name, _mask_url_path_segment(url, secret_value))
    elif auth_type == "api_key_header":
        headers[auth.get("header_name", "X-Api-Key")] = auth["value"]
        logger.debug("[auth] %s: api_key_header set", name)
    elif auth_type == "bearer":
        headers["Authorization"] = f"Bearer {auth['value']}"
        logger.debug("[auth] %s: bearer token injected", name)
    elif auth_type == "oauth2_client_credentials":
        headers["Authorization"] = f"Bearer {await fetch_token(name, auth)}"
        logger.debug("[auth] %s: oauth2 bearer token injected", name)
    else:
        raise ValueError(f"[auth] unknown auth type for {name!r}: {auth_type!r}")

    logger.info("[auth] %s: connection ready (auth_type=%s)", name, auth_type)
    credential_cache.set_cached(name, url, headers)
    return url, dict(headers)
