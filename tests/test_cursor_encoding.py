"""Unit tests for cursor encode/decode round-trips and tamper rejection.

Covers:
  - Valid cursor encodes to opaque base64url string.
  - Decoded cursor matches original epoch value.
  - Tampered cursor (truncated, bit-flipped, invalid base64url) → raises
    ValueError or KeyError (not 500 / not a silent swallow).
  - Cursor never leaks the internal field name in plain text.

encode_cursor and decode_cursor live in services/archive.py per implementation.

ADR references: ADR-018 (RFC 9457 errors), brief §2 cursor pagination spec.
"""

from __future__ import annotations

import base64
import json

import pytest


class TestCursorEncodeDecode:
    """encode_cursor / decode_cursor round-trip."""

    def test_encode_returns_non_empty_string(self) -> None:
        """encode_cursor with a valid epoch returns a non-empty string."""
        from weewx_clearskies_api.services.archive import encode_cursor

        result = encode_cursor(after_datetime=1778099700)
        assert isinstance(result, str)
        assert len(result) > 0

    def test_encode_is_base64url_safe(self) -> None:
        """Encoded cursor contains only URL-safe base64 characters (no +, /, =)."""
        from weewx_clearskies_api.services.archive import encode_cursor

        result = encode_cursor(after_datetime=1778099700)
        # base64url uses - and _ instead of + and /; no padding = is preferred
        assert "+" not in result, "Cursor must use URL-safe base64 (no +)"
        assert "/" not in result, "Cursor must use URL-safe base64 (no /)"

    def test_round_trip_preserves_epoch(self) -> None:
        """decode_cursor(encode_cursor(ts)) == ts."""
        from weewx_clearskies_api.services.archive import decode_cursor, encode_cursor

        epoch = 1778099700
        encoded = encode_cursor(after_datetime=epoch)
        decoded = decode_cursor(encoded)

        assert decoded == epoch, (
            f"Round-trip failed: encode then decode returned {decoded!r}, expected {epoch!r}"
        )

    def test_round_trip_with_zero_epoch(self) -> None:
        """Edge case: epoch=0 round-trips correctly."""
        from weewx_clearskies_api.services.archive import decode_cursor, encode_cursor

        encoded = encode_cursor(after_datetime=0)
        decoded = decode_cursor(encoded)
        assert decoded == 0

    def test_round_trip_with_large_epoch(self) -> None:
        """Large epoch (far future) round-trips without overflow."""
        from weewx_clearskies_api.services.archive import decode_cursor, encode_cursor

        large_epoch = 9_999_999_999
        encoded = encode_cursor(after_datetime=large_epoch)
        decoded = decode_cursor(encoded)
        assert decoded == large_epoch

    def test_different_epochs_produce_different_cursors(self) -> None:
        """Two different epochs must produce two different cursors."""
        from weewx_clearskies_api.services.archive import encode_cursor

        c1 = encode_cursor(after_datetime=1778099700)
        c2 = encode_cursor(after_datetime=1778099400)
        assert c1 != c2, "Different epochs must not produce the same cursor"


class TestTamperedCursorRejection:
    """Tampered cursors must be rejected with a specific error, not 500."""

    def test_empty_cursor_raises_specific_error(self) -> None:
        """Empty string cursor → raises ValueError or KeyError."""
        from weewx_clearskies_api.services.archive import decode_cursor

        with pytest.raises((ValueError, KeyError)):
            decode_cursor("")

    def test_truncated_base64_cursor_raises_specific_error(self) -> None:
        """Truncated base64 cursor (invalid padding) → raises specific error."""
        from weewx_clearskies_api.services.archive import decode_cursor

        truncated = "YWJj"[:-1]  # Lop off last char to break padding
        with pytest.raises((ValueError, KeyError)):
            decode_cursor(truncated)

    def test_non_base64_cursor_raises_specific_error(self) -> None:
        """Arbitrary non-base64 string → raises specific error."""
        from weewx_clearskies_api.services.archive import decode_cursor

        with pytest.raises((ValueError, KeyError)):
            decode_cursor("not!valid!base64!!!")

    def test_valid_base64_but_wrong_json_raises_specific_error(self) -> None:
        """Valid base64url encoding of non-JSON → raises specific error."""
        from weewx_clearskies_api.services.archive import decode_cursor

        # Encode a non-JSON payload as base64url
        not_json = base64.urlsafe_b64encode(b"this is not json").rstrip(b"=").decode()
        with pytest.raises((ValueError, KeyError)):
            decode_cursor(not_json)

    def test_valid_json_but_missing_expected_key_raises_specific_error(self) -> None:
        """Base64url-encoded JSON without expected cursor key → raises specific error."""
        from weewx_clearskies_api.services.archive import decode_cursor

        # Build valid JSON but without the expected key
        wrong_json = json.dumps({"something_else": 12345}).encode()
        encoded = base64.urlsafe_b64encode(wrong_json).rstrip(b"=").decode()
        with pytest.raises((ValueError, KeyError)):
            decode_cursor(encoded)

    def test_bit_flipped_cursor_either_raises_or_returns_different_epoch(
        self,
    ) -> None:
        """Single bit flip in an otherwise valid cursor raises error or returns wrong epoch."""
        from weewx_clearskies_api.services.archive import decode_cursor, encode_cursor

        valid = encode_cursor(after_datetime=1778099700)
        # Flip the last character
        if valid:
            flipped_char = "A" if valid[-1] != "A" else "B"
            tampered = valid[:-1] + flipped_char
            try:
                result = decode_cursor(tampered)
                # If it didn't raise, the tampered cursor must not equal the original epoch
                assert result != 1778099700, (
                    "Tampered cursor must not silently decode to the original epoch"
                )
            except (ValueError, KeyError):
                pass  # Expected — tamper was detected

    def test_cursor_is_opaque_base64_encoding(self) -> None:
        """Encoded cursor is base64-encoded — internal field name not readable as plain text."""
        from weewx_clearskies_api.services.archive import encode_cursor

        cursor = encode_cursor(after_datetime=1778099700)
        # The cursor should be encoded so it doesn't contain the literal field name
        # in obvious plain text (it's base64, so the raw JSON won't appear)
        # This is a weak check — mainly verifies the cursor is NOT just plain JSON
        try:
            # If the cursor is raw JSON, this would parse fine and expose internals
            parsed = json.loads(cursor)
            # If we got here, the cursor is not base64-encoded
            assert False, (
                f"Cursor appears to be raw JSON (not opaque): {cursor!r}. "
                "Cursors must be base64-encoded to be opaque."
            )
        except (json.JSONDecodeError, ValueError):
            pass  # Good — cursor is encoded, not raw JSON
