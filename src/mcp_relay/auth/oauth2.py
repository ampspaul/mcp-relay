"""OAuth2 client credentials flow with token caching."""
from __future__ import annotations
import logging
import time
import httpx

logger = logging.getLogger(__name__)

_oauth2_cache: dict[tuple[str, str], tuple[str, float]] = {}


async def fetch_token(server_name: str, auth: dict) -> str:
    token_url: str = auth["token_url"]
    client_id: str = auth["client_id"]
    cache_key = (token_url, client_id)

    if cache_key in _oauth2_cache:
        token, expires_at = _oauth2_cache[cache_key]
        if time.time() < expires_at - 60:
            logger.debug("[auth] %s: oauth2 token served from cache", server_name)
            return token

    scope = auth.get("scope", "")
    logger.info("[auth] %s: fetching OAuth2 token from %s (scope=%r)", server_name, token_url, scope)

    payload: dict = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": auth["client_secret"],
    }
    if scope:
        payload["scope"] = scope
    if auth.get("audience"):
        payload["audience"] = auth["audience"]

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(token_url, data=payload)
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPStatusError as exc:
        logger.error("[auth] %s: OAuth2 token request failed — HTTP %s from %s",
                     server_name, exc.response.status_code, token_url)
        raise RuntimeError(
            f"[{server_name}] OAuth2 token fetch failed: HTTP {exc.response.status_code}"
        ) from None
    except Exception:
        logger.exception("[auth] %s: OAuth2 token request error (url=%s)", server_name, token_url)
        raise

    token: str = data["access_token"]
    expires_in: int = data.get("expires_in", 3600)
    _oauth2_cache[cache_key] = (token, time.time() + expires_in)
    logger.info("[auth] %s: OAuth2 token cached (expires_in=%ds)", server_name, expires_in)
    return token
