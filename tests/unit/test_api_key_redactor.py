"""Tests for security/api_key_redactor.py"""

from src.mcp_relay.security.api_key_redactor import redact


def test_redacts_api_key_with_equals():
    text = "Error: api key=ABCDEF1234567890 is invalid"
    assert "[REDACTED]" in redact(text)
    assert "ABCDEF1234567890" not in redact(text)


def test_redacts_api_key_with_colon():
    text = "api key: ABCDEF1234567890XYZ"
    assert "[REDACTED]" in redact(text)


def test_redacts_case_insensitive():
    text = "API_KEY=ABCDEF1234567890 failed"
    assert "[REDACTED]" in redact(text)


def test_preserves_label_prefix():
    result = redact("api key: ABCDEF1234567890XYZ")
    assert "api key:" in result
    assert "[REDACTED]" in result


def test_no_match_leaves_text_unchanged():
    text = "The weather today is sunny and 72F."
    assert redact(text) == text


def test_short_value_not_redacted():
    # Values under 8 chars should not be matched
    text = "api key: ABC123"
    assert redact(text) == text


def test_multiple_keys_all_redacted():
    text = "api key: ABCDEF12345678 and api-key: ZYXWVU98765432"
    result = redact(text)
    assert result.count("[REDACTED]") == 2
