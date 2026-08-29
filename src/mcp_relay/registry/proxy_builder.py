"""Build typed async proxy callables from MCP tool input schemas."""
from __future__ import annotations
import inspect
import logging
from typing import Any, Callable, Awaitable

logger = logging.getLogger(__name__)

_TYPE_MAP: dict[str, type] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list,
    "object": dict,
}


def _py_type(schema: dict) -> type:
    return _TYPE_MAP.get(schema.get("type", "string"), Any)


def build(
    server_cfg: dict,
    remote_tool_name: str,
    input_schema: dict,
    call_fn: Callable[..., Awaitable[Any]],
) -> Any:
    props: dict = input_schema.get("properties", {})
    required: set[str] = set(input_schema.get("required", []))

    params: list[inspect.Parameter] = []
    annotations: dict[str, Any] = {}

    for pname, pschema in props.items():
        py_t = _py_type(pschema)
        if pname in required:
            param = inspect.Parameter(pname, inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=py_t)
            annotations[pname] = py_t
        else:
            param = inspect.Parameter(
                pname, inspect.Parameter.POSITIONAL_OR_KEYWORD,
                default=None, annotation=py_t | None,
            )
            annotations[pname] = py_t | None
        params.append(param)

    annotations["return"] = Any
    sig = inspect.Signature(params, return_annotation=Any)

    _cfg = server_cfg
    _name = remote_tool_name

    async def _proxy(**kwargs: Any) -> Any:
        args = {k: v for k, v in kwargs.items() if v is not None}
        return await call_fn(_cfg, _name, args)

    _proxy.__signature__ = sig
    _proxy.__annotations__ = annotations
    return _proxy
