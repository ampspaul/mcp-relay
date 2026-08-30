"""Prompt injection detection for tool responses."""

from __future__ import annotations

import logging
import re

from ..observability import metrics

logger = logging.getLogger(__name__)

_INJECTION_PATTERNS: list[tuple[re.Pattern, str]] = [
    (
        re.compile(
            r"(?i)ignore\s+(all\s+)?(previous|prior|above|earlier)\s+(instructions?|prompts?|context|rules?)"
        ),
        "ignore-instructions",
    ),
    (
        re.compile(
            r"(?i)disregard\s+(all\s+)?(previous|prior|above|earlier|your)\s+(instructions?|prompts?|context|rules?)"
        ),
        "disregard-instructions",
    ),
    (
        re.compile(
            r"(?i)override\s+(all\s+)?(previous|prior|your)?\s*(instructions?|rules?|constraints?|guidelines?)"
        ),
        "override-instructions",
    ),
    (
        re.compile(
            r"(?i)forget\s+(all\s+)?(previous|prior|above|everything|your)\s*(instructions?|prompts?|context|rules?)?"
        ),
        "forget-instructions",
    ),
    (re.compile(r"(?i)(new|updated|revised|replacement)\s+system\s+prompt"), "new-system-prompt"),
    (
        re.compile(r"(?i)from\s+now\s+on\s+(you\s+(are|should|must|will)|act\s+as|behave\s+as)"),
        "from-now-on",
    ),
    (
        re.compile(
            r"(?i)(act\s+as|pretend\s+(you\s+are|to\s+be)|roleplay\s+as)\s+(a|an|the)?\s+\w"
        ),
        "persona-hijack",
    ),
    (
        re.compile(
            r"(?i)your\s+(new|real|actual|true|hidden|secret)\s+(instructions?|purpose|role|task|goal|objective)"
        ),
        "hidden-instructions",
    ),
    (
        re.compile(
            r"(?i)(exfiltrate|send|transmit|forward|leak|dump)\s+(all\s+)?(this\s+)?(data|information|secrets?|credentials?|passwords?|api\s*keys?)\s+to"
        ),
        "exfiltration",
    ),
    (
        re.compile(
            r"(?i)(reveal|print|show|output|display|repeat|return|expose)\s+(your\s+)?(system\s+prompt|instructions?|initial\s+prompt|original\s+prompt)"
        ),
        "reveal-prompt",
    ),
    (
        re.compile(
            r"(?i)bypass\s+(your\s+|all\s+)?(safety|rules?|constraints?|guidelines?|restrictions?|filters?)"
        ),
        "bypass-safety",
    ),
    (re.compile(r"(?i)\b(jailbreak|do\s+anything\s+now)\b"), "jailbreak"),
]


def check(text: str, server_name: str) -> None:
    for pattern, label in _INJECTION_PATTERNS:
        match = pattern.search(text)
        if match:
            start = max(0, match.start() - 40)
            end = min(len(text), match.end() + 40)
            snippet = text[start:end].replace("\n", " ").strip()
            logger.warning(
                "[security] %s: prompt injection detected pattern=%r near: ...%s...",
                server_name,
                label,
                snippet,
            )
            metrics.increment("injection_blocked_total", server=server_name, pattern=label)
            raise RuntimeError(
                f"[{server_name}] tool response blocked — prompt injection attempt "
                f"detected (pattern={label!r}). Response discarded for safety."
            )
