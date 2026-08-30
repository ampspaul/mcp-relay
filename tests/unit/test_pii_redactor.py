"""Tests for security/pii_redactor.py"""

from src.mcp_relay.security.pii_redactor import redact, sanitize_args


def test_redacts_email():
    result = redact("Contact us at alice@example.com for help.")
    assert "[email]" in result
    assert "alice@example.com" not in result


def test_redacts_ssn():
    result = redact("SSN: 123-45-6789")
    assert "[ssn]" in result
    assert "123-45-6789" not in result


def test_redacts_credit_card():
    result = redact("Card: 4111 1111 1111 1111 was declined.")
    assert "[card]" in result
    assert "4111 1111 1111 1111" not in result


def test_redacts_credit_card_no_spaces():
    result = redact("Card: 4111111111111111")
    assert "[card]" in result


def test_redacts_us_phone_dashes():
    result = redact("Call me at 555-867-5309.")
    assert "[phone]" in result
    assert "555-867-5309" not in result


def test_redacts_us_phone_dots():
    result = redact("Call 555.867.5309 now.")
    assert "[phone]" in result


def test_multiple_pii_types_all_redacted():
    text = "Email: bob@test.com, SSN: 987-65-4321, Phone: 555-123-4567"
    result = redact(text)
    assert "[email]" in result
    assert "[ssn]" in result
    assert "[phone]" in result
    assert "bob@test.com" not in result


def test_clean_text_unchanged():
    text = "The temperature is 72 degrees today."
    assert redact(text) == text


def test_sanitize_args_redacts_string_values():
    args = {"query": "contact alice@example.com", "limit": 10}
    result = sanitize_args(args)
    assert "[email]" in result["query"]
    assert result["limit"] == 10


def test_sanitize_args_leaves_non_strings_unchanged():
    args = {"count": 5, "enabled": True, "tags": ["a", "b"]}
    result = sanitize_args(args)
    assert result == args


def test_sanitize_args_empty_dict():
    assert sanitize_args({}) == {}
