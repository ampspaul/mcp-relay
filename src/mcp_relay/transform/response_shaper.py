"""Category A response shaping: proxy-safe, universal noise reduction.

These transforms are applied after the security pipeline (redaction, injection
detection) and before the result is returned to the LLM caller.  Every transform
here is lossless with respect to semantic content — they remove fields that are
never useful in LLM context (Salesforce audit trails, API envelope metadata,
null padding) and impose configurable size limits.

Transform order:
  flatten_refs → strip_fields → strip_system_fields → strip_nulls →
  apply_limits → format_rows → truncate
"""

from __future__ import annotations

import csv
import io
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Salesforce audit/system columns — never carry semantic meaning for an LLM.
_SALESFORCE_SYSTEM_FIELDS: frozenset[str] = frozenset(
    {
        "SystemModstamp",
        "IsDeleted",
        "LastReferencedDate",
        "LastViewedDate",
        "MasterRecordId",
        "LastActivityDate",
        "CreatedById",
        "LastModifiedById",
    }
)

# REST API envelope keys — request metadata, pagination, HATEOAS links.
_API_ENVELOPE_FIELDS: frozenset[str] = frozenset(
    {
        "requestId",
        "request_id",
        "_links",
        "_embedded",
        "@context",
        "totalCount",
        "total_count",
        "pageSize",
        "page_size",
        "pageNumber",
        "page_number",
        "nextPage",
        "next_page",
        "previousPage",
        "previous_page",
        "cursor",
        "hasMore",
        "has_more",
    }
)

_SYSTEM_FIELDS: frozenset[str] = _SALESFORCE_SYSTEM_FIELDS | _API_ENVELOPE_FIELDS

# Workday ref-object sentinel: has 'descriptor' (string) and every other key is
# in this set.  'id' must be a list (not a plain string) to avoid false positives
# on foreign-key ID strings that happen to sit alongside a descriptor.
_REF_EXTRA_KEYS: frozenset[str] = frozenset({"id", "href", "_links", "idType", "value"})


# ---------------------------------------------------------------------------
# Individual transforms
# ---------------------------------------------------------------------------


def _is_ref_object(obj: Any) -> bool:
    """Return True if obj looks like a Workday-style descriptor reference."""
    if not isinstance(obj, dict):
        return False
    descriptor = obj.get("descriptor")
    if not isinstance(descriptor, str):
        return False
    other_keys = set(obj.keys()) - {"descriptor"}
    if not other_keys.issubset(_REF_EXTRA_KEYS):
        return False
    # Guard: if 'id' is a plain string the object is probably a real entity, not a ref.
    if "id" in obj and isinstance(obj["id"], str):
        return False
    return True


def _flatten_refs(obj: Any) -> Any:
    """Replace Workday-style {descriptor: X, id: [...]} objects with just X."""
    if _is_ref_object(obj):
        return obj["descriptor"]
    if isinstance(obj, dict):
        return {k: _flatten_refs(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_flatten_refs(item) for item in obj]
    return obj


def _strip_fields(obj: Any, fields: set[str]) -> Any:
    """Remove named fields from every dict in the structure."""
    if isinstance(obj, dict):
        return {k: _strip_fields(v, fields) for k, v in obj.items() if k not in fields}
    if isinstance(obj, list):
        return [_strip_fields(item, fields) for item in obj]
    return obj


def _strip_system_fields(obj: Any) -> Any:
    """Remove Salesforce audit columns and REST API envelope metadata."""
    return _strip_fields(obj, _SYSTEM_FIELDS)


def _is_empty(value: Any) -> bool:
    """True for None, empty string, empty list, empty dict — but NOT 0 or False."""
    if value is None:
        return True
    if isinstance(value, (str, list, dict)) and len(value) == 0:
        return True
    return False


def _strip_nulls(obj: Any) -> Any:
    """Recursively remove None / "" / [] / {} values.  Preserves 0 and False."""
    if isinstance(obj, dict):
        cleaned = {k: _strip_nulls(v) for k, v in obj.items()}
        return {k: v for k, v in cleaned.items() if not _is_empty(v)}
    if isinstance(obj, list):
        before = len(obj)
        cleaned = [_strip_nulls(item) for item in obj]
        result = [item for item in cleaned if not _is_empty(item)]
        dropped = before - len(result)
        if dropped:
            logger.debug("[shaper] strip_nulls: dropped %d all-null record(s)", dropped)
        return result
    return obj



def _apply_limits(obj: Any, max_rows: int | None, max_items: int | None) -> Any:
    """Cap list lengths.

    max_rows — applied to ALL top-level list-of-dicts keys in a dict, or to
               the top-level list if it is a list-of-dicts.
    max_items — applied to the top-level list regardless of element type.
    """
    if max_rows is not None:
        if isinstance(obj, list) and obj and isinstance(obj[0], dict):
            obj = obj[:max_rows]
        elif isinstance(obj, dict):
            updated = {}
            for k, v in obj.items():
                if isinstance(v, list) and v and isinstance(v[0], dict) and len(v) > max_rows:
                    updated[k] = v[:max_rows]
                else:
                    updated[k] = v
            if updated != obj:
                obj = updated

    if max_items is not None and isinstance(obj, list):
        obj = obj[:max_items]

    return obj


def _is_list_of_dicts(obj: Any) -> bool:
    return isinstance(obj, list) and bool(obj) and all(isinstance(r, dict) for r in obj)


def _to_csv(rows: list[dict]) -> str:
    fieldnames = list(dict.fromkeys(k for row in rows for k in row))
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


def _to_markdown(rows: list[dict]) -> str:
    fieldnames = list(dict.fromkeys(k for row in rows for k in row))
    header = "| " + " | ".join(str(f) for f in fieldnames) + " |"
    sep = "| " + " | ".join("---" for _ in fieldnames) + " |"
    body_lines = [
        "| " + " | ".join(str(row.get(f, "")) for f in fieldnames) + " |" for row in rows
    ]
    return "\n".join([header, sep] + body_lines)


def _format_rows(obj: Any, fmt: str) -> Any:
    """Convert a list-of-dicts to CSV or markdown table (opt-in, changes type to str)."""
    if fmt not in {"csv", "markdown"}:
        logger.warning("[shaper] unknown format %r — skipping", fmt)
        return obj

    rows: list[dict] | None = None
    key: str | None = None

    if _is_list_of_dicts(obj):
        rows = obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if _is_list_of_dicts(v):
                rows = v
                key = k
                break

    if not rows:
        return obj

    formatted = _to_csv(rows) if fmt == "csv" else _to_markdown(rows)

    if key is None:
        return formatted
    return {**obj, key: formatted}


def _truncate(obj: Any, max_chars: int) -> Any:
    """Serialise to string and hard-cap at max_chars, appending an omission note."""
    import json as _json

    text = obj if isinstance(obj, str) else _json.dumps(obj, ensure_ascii=False)
    if len(text) <= max_chars:
        return obj if not isinstance(obj, str) else text
    omitted = len(text) - max_chars
    return text[:max_chars] + f"…[{omitted} chars omitted]"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

_VALID_FORMATS = {"csv", "markdown"}
_VALID_KEYS = frozenset(
    {
        "strip_nulls",
        "strip_system_fields",
        "flatten_refs",
        "strip_fields",
        "max_rows",
        "max_items",
        "max_chars",
        "format",
    }
)


def validate_response_shape(cfg: dict, label: str) -> None:
    """Raise ValueError on bad response_shape config."""
    unknown = set(cfg) - _VALID_KEYS
    if unknown:
        raise ValueError(f"{label}: unknown response_shape keys: {sorted(unknown)}")

    for int_key in ("max_rows", "max_items", "max_chars"):
        val = cfg.get(int_key)
        if val is not None and (not isinstance(val, int) or val <= 0):
            raise ValueError(f"{label}: response_shape.{int_key} must be a positive integer")

    strip_fields = cfg.get("strip_fields")
    if strip_fields is not None and not isinstance(strip_fields, list):
        raise ValueError(f"{label}: response_shape.strip_fields must be a list of strings")

    fmt = cfg.get("format")
    if fmt is not None and fmt not in _VALID_FORMATS:
        raise ValueError(
            f"{label}: response_shape.format must be one of {sorted(_VALID_FORMATS)}"
        )


def truncate_text(text: str, max_chars: int) -> str:
    """Truncate a plain string to max_chars, appending an omission note. Public helper."""
    if len(text) <= max_chars:
        return text
    omitted = len(text) - max_chars
    return text[:max_chars] + f"…[{omitted} chars omitted]"


def shape(obj: Any, cfg: dict) -> Any:
    """Apply all configured Category A transforms in the canonical order."""
    if cfg.get("flatten_refs"):
        obj = _flatten_refs(obj)

    custom_fields = cfg.get("strip_fields")
    if custom_fields:
        obj = _strip_fields(obj, set(custom_fields))

    if cfg.get("strip_system_fields"):
        obj = _strip_system_fields(obj)

    if cfg.get("strip_nulls"):
        obj = _strip_nulls(obj)

    obj = _apply_limits(obj, cfg.get("max_rows"), cfg.get("max_items"))

    fmt = cfg.get("format")
    if fmt:
        obj = _format_rows(obj, fmt)

    max_chars = cfg.get("max_chars")
    if max_chars is not None:
        obj = _truncate(obj, max_chars)

    return obj
