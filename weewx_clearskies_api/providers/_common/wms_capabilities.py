"""WMS GetCapabilities XML parser for the radar domain (3b-14).

Extracts the TIME dimension from a WMS 1.3.0 GetCapabilities response for a
named layer.  Used by the four WMS-T radar providers (iem_nexrad, noaa_mrms,
msc_geomet, dwd_radolan) to obtain the list of available radar frames.

Security: uses defusedxml instead of stdlib xml.etree.ElementTree.
  Stdlib XML is vulnerable to billion-laughs, external-entity-expansion, and
  quadratic-blowup attacks on untrusted input (coding.md §1 "Avoid dangerous
  functions").  defusedxml is a pure-Python MIT drop-in that disables all
  these attack vectors.  Lead call 1 in 3b-14 brief.

WMS TIME dimension formats (WMS 1.3.0 spec §C.4):
  1. Comma-separated list:  "T1,T2,T3,..."
  2. Period notation:       "start/end/resolution"  (e.g. "2026-05-11T00:00:00Z/2026-05-11T04:00:00Z/PT5M")
  3. Mixed:                 "T1,start/end/res,T2"   (uncommon but valid)

For radar frame-index purposes we resolve periods to individual timestamps.
Period resolution is bounded: we stop at _MAX_PERIOD_FRAMES frames (300) to
avoid runaway expansion from a malformed or unexpectedly large TIME dimension.

Namespace handling:
  WMS GetCapabilities uses a default namespace that varies by server
  (typically "http://www.opengis.net/wms"). We use ElementTree's
  namespace-aware XPath with both common forms to be robust.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta

import defusedxml.ElementTree as ET  # type: ignore[import-untyped]

from weewx_clearskies_api.providers._common.errors import ProviderProtocolError

logger = logging.getLogger(__name__)

# Maximum number of expanded period frames.  Protects against runaway expansion
# from a malformed or unexpectedly-large TIME dimension.
_MAX_PERIOD_FRAMES = 300

# WMS 1.3.0 namespace (most servers use this; some omit the namespace).
_WMS_NS = "http://www.opengis.net/wms"

# ISO 8601 duration pattern — accepts P[n]Y[n]M[n]DT[n]H[n]M[n]S variants.
# Used to detect period notation "start/end/period".
_ISO_DURATION_RE = re.compile(
    r"^P(?:\d+Y)?(?:\d+M)?(?:\d+D)?(?:T(?:\d+H)?(?:\d+M)?(?:\d+(?:\.\d+)?S)?)?$",
    re.ASCII,
)


def _parse_iso_duration(period: str) -> timedelta | None:
    """Parse an ISO 8601 duration string to a timedelta.

    Handles: PT5M, PT10M, PT1H, P1DT, P1D, etc.
    Returns None if the period is not parseable (caller will raise ProtocolError).

    Note: This is a simple parser for radar-relevant durations (minutes, hours, days).
    It does not handle fractional years or months because those are ambiguous
    without a reference date.  WMS radar servers emit minute/hour cadences.
    """
    match = re.fullmatch(
        r"P(?:(\d+)Y)?(?:(\d+)M)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?",
        period,
    )
    if match is None:
        return None

    years = int(match.group(1) or 0)
    months = int(match.group(2) or 0)
    days = int(match.group(3) or 0)
    hours = int(match.group(4) or 0)
    minutes = int(match.group(5) or 0)
    seconds = float(match.group(6) or 0)

    # Approximate years + months as days (sufficient for radar frame-index purposes
    # since real radar servers never emit year/month cadences).
    approx_days = days + years * 365 + months * 30
    return timedelta(days=approx_days, hours=hours, minutes=minutes, seconds=seconds)


def _expand_period(start_iso: str, end_iso: str, period_iso: str) -> list[str]:
    """Expand a WMS period notation to a list of ISO-8601 timestamps.

    Args:
        start_iso: Start timestamp (ISO-8601 UTC).
        end_iso: End timestamp (ISO-8601 UTC).
        period_iso: ISO 8601 duration string (e.g. "PT5M").

    Returns:
        List of ISO-8601 UTC timestamps at period intervals, inclusive of
        start and up to (but not past) end. Capped at _MAX_PERIOD_FRAMES.

    Raises:
        ValueError: If any timestamp or duration cannot be parsed.
    """
    start = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
    end = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
    delta = _parse_iso_duration(period_iso)
    if delta is None or delta.total_seconds() <= 0:
        raise ValueError(f"Cannot parse or zero-duration period: {period_iso!r}")

    timestamps: list[str] = []
    current = start
    while current <= end and len(timestamps) < _MAX_PERIOD_FRAMES:
        timestamps.append(current.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"))
        current = current + delta
    return timestamps


def _parse_time_values(raw_value: str) -> list[str]:
    """Parse a raw TIME dimension value string into a list of timestamps.

    Handles comma-separated lists, period notation, and mixtures.

    Args:
        raw_value: Raw text content of the WMS TIME dimension element.

    Returns:
        Flattened list of ISO-8601 UTC timestamp strings.

    Raises:
        ValueError: If any component cannot be parsed.
    """
    timestamps: list[str] = []
    for part in raw_value.split(","):
        part = part.strip()
        if not part:
            continue
        if "/" in part:
            # Period notation: "start/end/duration"
            components = part.split("/")
            if len(components) != 3:
                raise ValueError(f"Invalid period notation (expected 3 components): {part!r}")
            start, end, period = components
            timestamps.extend(_expand_period(start.strip(), end.strip(), period.strip()))
        else:
            # Individual timestamp — normalise to UTC Z form.
            ts = datetime.fromisoformat(part.replace("Z", "+00:00"))
            timestamps.append(ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"))
    return timestamps


def _find_layer_recursive(
    root: ET.Element,  # type: ignore[type-arg]
    target_name: str,
    ns: str,
) -> ET.Element | None:  # type: ignore[type-arg]
    """Recursively find a WMS <Layer> element whose <Name> matches target_name.

    WMS GetCapabilities nests layers inside a root <Layer> container element.
    A layer's name lives in a child <Name> element.

    Args:
        root: Element tree root or sub-element to search.
        target_name: Layer name to match.
        ns: XML namespace prefix (e.g. "http://www.opengis.net/wms").

    Returns:
        The matching <Layer> element or None.
    """
    ns_prefix = f"{{{ns}}}" if ns else ""
    layer_tag = f"{ns_prefix}Layer"
    name_tag = f"{ns_prefix}Name"

    for layer in root.iter(layer_tag):
        name_el = layer.find(name_tag)
        if name_el is not None and name_el.text and name_el.text.strip() == target_name:
            return layer
    return None


def parse_wms_time_dimension(xml_bytes: bytes, *, layer: str, provider_id: str, domain: str) -> list[str]:
    """Extract TIME dimension values from a WMS GetCapabilities response.

    Parses the raw bytes of a WMS 1.3.0 GetCapabilities XML document, locates
    the named layer, finds its ``<Dimension name="time">`` element, and returns
    the expanded list of ISO-8601 UTC timestamps.

    Args:
        xml_bytes: Raw GetCapabilities response body (bytes).
        layer: Layer name to find within the capabilities tree (e.g.
            "nexrad-n0q-wmst", "RADAR_1KM_RDPR").
        provider_id: Provider identifier for error context.
        domain: Provider domain for error context.

    Returns:
        List of ISO-8601 UTC timestamps (sorted ascending) from the layer's
        ``<Dimension name="time">`` element.  Both comma-separated lists and
        ISO start/end/period notation are expanded to individual timestamps.

    Raises:
        ProviderProtocolError: XML is malformed; the named layer is not found
            in the capabilities tree; the layer has no ``<Dimension name="time">``
            element; or the dimension value cannot be parsed as timestamps.
    """
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise ProviderProtocolError(
            f"WMS GetCapabilities XML parse error: {exc}",
            provider_id=provider_id,
            domain=domain,
        ) from exc

    # Determine the namespace — WMS 1.3.0 uses "http://www.opengis.net/wms"
    # but some servers (e.g. ArcGIS) omit the namespace entirely.
    # Try namespaced first; fall back to no-namespace.
    target_layer_el = _find_layer_recursive(root, layer, _WMS_NS)
    if target_layer_el is None:
        target_layer_el = _find_layer_recursive(root, layer, "")

    if target_layer_el is None:
        raise ProviderProtocolError(
            f"WMS GetCapabilities: layer {layer!r} not found in capabilities tree. "
            f"Provider may have changed layer names or endpoint structure.",
            provider_id=provider_id,
            domain=domain,
        )

    # Find <Dimension name="time"> (case-insensitive on the name attribute).
    ns_prefix = f"{{{_WMS_NS}}}"
    dimension_el: ET.Element | None = None  # type: ignore[type-arg]

    # Search namespaced first, then no-namespace.
    for tag_prefix in (ns_prefix, ""):
        for dim in target_layer_el.iter(f"{tag_prefix}Dimension"):
            name_attr = dim.get("name", "")
            if name_attr.lower() == "time":
                dimension_el = dim
                break
        if dimension_el is not None:
            break

    if dimension_el is None:
        raise ProviderProtocolError(
            f"WMS GetCapabilities: layer {layer!r} has no TIME dimension. "
            "Provider may not support time-stepped radar.",
            provider_id=provider_id,
            domain=domain,
        )

    raw_value = (dimension_el.text or "").strip()
    if not raw_value:
        raise ProviderProtocolError(
            f"WMS GetCapabilities: layer {layer!r} TIME dimension is empty.",
            provider_id=provider_id,
            domain=domain,
        )

    try:
        timestamps = _parse_time_values(raw_value)
    except ValueError as exc:
        raise ProviderProtocolError(
            f"WMS GetCapabilities: TIME dimension parse failed for layer {layer!r}: {exc}. "
            f"Raw value (first 500 chars): {raw_value[:500]!r}",
            provider_id=provider_id,
            domain=domain,
        ) from exc

    if not timestamps:
        raise ProviderProtocolError(
            f"WMS GetCapabilities: TIME dimension for layer {layer!r} expanded to zero timestamps.",
            provider_id=provider_id,
            domain=domain,
        )

    logger.debug(
        "WMS GetCapabilities: layer %r has %d TIME dimension timestamp(s)",
        layer,
        len(timestamps),
    )
    return timestamps
