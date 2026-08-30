"""Tests for auth/credential_cache.py and auth/resolver.py"""

import time
from unittest.mock import AsyncMock, patch

import pytest

from src.mcp_relay.auth import credential_cache
from src.mcp_relay.auth.resolver import _mask_url, _mask_url_path_segment, resolve_connection


@pytest.fixture(autouse=True)
def clear_cache():
    credential_cache._credential_cache.clear()
    yield
    credential_cache._credential_cache.clear()


# --- credential_cache ---


def test_get_cached_miss_returns_none():
    assert credential_cache.get_cached("missing") is None


def test_set_and_get_cached():
    credential_cache.set_cached("srv", "https://example.com", {"X-Key": "abc"})
    result = credential_cache.get_cached("srv")
    assert result == ("https://example.com", {"X-Key": "abc"})


def test_cached_entry_expires():
    credential_cache._credential_cache["srv"] = (
        ("https://example.com", {}),
        time.time() - 1,  # already expired
    )
    assert credential_cache.get_cached("srv") is None


def test_invalidate_specific_server():
    credential_cache.set_cached("a", "https://a.com", {})
    credential_cache.set_cached("b", "https://b.com", {})
    credential_cache.invalidate("a")
    assert credential_cache.get_cached("a") is None
    assert credential_cache.get_cached("b") is not None


def test_invalidate_all():
    credential_cache.set_cached("a", "https://a.com", {})
    credential_cache.set_cached("b", "https://b.com", {})
    credential_cache.invalidate()
    assert credential_cache.get_cached("a") is None
    assert credential_cache.get_cached("b") is None


def test_returned_headers_are_a_copy():
    credential_cache.set_cached("srv", "https://example.com", {"X-Key": "abc"})
    result1 = credential_cache.get_cached("srv")
    result1[1]["X-Key"] = "MUTATED"
    result2 = credential_cache.get_cached("srv")
    assert result2[1]["X-Key"] == "abc"


# --- _mask_url ---


def test_mask_url_replaces_query_values():
    url = "https://api.example.com/mcp?api_key=supersecret&format=json"
    masked = _mask_url(url)
    assert "supersecret" not in masked
    assert "***" in masked
    assert "api_key" in masked


def test_mask_url_no_query_unchanged():
    url = "https://api.example.com/mcp"
    assert _mask_url(url) == url


def test_mask_url_path_segment():
    url = "https://api.example.com/SECRETKEY123/mcp"
    masked = _mask_url_path_segment(url, "SECRETKEY123")
    assert "SECRETKEY123" not in masked
    assert "***" in masked


def test_mask_url_path_segment_empty_secret_unchanged():
    url = "https://api.example.com/mcp"
    assert _mask_url_path_segment(url, "") == url


# --- resolve_connection (auth types) ---


@pytest.mark.asyncio
async def test_resolve_none_auth():
    cfg = {"name": "srv", "url": "https://example.com/mcp", "auth": {"type": "none"}}
    with patch(
        "src.mcp_relay.auth.resolver.resolve_secret_refs",
        new=AsyncMock(return_value={"type": "none"}),
    ):
        url, headers = await resolve_connection(cfg)
    assert url == "https://example.com/mcp"
    assert headers == {}


@pytest.mark.asyncio
async def test_resolve_api_key_query():
    cfg = {"name": "srv", "url": "https://example.com/mcp"}
    auth = {"type": "api_key_query", "param_name": "key", "value": "mytoken"}
    with patch("src.mcp_relay.auth.resolver.resolve_secret_refs", new=AsyncMock(return_value=auth)):
        url, headers = await resolve_connection(cfg)
    assert "key=mytoken" in url
    assert headers == {}


@pytest.mark.asyncio
async def test_resolve_api_key_header():
    cfg = {"name": "srv", "url": "https://example.com/mcp"}
    auth = {"type": "api_key_header", "header_name": "X-API-Key", "value": "mytoken"}
    with patch("src.mcp_relay.auth.resolver.resolve_secret_refs", new=AsyncMock(return_value=auth)):
        url, headers = await resolve_connection(cfg)
    assert headers["X-API-Key"] == "mytoken"


@pytest.mark.asyncio
async def test_resolve_bearer():
    cfg = {"name": "srv", "url": "https://example.com/mcp"}
    auth = {"type": "bearer", "value": "tok123"}
    with patch("src.mcp_relay.auth.resolver.resolve_secret_refs", new=AsyncMock(return_value=auth)):
        url, headers = await resolve_connection(cfg)
    assert headers["Authorization"] == "Bearer tok123"


@pytest.mark.asyncio
async def test_resolve_api_key_url_path():
    cfg = {"name": "srv", "url": "https://example.com/{api_key}/mcp"}
    auth = {"type": "api_key_url_path", "placeholder": "{api_key}", "value": "MYKEY"}
    with patch("src.mcp_relay.auth.resolver.resolve_secret_refs", new=AsyncMock(return_value=auth)):
        url, headers = await resolve_connection(cfg)
    assert "MYKEY" in url
    assert "{api_key}" not in url


@pytest.mark.asyncio
async def test_resolve_unknown_auth_type_raises():
    cfg = {"name": "srv", "url": "https://example.com/mcp"}
    auth = {"type": "magic"}
    with patch("src.mcp_relay.auth.resolver.resolve_secret_refs", new=AsyncMock(return_value=auth)):
        with pytest.raises(ValueError, match="unknown auth type"):
            await resolve_connection(cfg)


@pytest.mark.asyncio
async def test_resolve_caches_result():
    cfg = {"name": "cached-srv", "url": "https://example.com/mcp"}
    auth = {"type": "none"}
    mock = AsyncMock(return_value=auth)
    with patch("src.mcp_relay.auth.resolver.resolve_secret_refs", new=mock):
        await resolve_connection(cfg)
        await resolve_connection(cfg)
    # secret resolver called only once — second call served from cache
    assert mock.call_count == 1


@pytest.mark.asyncio
async def test_resolve_url_path_missing_placeholder_raises():
    cfg = {"name": "srv", "url": "https://example.com/mcp"}
    auth = {"type": "api_key_url_path", "placeholder": "{api_key}", "value": "K"}
    with patch("src.mcp_relay.auth.resolver.resolve_secret_refs", new=AsyncMock(return_value=auth)):
        with pytest.raises(ValueError, match="placeholder"):
            await resolve_connection(cfg)
