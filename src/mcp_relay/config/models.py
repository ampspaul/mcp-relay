"""Typed config models for server definitions (uses plain dataclasses — no extra deps)."""
from __future__ import annotations
from dataclasses import dataclass, field


@dataclass
class AuthConfig:
    type: str = "none"
    value: str | None = None
    param_name: str | None = None
    placeholder: str | None = None
    header_name: str | None = None
    token_url: str | None = None
    client_id: str | None = None
    client_secret: str | None = None
    scope: str | None = None
    audience: str | None = None


@dataclass
class RateLimitConfig:
    requests_per_day: int | None = None
    response_signal_keys: list[str] = field(default_factory=list)


@dataclass
class ServerConfig:
    name: str
    url: str
    description: str = ""
    transport: str = "sse"
    enabled: bool = True
    tool_prefix: str = ""
    auth: AuthConfig = field(default_factory=AuthConfig)
    rate_limit: RateLimitConfig | None = None
    sanitize_input: bool = False
    sanitize_output: bool = False
    redact_pii: bool = False
    pii_scan_enabled: bool = False
    pii_scan_model: str | None = None
    injection_detection: bool = False
