"""Regex and LLM-based PII redaction for tool inputs and outputs."""
from __future__ import annotations
import logging
import os
import re
import httpx

logger = logging.getLogger(__name__)

_PII_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r'[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}'), '[email]'),
    (re.compile(r'\b\d{3}-\d{2}-\d{4}\b'), '[ssn]'),
    (re.compile(r'\b\d{4}[\s\-]?\d{4}[\s\-]?\d{4}[\s\-]?\d{4}\b'), '[card]'),
    (re.compile(r'(\+1[\s\-]?)?\(?\d{3}\)?[\s.\-]\d{3}[\s.\-]\d{4}\b'), '[phone]'),
]

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


def redact(text: str) -> str:
    for pattern, label in _PII_PATTERNS:
        text = pattern.sub(label, text)
    return text


def sanitize_args(arguments: dict) -> dict:
    return {k: redact(v) if isinstance(v, str) else v for k, v in arguments.items()}


async def llm_redact(text: str, model: str) -> str:
    ollama_url = os.environ.get("OLLAMA_URL", "http://localhost:11434").rstrip("/")
    endpoint = f"{ollama_url}/api/generate"
    logger.info("[security] pii_scan: model=%r url=%s chars=%d", model, ollama_url, len(text))

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                endpoint,
                json={"model": model, "prompt": _OLLAMA_PII_PROMPT + text, "stream": False},
            )
            resp.raise_for_status()
            result = resp.json().get("response", "").strip()
            if not result:
                logger.warning("[security] pii_scan: Ollama returned empty response — falling back to regex")
                return redact(text)
            logger.info("[security] pii_scan: completed model=%r original=%d redacted=%d chars",
                        model, len(text), len(result))
            return result
    except httpx.ConnectError:
        logger.warning("[security] pii_scan: Ollama unreachable at %s — falling back to regex", ollama_url)
    except httpx.TimeoutException:
        logger.warning("[security] pii_scan: Ollama timed out (model=%r) — falling back to regex", model)
    except httpx.HTTPStatusError as exc:
        logger.warning("[security] pii_scan: Ollama HTTP %d — falling back to regex", exc.response.status_code)
    except Exception as exc:
        logger.warning("[security] pii_scan: unexpected error (%s) — falling back to regex", exc)

    return redact(text)
