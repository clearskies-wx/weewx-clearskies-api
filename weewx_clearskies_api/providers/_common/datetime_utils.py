"""Shared datetime utilities for provider modules (ADR-020).

Lifted from providers/alerts/nws.py per rules/coding.md §3 DRY rule
("search before writing a new helper — fix/extend the existing version
rather than forking a near-duplicate").

Function originally lived at providers/alerts/nws.py L275-299; moved here
so providers/forecast/nws.py can import it from the start without duplicating
logic.  Behavior is unchanged; only location differs.

Commit context: required before forecast/nws.py lands so the new module
imports from the shared location rather than duplicating.

epoch_to_utc_iso8601 added for providers/forecast/openweathermap.py (3b-5):
  OWM emits epoch UTC seconds for dt/sunrise/sunset fields rather than
  offset-aware ISO strings, so to_utc_iso8601_from_offset does NOT apply.
  The sibling helper converts epoch seconds → UTC ISO-8601 Z form (ADR-020).
  Per rules/coding.md §3 DRY rule, searched for an existing epoch-to-ISO
  helper before adding; none existed in _common/ as of ab3294a.
"""

from __future__ import annotations

from datetime import UTC, datetime

from weewx_clearskies_api.providers._common.errors import ProviderProtocolError


def to_utc_iso8601_from_offset(s: str, *, provider_id: str, domain: str) -> str:
    """Convert a provider timestamp (ISO-8601 with UTC offset) to UTC ISO-8601 Z.

    NWS (and other providers that emit offset-aware timestamps) always include
    a timezone offset (e.g. ``"2026-04-30T16:00:00-07:00"``).  ADR-020 mandates
    UTC ISO-8601 with an explicit ``Z`` suffix on the wire.

    Args:
        s: ISO-8601 timestamp string with a UTC offset, e.g.
           ``"2026-04-30T16:00:00-07:00"``.
        provider_id: Provider identifier (e.g. ``"nws"``); included in any
            error raised for context.
        domain: Provider domain (e.g. ``"alerts"`` or ``"forecast"``); included
            in any error raised for context.

    Returns:
        UTC ISO-8601 string with Z suffix, e.g. ``"2026-04-30T23:00:00Z"``.

    Raises:
        ProviderProtocolError: The timestamp cannot be parsed, or it carries
            no timezone offset (bare-naive timestamps are a protocol violation
            for NWS; a bare-naive value here indicates a provider schema change).
    """
    try:
        dt = datetime.fromisoformat(s)
    except ValueError as exc:
        raise ProviderProtocolError(
            f"Timestamp parse failed for {s!r}: {exc}",
            provider_id=provider_id,
            domain=domain,
        ) from exc
    if dt.tzinfo is None:
        # Provider always emits offset; bare-naive is a protocol violation.
        raise ProviderProtocolError(
            f"Timestamp {s!r} has no timezone offset",
            provider_id=provider_id,
            domain=domain,
        )
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def epoch_to_utc_iso8601(
    epoch_seconds: int | float,
    *,
    provider_id: str,
    domain: str,
) -> str:
    """Convert epoch UTC seconds to ISO-8601 Z form (ADR-020).

    OWM One Call 3.0 (and other providers that emit Unix timestamps) use epoch
    UTC seconds for ``dt``, ``sunrise``, ``sunset``.  ADR-020 mandates UTC
    ISO-8601 with an explicit ``Z`` suffix on the wire.

    This helper is the sibling of ``to_utc_iso8601_from_offset`` — both live
    here per rules/coding.md §3 DRY rule.  Added for
    providers/forecast/openweathermap.py (3b-5 round).

    Args:
        epoch_seconds: Unix timestamp (UTC seconds since 1970-01-01T00:00:00Z).
            Accepts int or float; sub-second precision is discarded in the
            output format (``%S`` not ``%f``).
        provider_id: Provider identifier (e.g. ``"openweathermap"``); included
            in any error raised for context.
        domain: Provider domain (e.g. ``"forecast"``); included in any error
            raised for context.

    Returns:
        UTC ISO-8601 string with Z suffix, e.g. ``"2026-04-30T23:00:00Z"``.

    Raises:
        ProviderProtocolError: ``epoch_seconds`` is out of platform range,
            non-numeric, or otherwise unparsable (e.g. ``None``, string,
            infinity, NaN).
    """
    try:
        dt = datetime.fromtimestamp(epoch_seconds, tz=UTC)
    except (OverflowError, ValueError, OSError, TypeError) as exc:
        raise ProviderProtocolError(
            f"Epoch parse failed for {epoch_seconds!r}: {exc}",
            provider_id=provider_id,
            domain=domain,
        ) from exc
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
