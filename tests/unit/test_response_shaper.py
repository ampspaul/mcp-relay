"""Unit tests for the Category A response shaper."""

from __future__ import annotations

import pytest

from src.mcp_relay.transform.response_shaper import (
    _apply_limits,
    _flatten_refs,
    _format_rows,
    _is_ref_object,
    _strip_fields,
    _strip_nulls,
    _strip_system_fields,
    _truncate,
    shape,
    truncate_text,
    validate_response_shape,
)

# ---------------------------------------------------------------------------
# _is_ref_object
# ---------------------------------------------------------------------------


def test_is_ref_object_basic():
    assert _is_ref_object({"descriptor": "Engineering", "id": ["abc123"]})


def test_is_ref_object_with_href():
    assert _is_ref_object({"descriptor": "Q1 Plan", "href": "/plans/1", "id": ["x"]})


def test_is_ref_object_plain_id_string_is_not_ref():
    # When 'id' is a plain string it's more likely a real entity, not a Workday ref.
    assert not _is_ref_object({"descriptor": "Thing", "id": "abc123"})


def test_is_ref_object_extra_key_disqualifies():
    assert not _is_ref_object({"descriptor": "X", "unknown_key": "y"})


def test_is_ref_object_no_descriptor():
    assert not _is_ref_object({"id": ["x"]})


def test_is_ref_object_descriptor_not_string():
    assert not _is_ref_object({"descriptor": 42})


def test_is_ref_object_scalar():
    assert not _is_ref_object("plain string")


# ---------------------------------------------------------------------------
# _flatten_refs
# ---------------------------------------------------------------------------


def test_flatten_refs_replaces_ref_with_descriptor():
    obj = {"department": {"descriptor": "Engineering", "id": ["abc"]}}
    assert _flatten_refs(obj) == {"department": "Engineering"}


def test_flatten_refs_nested():
    obj = [{"role": {"descriptor": "Manager", "href": "/r/1", "id": ["r1"]}}]
    assert _flatten_refs(obj) == [{"role": "Manager"}]


def test_flatten_refs_top_level_ref():
    ref = {"descriptor": "Finance", "id": ["f1"]}
    assert _flatten_refs(ref) == "Finance"


def test_flatten_refs_passthrough_non_ref():
    obj = {"name": "Alice", "score": 99}
    assert _flatten_refs(obj) == {"name": "Alice", "score": 99}


def test_flatten_refs_does_not_flatten_entity_with_plain_id():
    obj = {"id": "emp-001", "descriptor": "Alice Smith", "department": "Eng"}
    # Has extra key 'department' not in _REF_EXTRA_KEYS, and id is a plain string.
    result = _flatten_refs(obj)
    assert isinstance(result, dict)  # not flattened


# ---------------------------------------------------------------------------
# _strip_fields
# ---------------------------------------------------------------------------


def test_strip_fields_removes_named_keys():
    obj = {"a": 1, "b": 2, "c": 3}
    assert _strip_fields(obj, {"b", "c"}) == {"a": 1}


def test_strip_fields_nested():
    obj = {"record": {"name": "X", "created_at": "2024-01-01", "value": 5}}
    result = _strip_fields(obj, {"created_at"})
    assert result == {"record": {"name": "X", "value": 5}}


def test_strip_fields_in_list_of_dicts():
    rows = [{"id": 1, "ts": "t1"}, {"id": 2, "ts": "t2"}]
    assert _strip_fields(rows, {"ts"}) == [{"id": 1}, {"id": 2}]


# ---------------------------------------------------------------------------
# _strip_system_fields
# ---------------------------------------------------------------------------


def test_strip_system_fields_salesforce():
    record = {
        "Id": "001",
        "Name": "Acme",
        "SystemModstamp": "2024-01-01T00:00:00Z",
        "IsDeleted": False,
        "LastModifiedById": "user1",
    }
    result = _strip_system_fields(record)
    assert "SystemModstamp" not in result
    assert "IsDeleted" not in result
    assert "LastModifiedById" not in result
    assert result["Id"] == "001"
    assert result["Name"] == "Acme"


def test_strip_system_fields_api_envelope():
    payload = {
        "data": [{"id": 1}],
        "_links": {"self": "/data"},
        "requestId": "req-123",
        "totalCount": 100,
    }
    result = _strip_system_fields(payload)
    assert "_links" not in result
    assert "requestId" not in result
    assert "totalCount" not in result
    assert result["data"] == [{"id": 1}]


# ---------------------------------------------------------------------------
# _strip_nulls
# ---------------------------------------------------------------------------


def test_strip_nulls_removes_none():
    assert _strip_nulls({"a": None, "b": 1}) == {"b": 1}


def test_strip_nulls_removes_empty_string():
    assert _strip_nulls({"a": "", "b": "hi"}) == {"b": "hi"}


def test_strip_nulls_removes_empty_list():
    assert _strip_nulls({"a": [], "b": [1]}) == {"b": [1]}


def test_strip_nulls_removes_empty_dict():
    assert _strip_nulls({"a": {}, "b": {"x": 1}}) == {"b": {"x": 1}}


def test_strip_nulls_preserves_zero():
    assert _strip_nulls({"count": 0}) == {"count": 0}


def test_strip_nulls_preserves_false():
    assert _strip_nulls({"active": False}) == {"active": False}


def test_strip_nulls_in_list():
    assert _strip_nulls([None, 1, "", 2, []]) == [1, 2]


def test_strip_nulls_nested():
    obj = {"a": {"b": None, "c": 3}}
    assert _strip_nulls(obj) == {"a": {"c": 3}}


def test_strip_nulls_drops_all_null_record_and_logs(caplog):
    import logging

    with caplog.at_level(logging.DEBUG, logger="src.mcp_relay.transform.response_shaper"):
        result = _strip_nulls([{"id": 1, "val": "x"}, {"id": None, "val": None}])
    assert len(result) == 1
    assert result[0]["id"] == 1
    assert "dropped 1" in caplog.text


# ---------------------------------------------------------------------------
# _apply_limits
# ---------------------------------------------------------------------------


def test_apply_limits_max_rows_on_top_level_list():
    rows = [{"id": i} for i in range(10)]
    result = _apply_limits(rows, max_rows=3, max_items=None)
    assert len(result) == 3


def test_apply_limits_max_rows_on_nested_list():
    obj = {"data": [{"id": i} for i in range(10)], "meta": "info"}
    result = _apply_limits(obj, max_rows=4, max_items=None)
    assert len(result["data"]) == 4
    assert result["meta"] == "info"


def test_apply_limits_max_items_on_list():
    obj = ["a", "b", "c", "d"]
    result = _apply_limits(obj, max_rows=None, max_items=2)
    assert result == ["a", "b"]


def test_apply_limits_no_op_when_under_limit():
    rows = [{"id": i} for i in range(3)]
    result = _apply_limits(rows, max_rows=10, max_items=None)
    assert len(result) == 3


def test_apply_limits_both_applied():
    obj = [{"id": i} for i in range(20)]
    # max_rows applies first (list-of-dicts), then max_items on resulting list
    result = _apply_limits(obj, max_rows=10, max_items=5)
    assert len(result) == 5


def test_apply_limits_trims_all_list_of_dicts_keys():
    # Bug fix: previously only the first list-of-dicts key was trimmed.
    obj = {
        "accounts": [{"id": i} for i in range(100)],
        "transactions": [{"id": i} for i in range(200)],
    }
    result = _apply_limits(obj, max_rows=5, max_items=None)
    assert len(result["accounts"]) == 5
    assert len(result["transactions"]) == 5


def test_apply_limits_does_not_trim_scalar_keys():
    obj = {"accounts": [{"id": i} for i in range(20)], "total": 20}
    result = _apply_limits(obj, max_rows=5, max_items=None)
    assert len(result["accounts"]) == 5
    assert result["total"] == 20


# ---------------------------------------------------------------------------
# _format_rows — CSV
# ---------------------------------------------------------------------------


def test_format_rows_csv_top_level_list():
    rows = [{"name": "Alice", "age": 30}, {"name": "Bob", "age": 25}]
    result = _format_rows(rows, "csv")
    assert isinstance(result, str)
    assert "Alice" in result
    assert "Bob" in result
    assert "name" in result  # header present


def test_format_rows_csv_nested_list():
    obj = {"records": [{"x": 1}, {"x": 2}], "count": 2}
    result = _format_rows(obj, "csv")
    assert isinstance(result, dict)
    assert isinstance(result["records"], str)
    assert "x" in result["records"]
    assert result["count"] == 2


def test_format_rows_markdown():
    rows = [{"col": "val1"}, {"col": "val2"}]
    result = _format_rows(rows, "markdown")
    assert "| col |" in result
    assert "| --- |" in result
    assert "val1" in result


def test_format_rows_passthrough_non_list():
    obj = {"scalar": "value"}
    assert _format_rows(obj, "csv") == obj


def test_format_rows_unknown_format_returns_unchanged(caplog):
    rows = [{"a": 1}]
    result = _format_rows(rows, "xml")
    assert result == rows


# ---------------------------------------------------------------------------
# _truncate
# ---------------------------------------------------------------------------


def test_truncate_no_op_when_under_limit():
    assert _truncate("hello", 10) == "hello"


def test_truncate_cuts_long_string():
    result = _truncate("a" * 100, 10)
    assert result.startswith("a" * 10)
    assert "omitted" in result


def test_truncate_serialises_dict():
    obj = {"key": "value"}
    result = _truncate(obj, 5)
    assert isinstance(result, str)
    assert len(result) > 5  # includes omission annotation
    assert "omitted" in result


def test_truncate_preserves_short_dict():
    obj = {"k": "v"}
    result = _truncate(obj, 1000)
    # Short dict is returned unchanged (as dict) — only truncated output becomes a string.
    assert result == obj


# ---------------------------------------------------------------------------
# shape — integration (transforms applied in canonical order)
# ---------------------------------------------------------------------------


def test_shape_full_pipeline():
    obj = {
        "records": [
            {
                "name": "Alice",
                "score": 9.9876,
                "department": {"descriptor": "Engineering", "id": ["d1"]},
                "SystemModstamp": "2024-01-01",
                "notes": None,
                "updated_at": "2024-01-02",
            },
            {
                "name": "Bob",
                "score": 8.1234,
                "department": {"descriptor": "Marketing", "id": ["d2"]},
                "SystemModstamp": "2024-01-01",
                "notes": "",
                "updated_at": "2024-01-02",
            },
        ],
        "requestId": "req-abc",
        "totalCount": 2,
    }

    cfg = {
        "flatten_refs": True,
        "strip_system_fields": True,
        "strip_nulls": True,
        "strip_fields": ["updated_at"],
        "max_rows": 10,
    }
    result = shape(obj, cfg)

    assert "requestId" not in result
    assert "totalCount" not in result
    assert result["records"][0]["department"] == "Engineering"
    assert result["records"][0]["score"] == 9.9876
    assert "notes" not in result["records"][0]
    assert "updated_at" not in result["records"][0]
    assert "SystemModstamp" not in result["records"][0]


def test_shape_max_rows_trims():
    obj = {"data": [{"id": i} for i in range(100)]}
    result = shape(obj, {"max_rows": 5})
    assert len(result["data"]) == 5


def test_shape_format_csv():
    rows = [{"a": 1, "b": 2}, {"a": 3, "b": 4}]
    result = shape(rows, {"format": "csv"})
    assert isinstance(result, str)
    assert "a,b" in result or "b,a" in result


def test_shape_max_chars_truncates():
    obj = {"data": "x" * 5000}
    result = shape(obj, {"max_chars": 100})
    assert isinstance(result, str)
    assert len(result) > 100  # includes omission annotation
    assert "omitted" in result


def test_shape_empty_cfg_passthrough():
    # shape() must run (and no-op) even for an empty config dict.
    # server_registry uses `if shape_cfg is not None:` so {} does reach shape().
    obj = {"a": 1}
    assert shape(obj, {}) == {"a": 1}


def test_truncate_text_under_limit():
    assert truncate_text("hello", 100) == "hello"


def test_truncate_text_over_limit():
    result = truncate_text("a" * 200, 10)
    assert result.startswith("a" * 10)
    assert "omitted" in result


def test_truncate_text_exact_limit():
    assert truncate_text("hello", 5) == "hello"


# ---------------------------------------------------------------------------
# validate_response_shape
# ---------------------------------------------------------------------------


def test_validate_response_shape_valid():
    validate_response_shape(
        {
            "strip_nulls": True,
            "strip_system_fields": True,
            "flatten_refs": True,
            "strip_fields": ["ts"],
            "max_rows": 50,
            "max_items": 10,
            "max_chars": 4000,
            "format": "csv",
        },
        "test",
    )


def test_validate_response_shape_unknown_key():
    with pytest.raises(ValueError, match="unknown response_shape keys"):
        validate_response_shape({"bogus_key": True}, "srv")


def test_validate_response_shape_bad_max_rows_zero():
    with pytest.raises(ValueError, match="max_rows"):
        validate_response_shape({"max_rows": 0}, "srv")


def test_validate_response_shape_bad_max_rows_negative():
    with pytest.raises(ValueError, match="max_rows"):
        validate_response_shape({"max_rows": -1}, "srv")


def test_validate_response_shape_bad_max_rows_string():
    with pytest.raises(ValueError, match="max_rows"):
        validate_response_shape({"max_rows": "fifty"}, "srv")


def test_validate_response_shape_bad_format():
    with pytest.raises(ValueError, match="format"):
        validate_response_shape({"format": "xml"}, "srv")


def test_validate_response_shape_strip_fields_not_list():
    with pytest.raises(ValueError, match="strip_fields"):
        validate_response_shape({"strip_fields": "ts"}, "srv")
