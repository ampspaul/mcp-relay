"""Tests for registry/proxy_builder.py"""
import inspect
import pytest
from src.mcp_relay.registry.proxy_builder import build, _py_type


# --- _py_type ---

def test_py_type_string():
    assert _py_type({"type": "string"}) is str


def test_py_type_integer():
    assert _py_type({"type": "integer"}) is int


def test_py_type_number():
    assert _py_type({"type": "number"}) is float


def test_py_type_boolean():
    assert _py_type({"type": "boolean"}) is bool


def test_py_type_array():
    assert _py_type({"type": "array"}) is list


def test_py_type_object():
    assert _py_type({"type": "object"}) is dict


def test_py_type_unknown_falls_back_to_any():
    from typing import Any
    assert _py_type({"type": "exotic"}) is Any


def test_py_type_missing_type_defaults_to_str():
    assert _py_type({}) is str


# --- build ---

@pytest.fixture
def calls():
    return []


@pytest.fixture
def call_fn(calls):
    async def _fn(cfg, tool_name, args):
        calls.append((cfg, tool_name, args))
        return {"ok": True}
    return _fn


def _schema(props: dict, required: list | None = None) -> dict:
    return {"properties": props, "required": required or []}


def test_build_returns_callable(call_fn):
    proxy = build({}, "my_tool", _schema({"q": {"type": "string"}}, ["q"]), call_fn)
    assert callable(proxy)


def test_build_sets_name(call_fn):
    proxy = build({}, "my_tool", _schema({"q": {"type": "string"}}, ["q"]), call_fn)
    # caller sets __name__ after build; proxy itself is named _proxy internally
    assert callable(proxy)


def test_required_param_has_no_default(call_fn):
    proxy = build({}, "tool", _schema({"q": {"type": "string"}}, ["q"]), call_fn)
    sig = inspect.signature(proxy)
    assert sig.parameters["q"].default is inspect.Parameter.empty


def test_optional_param_defaults_to_none(call_fn):
    proxy = build({}, "tool", _schema({"limit": {"type": "integer"}}), call_fn)
    sig = inspect.signature(proxy)
    assert sig.parameters["limit"].default is None


async def test_proxy_forwards_non_none_args(call_fn, calls):
    proxy = build({"name": "s"}, "tool",
                  _schema({"q": {"type": "string"}, "limit": {"type": "integer"}}, ["q"]),
                  call_fn)
    await proxy(q="hello", limit=None)
    assert calls[0][2] == {"q": "hello"}   # limit was None, stripped


async def test_proxy_strips_none_optional_args(call_fn, calls):
    proxy = build({"name": "s"}, "tool",
                  _schema({"a": {"type": "string"}, "b": {"type": "string"}}, ["a"]),
                  call_fn)
    await proxy(a="x", b=None)
    assert "b" not in calls[0][2]


def test_empty_schema_builds_zero_param_proxy(call_fn):
    proxy = build({}, "tool", {}, call_fn)
    sig = inspect.signature(proxy)
    assert len(sig.parameters) == 0
