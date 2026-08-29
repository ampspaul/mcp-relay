"""Tests for resilience/rate_limiter.py"""
import datetime
import pytest
from src.mcp_relay.resilience import rate_limiter


def _cfg(name: str, limit: int | None = None, signal_keys: list | None = None) -> dict:
    cfg: dict = {"name": name}
    if limit is not None or signal_keys is not None:
        cfg["rate_limit"] = {}
        if limit is not None:
            cfg["rate_limit"]["requests_per_day"] = limit
        if signal_keys is not None:
            cfg["rate_limit"]["response_signal_keys"] = signal_keys
    return cfg


@pytest.fixture(autouse=True)
def clear_counters():
    rate_limiter._rate_counters.clear()
    yield
    rate_limiter._rate_counters.clear()


def test_no_rate_limit_always_passes():
    cfg = _cfg("no-limit-server")
    for _ in range(1000):
        rate_limiter.check(cfg)  # should not raise


def test_zero_limit_treated_as_disabled():
    cfg = _cfg("zero-limit", limit=0)
    for _ in range(100):
        rate_limiter.check(cfg)


def test_allows_requests_up_to_limit():
    cfg = _cfg("tight-server", limit=3)
    rate_limiter.check(cfg)
    rate_limiter.check(cfg)
    rate_limiter.check(cfg)


def test_blocks_request_over_limit():
    cfg = _cfg("tight-server", limit=2)
    rate_limiter.check(cfg)
    rate_limiter.check(cfg)
    with pytest.raises(RuntimeError, match="daily quota"):
        rate_limiter.check(cfg)


def test_error_contains_server_name():
    cfg = _cfg("my-server", limit=1)
    rate_limiter.check(cfg)
    with pytest.raises(RuntimeError, match="my-server"):
        rate_limiter.check(cfg)


def test_counter_resets_on_new_day():
    cfg = _cfg("daily-server", limit=1)
    rate_limiter.check(cfg)
    # Simulate yesterday's counter
    rate_limiter._rate_counters["daily-server"] = (
        datetime.date.today() - datetime.timedelta(days=1), 999
    )
    rate_limiter.check(cfg)  # should pass — new day, fresh count


def test_different_servers_have_independent_counters():
    cfg_a = _cfg("server-a", limit=1)
    cfg_b = _cfg("server-b", limit=1)
    rate_limiter.check(cfg_a)
    rate_limiter.check(cfg_b)
    with pytest.raises(RuntimeError):
        rate_limiter.check(cfg_a)
    # server-b should still have one call available... already used it
    with pytest.raises(RuntimeError):
        rate_limiter.check(cfg_b)


# --- check_response ---

def test_check_response_no_signal_keys_passes():
    cfg = _cfg("server")
    rate_limiter.check_response(cfg, {"data": "value"})


def test_check_response_key_absent_passes():
    cfg = _cfg("server", signal_keys=["Note"])
    rate_limiter.check_response(cfg, {"data": "value"})


def test_check_response_signal_key_present_raises():
    cfg = _cfg("server", signal_keys=["Note"])
    with pytest.raises(RuntimeError, match="rate limit"):
        rate_limiter.check_response(cfg, {"Note": "API rate limit exceeded."})


def test_check_response_non_dict_passes():
    cfg = _cfg("server", signal_keys=["Note"])
    rate_limiter.check_response(cfg, "plain string response")


def test_check_response_error_contains_server_name():
    cfg = _cfg("finance-api", signal_keys=["Information"])
    with pytest.raises(RuntimeError, match="finance-api"):
        rate_limiter.check_response(cfg, {"Information": "Rate limit hit."})
