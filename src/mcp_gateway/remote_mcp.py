"""
Remote MCP server connector.

Reads remote_servers.yaml, connects to each enabled server at startup,
discovers its tools, and registers typed proxy callables on the gateway's
FastMCP instance. Adding a new MCP server requires only a new YAML entry.

Per-call connections keep the gateway stateless and resilient to upstream
restarts, at the cost of ~100-300 ms handshake per call.
"""
from __future__ import annotations

import datetime
import inspect
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import anyio
import httpx
import yaml
from mcp.client.session import ClientSession
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamablehttp_client

from src.mcp_gateway.auth import resolve_connection

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).parent.parent.parent / "remote_servers.yaml"

# Transport timeouts
_CONNECT_TIMEOUT = 10.0   # seconds to establish the MCP session
_READ_TIMEOUT    = 30.0   # seconds to wait for an SSE/HTTP response

# ── JSON Schema → Python type ─────────────────────────────────────────────────

_TYPE_MAP: dict[str, type] = {
    "string":  str,
    "integer": int,
    "number":  float,
    "boolean": bool,
    "array":   list,
    "object":  dict,
}


def _py_type(schema: dict) -> type:
    return _TYPE_MAP.get(schema.get("type", "string"), Any)


# ── Config loader ────────────────────────────────────────────────────────────

async def _load_servers() -> list[dict]:
    """Read remote_servers.yaml without blocking the event loop."""
    if not _CONFIG_PATH.exists():
        logger.warning("[remote_mcp] remote_servers.yaml not found at %s", _CONFIG_PATH)
        return []

    def _read() -> list[dict]:
        with _CONFIG_PATH.open() as fh:
            data = yaml.safe_load(fh) or {}
        return data.get("servers") or []

    servers: list[dict] = await anyio.to_thread.run_sync(_read)
    logger.info("[remote_mcp] loaded %d server(s) from remote_servers.yaml", len(servers))
    return servers


# ── Transport session ────────────────────────────────────────────────────────

@asynccontextmanager
async def _open_session(server_cfg: dict):
    """Yield an initialised ClientSession using the server's configured transport."""
    name = server_cfg["name"]
    transport = server_cfg.get("transport", "sse")
    url, headers = await resolve_connection(server_cfg)

    logger.debug("[remote_mcp] %s: opening %s session", name, transport)
    try:
        if transport == "streamable_http":
            async with streamablehttp_client(
                url, headers=headers,
                timeout=_CONNECT_TIMEOUT,
                sse_read_timeout=_READ_TIMEOUT,
            ) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    logger.debug("[remote_mcp] %s: session initialised (streamable_http)", name)
                    yield session
        else:
            async with sse_client(
                url, headers=headers,
                timeout=_CONNECT_TIMEOUT,
                sse_read_timeout=_READ_TIMEOUT,
            ) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    logger.debug("[remote_mcp] %s: session initialised (sse)", name)
                    yield session
    except Exception as exc:
        safe_msg = _safe_exc_msg(exc)
        logger.error("[remote_mcp] %s: session error (transport=%s): %s", name, transport, safe_msg)
        raise


def _safe_exc_msg(exc: BaseException) -> str:
    """Return an exception description that never includes a URL with secrets."""
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code} from upstream"
    if isinstance(exc, httpx.RequestError):
        return f"{type(exc).__name__} (connection-level error)"
    return repr(exc)


# Matches "API key <value>" patterns in upstream error responses.
_API_KEY_RE = re.compile(r'(?i)(api[\s_-]*key[\s:=]+)[A-Za-z0-9]{8,64}')


def _redact(text: str) -> str:
    """Replace embedded API keys in upstream error messages with [REDACTED]."""
    return _API_KEY_RE.sub(r'\1[REDACTED]', text)


# PII patterns applied when a server sets redact_pii: true.
_PII_PATTERNS: list[tuple[re.Pattern, str]] = [
    # Email — before phone so "user@123-456-7890.com" matches email first
    (re.compile(r'[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}'), '[email]'),
    # US SSN — ddd-dd-dddd
    (re.compile(r'\b\d{3}-\d{2}-\d{4}\b'), '[ssn]'),
    # Credit / debit card — 16-digit groups separated by space, dash, or nothing
    (re.compile(r'\b\d{4}[\s\-]?\d{4}[\s\-]?\d{4}[\s\-]?\d{4}\b'), '[card]'),
    # US phone — (ddd) ddd-dddd  |  ddd-ddd-dddd  |  ddd.ddd.dddd  |  +1dddddddddd
    (re.compile(r'(\+1[\s\-]?)?\(?\d{3}\)?[\s.\-]\d{3}[\s.\-]\d{4}\b'), '[phone]'),
]


def _redact_pii(text: str) -> str:
    """Replace common PII (email, SSN, card, phone) with labelled placeholders."""
    for pattern, label in _PII_PATTERNS:
        text = pattern.sub(label, text)
    return text


def _sanitize_args(arguments: dict) -> dict:
    """Redact PII from string argument values before sending them to a remote MCP server.

    Only string values are touched — structured types (int, list, dict) pass through
    unchanged so typed tool schemas are not broken.
    """
    return {k: _redact_pii(v) if isinstance(v, str) else v for k, v in arguments.items()}


# ── Prompt injection detection ────────────────────────────────────────────────

_INJECTION_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(
        r'(?i)ignore\s+(all\s+)?(previous|prior|above|earlier)\s+(instructions?|prompts?|context|rules?)'
    ), "ignore-instructions"),
    (re.compile(
        r'(?i)disregard\s+(all\s+)?(previous|prior|above|earlier|your)\s+(instructions?|prompts?|context|rules?)'
    ), "disregard-instructions"),
    (re.compile(
        r'(?i)override\s+(all\s+)?(previous|prior|your)?\s*(instructions?|rules?|constraints?|guidelines?)'
    ), "override-instructions"),
    (re.compile(
        r'(?i)forget\s+(all\s+)?(previous|prior|above|everything|your)\s*(instructions?|prompts?|context|rules?)?'
    ), "forget-instructions"),
    (re.compile(
        r'(?i)(new|updated|revised|replacement)\s+system\s+prompt'
    ), "new-system-prompt"),
    (re.compile(
        r'(?i)from\s+now\s+on\s+(you\s+(are|should|must|will)|act\s+as|behave\s+as)'
    ), "from-now-on"),
    (re.compile(
        r'(?i)you\s+are\s+now\s+(a|an|the)?\s+\w'
    ), "you-are-now"),
    (re.compile(
        r'(?i)(act\s+as|pretend\s+(you\s+are|to\s+be)|roleplay\s+as)\s+(a|an|the)?\s+\w'
    ), "persona-hijack"),
    (re.compile(
        r'(?i)your\s+(new|real|actual|true|hidden|secret)\s+(instructions?|purpose|role|task|goal|objective)'
    ), "hidden-instructions"),
    (re.compile(
        r'(?i)(exfiltrate|send|transmit|forward|leak|dump)\s+(all\s+)?(this\s+)?'
        r'(data|information|secrets?|credentials?|passwords?|api\s*keys?)\s+to'
    ), "exfiltration"),
    (re.compile(
        r'(?i)(reveal|print|show|output|display|repeat|return|expose)\s+(your\s+)?'
        r'(system\s+prompt|instructions?|initial\s+prompt|original\s+prompt)'
    ), "reveal-prompt"),
    (re.compile(
        r'(?i)bypass\s+(your\s+|all\s+)?(safety|rules?|constraints?|guidelines?|restrictions?|filters?)'
    ), "bypass-safety"),
    (re.compile(
        r'(?i)\b(jailbreak|do\s+anything\s+now)\b'
    ), "jailbreak"),
]


def _check_injection(text: str, server_name: str) -> None:
    """
    Scan *text* for prompt injection patterns and raise RuntimeError if any match.

    Called on the raw tool response string before it reaches the LLM or JSON
    parsing. Enabled per-server via ``injection_detection: true`` in
    remote_servers.yaml. Defaults to false — opt-in only.

    On detection the entire tool result is discarded — the agent receives a
    RuntimeError, not poisoned data. A WARNING log records the pattern label
    and an 80-character snippet for audit.
    """
    for pattern, label in _INJECTION_PATTERNS:
        match = pattern.search(text)
        if match:
            start = max(0, match.start() - 40)
            end   = min(len(text), match.end() + 40)
            snippet = text[start:end].replace("\n", " ").strip()
            logger.warning(
                "[remote_mcp] %s: prompt injection detected pattern=%r near: ...%s...",
                server_name, label, snippet,
            )
            raise RuntimeError(
                f"[{server_name}] tool response blocked — prompt injection attempt "
                f"detected (pattern={label!r}). Response discarded for safety."
            )


# ── LLM-based PII redaction (Ollama) ─────────────────────────────────────────

_OLLAMA_PII_PROMPT = (
    "You are a data privacy filter. Redact every piece of personally identifiable "
    "information (PII) from the text below. Replace each instance with its type in "
    "square brackets — use [NAME] for full or partial names, [EMAIL] for email "
    "addresses, [PHONE] for phone numbers, [ADDRESS] for street addresses or zip "
    "codes, [SSN] for social security numbers, [CARD] for credit or debit card "
    "numbers, [DOB] for dates of birth, [IP] for IP addresses, and [ID] for any "
    "other government-issued identifier. Do not alter any other content. Return only "
    "the redacted text — no explanation, no preamble.\n\nText:\n"
)


async def _llm_redact_pii(text: str, model: str) -> str:
    """
    Send *text* to a local Ollama model and return the PII-redacted version.

    Enabled per-server via ``pii_scan_enabled: true`` + ``pii_scan_model: <model>``
    in remote_servers.yaml. ``pii_scan_enabled`` defaults to false so this
    function is never called unless explicitly opted in.

    Requires Ollama running at OLLAMA_URL (default: http://localhost:11434).
    In production, point OLLAMA_URL at a dedicated GPU-enabled service with
    Ollama installed (e.g. llama3.2:3b or phi3:mini for low latency).

    Falls back to regex-only PII redaction on any error so tool calls are never
    blocked by an unreachable Ollama instance.
    """
    ollama_url = os.environ.get("OLLAMA_URL", "http://localhost:11434").rstrip("/")
    endpoint = f"{ollama_url}/api/generate"
    logger.info("[remote_mcp] pii_scan: model=%r url=%s chars=%d", model, ollama_url, len(text))

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                endpoint,
                json={"model": model, "prompt": _OLLAMA_PII_PROMPT + text, "stream": False},
            )
            resp.raise_for_status()
            redacted = resp.json().get("response", "").strip()
            if not redacted:
                logger.warning("[remote_mcp] pii_scan: Ollama returned empty response — keeping regex output")
                return _redact_pii(text)
            logger.info("[remote_mcp] pii_scan: completed model=%r original=%d redacted=%d chars",
                        model, len(text), len(redacted))
            return redacted
    except httpx.ConnectError:
        logger.warning("[remote_mcp] pii_scan: Ollama unreachable at %s — falling back to regex", ollama_url)
    except httpx.TimeoutException:
        logger.warning("[remote_mcp] pii_scan: Ollama timed out (model=%r) — falling back to regex", model)
    except httpx.HTTPStatusError as exc:
        logger.warning("[remote_mcp] pii_scan: Ollama HTTP %d — falling back to regex", exc.response.status_code)
    except Exception as exc:
        logger.warning("[remote_mcp] pii_scan: unexpected error (%s) — falling back to regex", exc)

    return _redact_pii(text)


# ── Rate limiting ─────────────────────────────────────────────────────────────

# server_name → (date, call_count) — resets automatically on a new calendar day.
_rate_counters: dict[str, tuple[datetime.date, int]] = {}


def _check_rate_limit(server_cfg: dict) -> None:
    """Raise RuntimeError if the server's daily request quota is exhausted.

    Configured via rate_limit.requests_per_day in remote_servers.yaml.
    """
    limit: int | None = (server_cfg.get("rate_limit") or {}).get("requests_per_day")
    if not limit:
        return
    name = server_cfg["name"]
    today = datetime.date.today()
    date, count = _rate_counters.get(name, (today, 0))
    if date != today:
        date, count = today, 0
    if count >= limit:
        raise RuntimeError(
            f"[{name}] daily quota of {limit} request(s) per day exhausted. "
            "Try again tomorrow or upgrade your API plan."
        )
    _rate_counters[name] = (today, count + 1)
    logger.debug("[remote_mcp] %s: rate limit %d/%d used today", name, count + 1, limit)


def _check_response_for_rate_limit(server_cfg: dict, parsed: Any) -> None:
    """Detect rate-limit signals embedded in an otherwise-successful JSON response.

    Some APIs (e.g. Alpha Vantage) return HTTP 200 with a JSON body like
    {"Note": "...rate limit..."} instead of an error status. Without this
    check the LLM receives the note as a normal tool result and may silently
    fail or hallucinate data.

    Configured via rate_limit.response_signal_keys in remote_servers.yaml.
    """
    signal_keys: list[str] = (server_cfg.get("rate_limit") or {}).get("response_signal_keys", [])
    if not signal_keys or not isinstance(parsed, dict):
        return
    for key in signal_keys:
        if key in parsed:
            name = server_cfg["name"]
            msg = str(parsed[key])[:300]
            logger.warning("[remote_mcp] %s: rate-limit signal detected (key=%r): %s",
                           name, key, msg)
            raise RuntimeError(f"[{name}] rate limit reached — {msg}")


# ── Discovery ────────────────────────────────────────────────────────────────

async def _discover_tools(server_cfg: dict) -> list:
    name = server_cfg["name"]
    logger.info("[remote_mcp] %s: discovering tools...", name)
    try:
        async with _open_session(server_cfg) as session:
            result = await session.list_tools()
        logger.info("[remote_mcp] %s: discovered %d tool(s)", name, len(result.tools))
        return result.tools
    except Exception:
        logger.error("[remote_mcp] %s: tool discovery failed — server will be skipped", name)
        return []


# ── Tool call ────────────────────────────────────────────────────────────────

async def _call_remote(server_cfg: dict, tool_name: str, arguments: dict) -> Any:
    name = server_cfg["name"]
    logger.info("[remote_mcp] %s: calling tool=%s args_keys=%s",
                name, tool_name, list(arguments.keys()))
    _check_rate_limit(server_cfg)
    if server_cfg.get("sanitize_input"):
        arguments = _sanitize_args(arguments)
    try:
        async with _open_session(server_cfg) as session:
            result = await session.call_tool(tool_name, arguments)
    except RuntimeError:
        raise
    except Exception as exc:
        safe_msg = _safe_exc_msg(exc)
        logger.error("[remote_mcp] %s: tool call failed (tool=%s): %s", name, tool_name, safe_msg)
        raise RuntimeError(
            f"[{name}] remote tool {tool_name!r} transport error: {safe_msg}"
        ) from None

    if getattr(result, "isError", False):
        error_text = "unknown error"
        if result.content and hasattr(result.content[0], "text"):
            error_text = result.content[0].text
        safe_text = _redact(error_text)
        logger.error("[remote_mcp] %s: tool=%s returned isError=True: %s",
                     name, tool_name, safe_text[:200])
        raise RuntimeError(f"[{name}] remote tool {tool_name!r} failed: {safe_text}")

    if not result.content:
        logger.debug("[remote_mcp] %s: tool=%s returned empty content", name, tool_name)
        return {}

    first = result.content[0]
    if hasattr(first, "text"):
        # Apply output filters before the text reaches JSON parsing or the LLM.
        # Each filter is opt-in per server via remote_servers.yaml flags.
        raw = first.text
        if server_cfg.get("sanitize_output"):
            raw = _redact(raw)
        if server_cfg.get("redact_pii"):
            raw = _redact_pii(raw)
        if server_cfg.get("pii_scan_enabled", False) and (pii_model := server_cfg.get("pii_scan_model")):
            raw = await _llm_redact_pii(raw, pii_model)
        if server_cfg.get("injection_detection", False):
            _check_injection(raw, name)
        try:
            parsed = json.loads(raw)
            _check_response_for_rate_limit(server_cfg, parsed)
            logger.debug("[remote_mcp] %s: tool=%s returned JSON (%d bytes)",
                         name, tool_name, len(raw))
            return parsed
        except (json.JSONDecodeError, TypeError):
            logger.debug("[remote_mcp] %s: tool=%s returned plain text (%d chars)",
                         name, tool_name, len(raw))
            return raw
    if hasattr(first, "data"):
        return first.data
    return {}


# ── Dynamic proxy builder ────────────────────────────────────────────────────

def _make_proxy(server_cfg: dict, remote_tool_name: str, input_schema: dict) -> Any:
    """Return a typed async callable that forwards calls to a remote MCP tool."""
    props: dict = input_schema.get("properties", {})
    required: set[str] = set(input_schema.get("required", []))

    params: list[inspect.Parameter] = []
    annotations: dict[str, Any] = {}

    for pname, pschema in props.items():
        py_t = _py_type(pschema)
        if pname in required:
            param = inspect.Parameter(
                pname, inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=py_t,
            )
            annotations[pname] = py_t
        else:
            param = inspect.Parameter(
                pname, inspect.Parameter.POSITIONAL_OR_KEYWORD,
                default=None, annotation=py_t | None,
            )
            annotations[pname] = py_t | None
        params.append(param)

    annotations["return"] = dict
    sig = inspect.Signature(params, return_annotation=dict)

    _cfg = server_cfg
    _name = remote_tool_name

    async def _proxy(**kwargs: Any) -> dict:
        args = {k: v for k, v in kwargs.items() if v is not None}
        return await _call_remote(_cfg, _name, args)

    _proxy.__signature__ = sig
    _proxy.__annotations__ = annotations
    return _proxy


# ── Public API ───────────────────────────────────────────────────────────────

async def register_remote_servers(mcp: Any) -> int:
    """
    Discover tools from all enabled remote servers and register proxy callables
    on the FastMCP instance. Returns the total number of tools registered.

    Raises RuntimeError if all enabled servers return zero tools — prevents a
    silently broken gateway from passing health checks.
    """
    configs = await _load_servers()
    total = 0
    enabled_count = 0
    registered_tool_names: set[str] = set()

    for cfg in configs:
        name = cfg["name"]
        if not cfg.get("enabled", True):
            logger.info("[remote_mcp] %s: disabled — skipping", name)
            continue

        enabled_count += 1
        prefix = cfg.get("tool_prefix", "")
        tools = await _discover_tools(cfg)

        if not tools:
            logger.warning("[remote_mcp] %s: no tools registered (discovery returned 0)", name)
            continue

        registered_this_server = 0
        for tool in tools:
            proxy_name = f"{prefix}{tool.name}" if prefix else tool.name

            if proxy_name in registered_tool_names:
                logger.error(
                    "[remote_mcp] %s: tool name collision — %r already registered by another "
                    "server. Set tool_prefix to avoid ambiguity. Skipping.",
                    name, proxy_name,
                )
                continue

            schema: dict = tool.inputSchema or {}
            proxy_fn = _make_proxy(cfg, tool.name, schema)
            proxy_fn.__name__ = proxy_name
            proxy_fn.__qualname__ = proxy_name
            mcp.add_tool(proxy_fn, name=proxy_name, description=tool.description or "")
            registered_tool_names.add(proxy_name)
            logger.debug("[remote_mcp] registered proxy: %s → %s@%s", proxy_name, tool.name, name)
            total += 1
            registered_this_server += 1

        logger.info("[remote_mcp] %s: %d proxy tool(s) registered (prefix=%r)",
                    name, registered_this_server, prefix)

    if enabled_count > 0 and total == 0:
        raise RuntimeError(
            "[remote_mcp] startup failed: all enabled remote servers returned 0 tools. "
            "Check secret backend configuration and server connectivity."
        )

    return total
