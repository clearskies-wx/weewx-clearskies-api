"""HTTP client for AstronomyAPI.com — eclipse contact times + local visibility.

Fetches lunar and solar eclipse events with contact times and body altitudes
from the AstronomyAPI.com Events endpoint.

Endpoints used:
    GET /api/v2/bodies/events/moon  (lunar eclipses)
    GET /api/v2/bodies/events/sun   (solar eclipses)

Auth: HTTP Basic (app_id:app_secret via httpx.BasicAuth).

Error handling strategy:
    All errors return an empty list rather than raising.  AstronomyAPI is
    an optional enrichment provider (contact times + local visibility).
    A missing response degrades gracefully — the cache warmer skips the
    enrichment; callers never see a 5xx from this client's failures.

No caching in this client.  Caching is handled by the cache_warmer layer.

Max date-range: 366 days per request.  Ranges longer than 366 days are split
into ≤366-day sub-requests and results concatenated.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.astronomyapi.com/api/v2"
_MAX_RANGE_DAYS = 366

# Contact-time field names for each body.
_LUNAR_CONTACT_FIELDS = (
    "penumbralStart",
    "partialStart",
    "fullStart",
    "peak",
    "fullEnd",
    "partialEnd",
    "penumbralEnd",
)

_SOLAR_CONTACT_FIELDS = (
    "partialStart",
    "totalStart",
    "peak",
    "totalEnd",
    "partialEnd",
)


class AstronomyApiClient:
    """HTTP client for AstronomyAPI.com — eclipse contact times + local visibility.

    Usage:
        client = AstronomyApiClient(app_id="...", app_secret="...")
        events = client.get_lunar_eclipses(lat, lon, elev, from_date, to_date)
        client.close()

    Or as a context manager:
        with AstronomyApiClient(app_id="...", app_secret="...") as client:
            events = client.get_lunar_eclipses(lat, lon, elev, from_date, to_date)
    """

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        timeout_seconds: int = 10,
    ) -> None:
        """Initialise the client with HTTP Basic Auth credentials.

        Args:
            app_id: AstronomyAPI application ID.
            app_secret: AstronomyAPI application secret.
            timeout_seconds: Request timeout in seconds (default 10).

        Credentials are stored only inside the httpx.BasicAuth object and are
        never written to any log message.
        """
        self._timeout_seconds = timeout_seconds
        self._client = httpx.Client(
            auth=httpx.BasicAuth(app_id, app_secret),
            timeout=httpx.Timeout(float(timeout_seconds)),
            verify=True,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_lunar_eclipses(
        self,
        lat: float,
        lon: float,
        elevation: float,
        from_date: date,
        to_date: date,
    ) -> list[dict]:  # type: ignore[type-arg]
        """Fetch lunar eclipse events with contact times and body altitudes.

        Args:
            lat: Observer latitude in decimal degrees.
            lon: Observer longitude in decimal degrees.
            elevation: Observer elevation in metres above sea level.
            from_date: Start of search window (inclusive).
            to_date: End of search window (inclusive).

        Returns:
            List of parsed eclipse dicts.  Returns an empty list on any error.
        """
        return self._fetch_eclipse_events(
            body="moon",
            contact_fields=_LUNAR_CONTACT_FIELDS,
            lat=lat,
            lon=lon,
            elevation=elevation,
            from_date=from_date,
            to_date=to_date,
        )

    def get_solar_eclipses(
        self,
        lat: float,
        lon: float,
        elevation: float,
        from_date: date,
        to_date: date,
    ) -> list[dict]:  # type: ignore[type-arg]
        """Fetch solar eclipse events with contact times and body altitudes.

        Args:
            lat: Observer latitude in decimal degrees.
            lon: Observer longitude in decimal degrees.
            elevation: Observer elevation in metres above sea level.
            from_date: Start of search window (inclusive).
            to_date: End of search window (inclusive).

        Returns:
            List of parsed eclipse dicts.  Returns an empty list on any error.
        """
        return self._fetch_eclipse_events(
            body="sun",
            contact_fields=_SOLAR_CONTACT_FIELDS,
            lat=lat,
            lon=lon,
            elevation=elevation,
            from_date=from_date,
            to_date=to_date,
        )

    def close(self) -> None:
        """Release the underlying httpx.Client."""
        self._client.close()

    # ------------------------------------------------------------------
    # Context-manager support
    # ------------------------------------------------------------------

    def __enter__(self) -> AstronomyApiClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _fetch_eclipse_events(
        self,
        body: str,
        contact_fields: tuple[str, ...],
        lat: float,
        lon: float,
        elevation: float,
        from_date: date,
        to_date: date,
    ) -> list[dict]:  # type: ignore[type-arg]
        """Fetch and parse eclipse events for a given body, splitting long ranges.

        Ranges longer than _MAX_RANGE_DAYS (366) are split into ≤366-day
        sub-requests.  Results from each sub-request are concatenated.

        Args:
            body: "moon" or "sun".
            contact_fields: Ordered tuple of eventHighlights field names to parse.
            lat, lon, elevation: Observer location.
            from_date, to_date: Date range (inclusive).

        Returns:
            Concatenated list of parsed eclipse dicts, or [] on any error.
        """
        results: list[dict] = []  # type: ignore[type-arg]

        # Split the range into ≤366-day chunks.
        chunk_start = from_date
        while chunk_start <= to_date:
            chunk_end = min(
                chunk_start + timedelta(days=_MAX_RANGE_DAYS - 1),
                to_date,
            )
            chunk_results = self._fetch_chunk(
                body=body,
                contact_fields=contact_fields,
                lat=lat,
                lon=lon,
                elevation=elevation,
                from_date=chunk_start,
                to_date=chunk_end,
            )
            results.extend(chunk_results)
            chunk_start = chunk_end + timedelta(days=1)

        return results

    def _fetch_chunk(
        self,
        body: str,
        contact_fields: tuple[str, ...],
        lat: float,
        lon: float,
        elevation: float,
        from_date: date,
        to_date: date,
    ) -> list[dict]:  # type: ignore[type-arg]
        """Fetch one ≤366-day chunk from the AstronomyAPI Events endpoint.

        Returns [] on timeout, HTTP error, or malformed JSON.  Logs a warning
        for each failure.  Never raises.
        """
        url = f"{_BASE_URL}/bodies/events/{body}"
        params = {
            "latitude": str(lat),
            "longitude": str(lon),
            "elevation": str(elevation),
            "from_date": from_date.strftime("%Y-%m-%d"),
            "to_date": to_date.strftime("%Y-%m-%d"),
            "time": "00:00:00",
        }

        try:
            response = self._client.get(url, params=params)
            response.raise_for_status()
        except httpx.TimeoutException:
            logger.warning(
                "AstronomyAPI request timed out after %s s (body=%s, from=%s, to=%s)",
                self._timeout_seconds,
                body,
                from_date,
                to_date,
            )
            return []
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "AstronomyAPI returned HTTP %s (body=%s, from=%s, to=%s)",
                exc.response.status_code,
                body,
                from_date,
                to_date,
            )
            return []
        except httpx.HTTPError as exc:
            # Catches ConnectError, RemoteProtocolError, etc.
            logger.warning(
                "AstronomyAPI HTTP error (body=%s, from=%s, to=%s): %s",
                body,
                from_date,
                to_date,
                exc,
            )
            return []

        try:
            payload: dict[str, Any] = response.json()
        except Exception:  # noqa: BLE001
            logger.warning(
                "AstronomyAPI returned non-JSON response (body=%s, from=%s, to=%s)",
                body,
                from_date,
                to_date,
            )
            return []

        return self._parse_payload(payload, body, contact_fields, from_date, to_date)

    def _parse_payload(
        self,
        payload: dict[str, Any],
        body: str,
        contact_fields: tuple[str, ...],
        from_date: date,
        to_date: date,
    ) -> list[dict]:  # type: ignore[type-arg]
        """Parse the AstronomyAPI Events JSON response.

        Actual API structure (verified 2026-06-04):
            {"data": {"table": {"rows": [{"entry": {...}, "cells": [{...}, ...]}]}}}

        Each cell IS an event (type + eventHighlights + extraInfo directly).

        Skips individual events that fail to parse rather than aborting the
        whole response.

        Args:
            payload: Decoded JSON dict from the API.
            body: "moon" or "sun" (for log messages).
            contact_fields: Ordered tuple of eventHighlights keys to extract.
            from_date, to_date: Date range for log context.

        Returns:
            List of parsed eclipse dicts.
        """
        try:
            rows = payload["data"]["table"]["rows"]
        except (KeyError, TypeError):
            logger.warning(
                "AstronomyAPI response missing data.table.rows (body=%s, from=%s, to=%s)",
                body,
                from_date,
                to_date,
            )
            return []

        if not isinstance(rows, list):
            logger.warning(
                "AstronomyAPI data.table.rows is not a list (body=%s, from=%s, to=%s)",
                body,
                from_date,
                to_date,
            )
            return []

        results: list[dict] = []  # type: ignore[type-arg]
        for row in rows:
            cells = row.get("cells") if isinstance(row, dict) else None
            if not isinstance(cells, list):
                continue
            for cell in cells:
                parsed = self._parse_event(cell, contact_fields, body, from_date, to_date)
                if parsed is not None:
                    results.append(parsed)

        return results

    def _parse_event(
        self,
        event: Any,
        contact_fields: tuple[str, ...],
        body: str,
        from_date: date,
        to_date: date,
    ) -> dict | None:  # type: ignore[type-arg]
        """Parse one eclipse event dict from the API response.

        Returns None (and logs a warning) on any parse error.

        Output shape:
            {
                "type": str,               # e.g. "total_lunar_eclipse"
                "date": str,               # "YYYY-MM-DD" from peak.date
                "contactTimes": {
                    "<field>": {"date": str, "altitude": float} | None,
                    ...
                },
                "obscuration": float | None,
            }
        """
        if not isinstance(event, dict):
            logger.warning(
                "AstronomyAPI event is not a dict (body=%s, from=%s, to=%s)",
                body,
                from_date,
                to_date,
            )
            return None

        try:
            event_type = str(event["type"])
        except (KeyError, TypeError) as exc:
            logger.warning(
                "AstronomyAPI event missing 'type' (body=%s, from=%s, to=%s): %s",
                body,
                from_date,
                to_date,
                exc,
            )
            return None

        highlights = event.get("eventHighlights")
        if not isinstance(highlights, dict):
            logger.warning(
                "AstronomyAPI event missing eventHighlights (type=%s, body=%s)",
                event_type,
                body,
            )
            return None

        # Extract the peak contact for the date field (peak is always required).
        peak_raw = highlights.get("peak")
        if not isinstance(peak_raw, dict):
            logger.warning(
                "AstronomyAPI event missing peak contact (type=%s, body=%s)",
                event_type,
                body,
            )
            return None

        peak_date_iso = peak_raw.get("date")
        if not isinstance(peak_date_iso, str) or not peak_date_iso:
            logger.warning(
                "AstronomyAPI peak.date is missing or not a string (type=%s, body=%s)",
                event_type,
                body,
            )
            return None

        # Extract "YYYY-MM-DD" from the peak ISO-8601 datetime string.
        # The API returns strings like "2026-09-07T18:44:10.000Z".
        date_str = peak_date_iso[:10]

        # Build contactTimes dict — None for absent/null fields.
        contact_times: dict[str, dict | None] = {}
        for field in contact_fields:
            raw = highlights.get(field)
            if raw is None:
                contact_times[field] = None
            elif isinstance(raw, dict):
                contact_date = raw.get("date")
                contact_alt = raw.get("altitude")
                if isinstance(contact_date, str) and contact_alt is not None:
                    try:
                        contact_times[field] = {
                            "date": contact_date,
                            "altitude": float(contact_alt),
                        }
                    except (TypeError, ValueError):
                        contact_times[field] = None
                else:
                    contact_times[field] = None
            else:
                contact_times[field] = None

        # obscuration from extraInfo (optional).
        obscuration: float | None = None
        extra_info = event.get("extraInfo")
        if isinstance(extra_info, dict):
            raw_obs = extra_info.get("obscuration")
            if raw_obs is not None:
                try:
                    obscuration = float(raw_obs)
                except (TypeError, ValueError):
                    obscuration = None

        return {
            "type": event_type,
            "date": date_str,
            "contactTimes": contact_times,
            "obscuration": obscuration,
        }
