"""
Auth resolver for remote MCP servers.

All credential values in remote_servers.yaml should use the
``secret::<name>`` pattern. resolve_secret_refs() resolves them from
the configured secret backend (env, GCP Secret Manager, AWS Secrets Manager,
Azure Key Vault, or HashiCorp Vault) — set SECRET_BACKEND env var to choose.

Supported auth types:
  none                     — no auth (public servers)
  api_key_query            — appends ?<param_name>=<value> to the URL
  api_key_url_path         — substitutes {api_key} (or custom placeholder) in the URL path
  api_key_header           — sends a custom header (default: X-Api-Key)
  bearer                   — sends Authorization: Bearer <value>
  oauth2_client_credentials — fetches and caches a token from token_url

IMPORTANT: never pass resolved secret values to logger.*() calls.
_mask_url() must be used before logging any URL that may contain query-param
secrets. httpx exceptions must never propagate unwrapped — they embed
request URLs and expose secrets in log tracebacks.
"""
from __future__ import annotations

import logging
import time
from urllib.parse import parse_qs, quote, urlencode, urlparse, urlunparse

import httpx

from src.secret_resolver import resolve_secret_refs

logger = logging.getLogger(__name__)

# ── Caches ────────────────────────────────────────────────────────────────────

# server_name → ((authenticated_url, headers), expires_at_unix)
# Avoids hitting the secret backend on every tool call.
_credential_cache: dict[str, tuple[tuple[str, dict[str, str]], float]] = {}
_CREDENTIAL_TTL = 300  # 5 minutes — acceptable window after a secret rotation

# (token_url, client_id) → (access_token, expires_at_unix)
# Keyed by both fields so two servers on the same IdP don't share a slot.
_oauth2_cache: dict[tuple[str, str], tuple[str, float]] = {}


# ── URL masking ───────────────────────────────────────────────────────────────

def _mask_url(url: str) -> str:
    """Return the URL with all query-parameter values replaced by *** for safe logging."""
    parsed = urlparse(url)
    if not parsed.query:
        return url
    masked = {k: ["***"] for k in parse_qs(parsed.query)}
    return urlunparse(parsed._replace(
        query=urlencode(masked, doseq=True, quote_via=lambda s, *_: s)
    ))


def _mask_url_path_segment(url: str, secret_value: str) -> str:
    """Replace a literal secret value embedded in the URL path with *** for safe logging."""
    if not secret_value:
        return url
    return url.replace(secret_value, "***")


# ── Connection resolver ───────────────────────────────────────────────────────

async def resolve_connection(server_cfg: dict) -> tuple[str, dict[str, str]]:
    """Return (authenticated_url, extra_headers) ready to pass to the MCP transport.

    Results are cached for _CREDENTIAL_TTL seconds to avoid hitting the secret
    backend on every tool call.
    """
    name: str = server_cfg.get("name", "<unnamed>")
    now = time.time()

    if name in _credential_cache:
        (cached_url, cached_headers), expires_at = _credential_cache[name]
        if now < expires_at:
            logger.debug("[mcp_relay.auth] %s: credentials served from cache", name)
            return cached_url, dict(cached_headers)

    url: str = server_cfg["url"]
    auth: dict = await resolve_secret_refs(server_cfg.get("auth", {}))
    auth_type: str = auth.get("type", "none")
    headers: dict[str, str] = {}

    logger.info("[mcp_relay.auth] resolving connection for %s (auth_type=%s)", name, auth_type)

    if auth_type == "none":
        logger.debug("[mcp_relay.auth] %s: no auth required", name)

    elif auth_type == "api_key_query":
        param = auth.get("param_name", "apikey")
        sep = "&" if "?" in url else "?"
        # URL-encode the value so keys with +, &, = don't corrupt the URL
        url = f"{url}{sep}{param}={quote(auth['value'], safe='')}"
        logger.debug("[mcp_relay.auth] %s: api_key_query → %s", name, _mask_url(url))

    elif auth_type == "api_key_url_path":
        # Key is embedded in the URL path, e.g. https://mcp.example.com/{api_key}/mcp
        placeholder = auth.get("placeholder", "{api_key}")
        secret_value = auth["value"]
        if placeholder not in url:
            raise ValueError(
                f"[mcp_relay.auth] {name!r}: api_key_url_path — "
                f"placeholder {placeholder!r} not found in url"
            )
        url = url.replace(placeholder, quote(secret_value, safe=""))
        logger.debug("[mcp_relay.auth] %s: api_key_url_path → %s", name,
                     _mask_url_path_segment(url, secret_value))

    elif auth_type == "api_key_header":
        header = auth.get("header_name", "X-Api-Key")
        headers[header] = auth["value"]
        logger.debug("[mcp_relay.auth] %s: api_key_header → header=%s", name, header)

    elif auth_type == "bearer":
        headers["Authorization"] = f"Bearer {auth['value']}"
        logger.debug("[mcp_relay.auth] %s: bearer token injected", name)

    elif auth_type == "oauth2_client_credentials":
        token = await _fetch_oauth2_token(name, auth)
        headers["Authorization"] = f"Bearer {token}"
        logger.debug("[mcp_relay.auth] %s: oauth2 bearer token injected", name)

    else:
        raise ValueError(f"[mcp_relay.auth] unknown auth type for {name!r}: {auth_type!r}")

    logger.info("[mcp_relay.auth] %s: connection ready (auth_type=%s)", name, auth_type)
    _credential_cache[name] = ((url, headers), now + _CREDENTIAL_TTL)
    return url, dict(headers)


def invalidate_credential_cache(server_name: str | None = None) -> None:
    """Evict cached credentials — call after a secret rotation."""
    if server_name:
        _credential_cache.pop(server_name, None)
        logger.info("[mcp_relay.auth] credential cache cleared for %s", server_name)
    else:
        _credential_cache.clear()
        logger.info("[mcp_relay.auth] entire credential cache cleared")


# ── OAuth2 ────────────────────────────────────────────────────────────────────

async def _fetch_oauth2_token(server_name: str, auth: dict) -> str:
    """Fetch an OAuth2 client_credentials token, caching by (token_url, client_id).

    client_id is a public OAuth2 identifier — safe to use as a cache key.
    Keying by both token_url and client_id prevents cross-server cache collision
    when two servers share an IdP but have different app registrations.
    """
    token_url: str = auth["token_url"]
    client_id: str = auth["client_id"]
    cache_key = (token_url, client_id)

    if cache_key in _oauth2_cache:
        token, expires_at = _oauth2_cache[cache_key]
        if time.time() < expires_at - 60:  # 60 s buffer before expiry
            logger.debug("[mcp_relay.auth] %s: oauth2 token served from cache", server_name)
            return token

    scope = auth.get("scope", "")
    logger.info("[mcp_relay.auth] %s: fetching OAuth2 token from %s (scope=%r)",
                server_name, token_url, scope)

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(token_url, data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": auth["client_secret"],
                **({"scope": scope} if scope else {}),
            })
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPStatusError as exc:
        # Log status code only — exc.request.url may contain credentials
        logger.error("[mcp_relay.auth] %s: OAuth2 token request failed — HTTP %s from %s",
                     server_name, exc.response.status_code, token_url)
        raise RuntimeError(
            f"[{server_name}] OAuth2 token fetch failed: HTTP {exc.response.status_code}"
        ) from None
    except Exception:
        logger.exception("[mcp_relay.auth] %s: OAuth2 token request error (url=%s)",
                         server_name, token_url)
        raise

    token: str = data["access_token"]
    expires_in: int = data.get("expires_in", 3600)
    _oauth2_cache[cache_key] = (token, time.time() + expires_in)
    logger.info("[mcp_relay.auth] %s: OAuth2 token cached (expires_in=%ds)", server_name, expires_in)
    return token
