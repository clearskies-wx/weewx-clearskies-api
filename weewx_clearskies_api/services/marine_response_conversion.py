"""SI -> operator-display-unit conversion for companion-proxied marine
responses (T6.2, API-MANUAL §19.3).

The marine service (``weewx-clearskies-marine``) emits canonical SI only —
meters, seconds, Celsius, meters per second, hPa, nautical miles — and no
envelope (verified by reading its ``models/responses.py`` module docstring
and every one of its eleven route handlers, none of which call a unit
converter). Per ARCHITECTURE.md "Layer Responsibilities" and API-MANUAL
§19.3, the API's companion proxy (``services/companion_proxy.py``) is the
single conversion authority. This module is that authority's
implementation, called from the proxy's one transform seam
(``_apply_response_transform()`` — do not add a second conversion call
site anywhere else).

**Why a generic, recursive, field-name-keyed converter instead of one
function per bundle shape.** ``services/companion_proxy.py`` is
deliberately generic (its own docstring: "Generic, not marine-specific in
principle") — it does not know the shape of any particular route's
payload, only that eleven routes exist per the manifest. Hand-writing one
converter per bundle (the pattern the soon-to-be-deleted
``endpoints/surf.py`` / ``endpoints/marine.py`` / etc. use today) would
require this module to know every route's exact schema and would break
silently the next time the marine service's manifest adds a field or a
route. Instead this module walks whatever JSON tree comes back and
converts every field whose *name* is a known physical-unit-bearing marine
field, regardless of nesting depth.

**Field -> unit-group inventory.** Built from three sources, not
guesswork:
  1. The marine service's ``models/responses.py`` field-level comments
     (every field there is explicitly tagged ``# group_X`` or explicitly
     left untagged for a documented physical-but-unconverted reason, e.g.
     "energy: zeroth spectral moment m0 (m^2)" has no group tag).
  2. API-MANUAL §16 "Marine Data Model" / §19.3.
  3. Cross-checked against the still-present (pre-T6.5-deletion) API
     endpoints (``endpoints/surf.py``, ``beach_profile.py``,
     ``beach_safety.py``, ``fishing.py``, ``tides.py``) which perform this
     exact conversion today against the same field names — the
     authoritative precedent for which fields convert and against which
     group, not an assumption.

One genuine field-name collision exists and is resolved by structural
context (the immediate parent container's key), not by guessing:
  - ``height``: means wave height (group_wave_height) inside
    ``spectralComponents`` / ``multiSwell`` lists (SpectralWaveComponent),
    but means water level (group_water_level) inside ``predictions`` /
    ``waterLevels`` / ``totalWaterLevelForecast`` lists (TidePrediction /
    WaterLevel). Elsewhere, an unqualified ``height`` field is left
    unconverted (never seen in the current 11-route inventory).

  **Retired 2026-08-05 (Round S, ADR-101 SurfScoringBreakdown reshape):** a
  second collision used to exist here — ``waveHeight``/``wavePeriod``/
  ``waveOrganization`` inside a ``scoring`` object were 0-35/0-30 SCORE
  POINTS, not physical quantities, suppressed via a dedicated
  ``key_context == "scoring"`` rule in ``_resolve_group()``. The reshape
  deletes all three field names from the scoring shape (replaced by
  ``size``/``shape``/``conditions``/``power``/``consistency``, each a
  unitless 0-100 int, plus a ``weights`` object of 0-1 floats) — none of
  the six new names appear anywhere in ``_FIELD_GROUPS``, so no collision
  is possible and the suppression rule/constants were deleted rather than
  left as dead code for a collision that can no longer occur.

**Deliberate extension beyond the pre-separation API's literal behaviour
(flagged, not hidden):** the old ``endpoints/surf.py`` converted
``breakPoints[].hs`` but left ``breakPoints[].distance`` /
``.depth`` raw, while ``endpoints/beach_profile.py`` converted the
*same-named* ``distance`` / ``depth`` fields (in its own transect/
breakPoints/waveShapes/jackingFactors output) via the operator's
``group_wave_height`` unit (its own ``_distance_unit()``: "return
(internal_unit_name, display_symbol) for cross-shore distances and
depths"). A route-agnostic, field-name-keyed converter cannot apply two
different rules to a field literally named ``distance`` depending on
which of the two old routes it came from without hardcoding per-route
knowledge back into the (deliberately generic) proxy. This module applies
beach_profile.py's rule uniformly to every occurrence of
``distance``/``depth`` (and same-family names ``distanceFromShore``,
``handoffDepthM``, ``meanBreakDistanceM``, ``meanBreakDepthM``) — meaning
surf.py's ``breakPoints[].distance``/``.depth`` now convert too, where the
old API left them raw. This is more complete (every numeric marine field
converts, per the task brief) and more internally consistent (the
dashboard no longer sees raw meters in one route's breakPoints and
converted feet in another's), but it is a behavior change from the old
API's literal output and is reported as such.

**Every field checked against the still-present (pre-T6.5) API endpoints'
live behaviour today, per lead directive (not left as an assumption) —
bucketed explicitly in the T6.2 closeout report as (i) has a group today
-> converted same group, (ii) genuinely dimensionless/count/score ->
correctly unconverted, or (iii) has a physical dimension but no group
anywhere in the codebase -> left unconverted and reported as a
pre-existing gap:**

  - ``SurfZones`` sub-dict fields (``impactZone``/``foamZone``/
    ``totalSurfZone``/``reformTrough`` -> ``startDistance``/
    ``endDistance``/``widthM``/``startDepth``/``endDepth``) ARE converted
    today (``endpoints/beach_profile.py``'s ``_serialize_surf_zones()`` /
    ``_convert_zone()`` converts exactly this key set via
    ``group_wave_height``) — bucket (i), added to ``_FIELD_GROUPS`` above.
  - ``breakingDissipation`` (W/m², SWAN CURVE TABLE DISSURF) — bucket
    (iii). Not converted by any live endpoint today (not even referenced
    by ``endpoints/marine.py``'s ``_convert_forecast_point()``, and not a
    field on ``SurfForecast`` at all). No W/m² group exists anywhere in
    ``units/groups.py``. Left unconverted; reported.
  - ``energy`` (m², SpectralWaveComponent zeroth spectral moment m0) —
    bucket (iii). ``endpoints/surf.py``'s ``multiSwell`` construction
    passes it through raw (``c.get("energy", 0.0)``, no ``_convert_unit``
    call). No m² group exists. Left unconverted; reported.
  - ``frequencyRange`` (Hz pair) — bucket (iii). Same construction site,
    same raw passthrough, no Hz group exists. Left unconverted; reported.
  - ``breakingFraction`` (QB, dimensionless 0.0-1.0) — bucket (ii),
    correctly unconverted (not a length/time/speed/temperature/pressure
    quantity).
  - Every ``SurfScoringBreakdown`` field (``size``/``shape``/``conditions``/
    ``power``/``consistency``/``weights``, reshaped 2026-08-05 — none of
    these names appear in ``_FIELD_GROUPS``, so they pass through
    unconverted with no context-based rule needed), ``peelAngle``,
    ``peelClassification``, ``peelDirection``, ``directionalSpread``,
    every ``*Direction*`` / ``*Bearing*`` field — bucket (ii):
    scores/classifications/degrees (single universal unit, no group).
  - ``currentResidual`` (``TideBundle``, ADR-091 composite) — structure
    not verified against ``services/water_level_compositor.py``; walked
    with the default (non-water-level) context. If it carries a bare
    ``height``/``value`` field, that field is NOT recognised by name here
    and is left unconverted with no warning (only the ambiguous ``height``
    case inside a *known* container logs — see below). Flagged for the
    lead to verify.

**Fail loud on the one genuinely ambiguous name.** ``height`` is looked up
by the immediate parent container's key (``key_context``) against the two
known-shape container sets above. When ``height`` appears under a
container key this module does not recognise, it is left unconverted AND
logged at WARNING with the dotted key path — never silently passed
through as if it had been resolved. Every other field name in
``_FIELD_GROUPS`` has exactly one meaning across all eleven routes
(verified during the field inventory above), so no equivalent warning
exists for them; adding a second field with a genuine same-name
different-meaning collision must add a matching container-based
disambiguation rule here, not a guess.
"""

from __future__ import annotations

import logging
from typing import Any

from weewx_clearskies_api.services.units import get_group_unit, get_target_unit
from weewx_clearskies_api.units.conversion import convert
from weewx_clearskies_api.units.labels import get_label

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Canonical SI base units the marine service emits, per group (verified
# against weewx-clearskies-marine/providers/buoy/ndbc.py's wire-format
# docstring and models/responses.py's field comments — not assumed).
# ---------------------------------------------------------------------------

_BASE_UNITS: dict[str, str] = {
    "group_wave_height": "meter",
    "group_wave_period": "second",
    "group_ocean_speed": "meter_per_second",
    "group_water_level": "meter",
    "group_visibility": "nautical_mile",
    "group_temperature": "degree_C",
    "group_pressure": "hPa",
}

# ---------------------------------------------------------------------------
# Flat field-name -> group table. Applies regardless of nesting depth
# EXCEPT for the one collision handled contextually below ("height").
# ---------------------------------------------------------------------------

_FIELD_GROUPS: dict[str, str] = {
    # --- group_wave_height (base meter) ---
    "waveHeight": "group_wave_height",
    "swellHeight": "group_wave_height",
    "windWaveHeight": "group_wave_height",
    "waveHeightAtBreak": "group_wave_height",
    "breakingFaceHeight": "group_wave_height",
    "breakingHawaiianHeight": "group_wave_height",
    "bestPeakFaceHeight": "group_wave_height",
    "spotAverageFaceHeight": "group_wave_height",
    # D10.2 (2026-08-04, marine 69d831a): secondary shadow-zone face height
    # on SurfForecast — same statistic family as the two fields above (mean
    # of best_face_height_m over structure-affected transects, SI meters at
    # the marine service). Without this entry the field would pass through
    # in raw meters while the dashboard labels it with the height unit.
    # (SurfForecast's new perPartitionBreaks needs NO entry: its sub-fields
    # — heightM/meanFaceHeightM/peakFaceHeightM/meanBreakDistanceM/
    # meanBreakDepthM/periodS — are already in this table via the
    # beach-profile route, and the walker converts by field name at any
    # nesting depth.)
    "shadowFaceHeight": "group_wave_height",
    "surfHeightMin": "group_wave_height",
    "surfHeightMax": "group_wave_height",
    # WC-D3: model-derived surf height range (distinct from NWS SRF above)
    "modelSurfHeightMin": "group_wave_height",
    "modelSurfHeightMax": "group_wave_height",
    "hs": "group_wave_height",
    "faceHeight": "group_wave_height",
    "setup": "group_wave_height",  # SWAN TABLE SETUP — converted via the
    # height group, matching the pre-separation endpoints/surf.py precedent
    # (`_convert_unit(setup, "meter", wave_height_internal)`), not a
    # separate water-level reading.
    "heightM": "group_wave_height",
    "meanFaceHeightM": "group_wave_height",
    "peakFaceHeightM": "group_wave_height",
    # Cross-shore geometry (beach_profile.py precedent — see module
    # docstring "Deliberate extension" note above).
    "distance": "group_wave_height",
    "depth": "group_wave_height",
    "distanceFromShore": "group_wave_height",
    "handoffDepthM": "group_wave_height",
    "meanBreakDistanceM": "group_wave_height",
    "meanBreakDepthM": "group_wave_height",
    # Round P (2026-08-04, marine 8c2def8): beach-profile per-transect
    # tide-aware waterline crossing (m) and raw signed-profile elevation
    # (m, = -depth_m, LMSL datum) — same cross-shore-geometry family as
    # distance/depth above, same group_wave_height precedent.
    # ("tideLevel" needs NO new entry here: it already has a
    # group_water_level entry below, pre-dating this round.)
    "waterlineDistance": "group_wave_height",
    "elevation": "group_wave_height",
    # SurfZones (1D analytical model) sub-dict fields — confirmed against
    # the still-present endpoints/beach_profile.py's _serialize_surf_zones()
    # / _convert_zone(), which converts exactly this key set via d_unit
    # (== group_wave_height's operator target), never left raw.
    "startDistance": "group_wave_height",
    "endDistance": "group_wave_height",
    "widthM": "group_wave_height",
    "startDepth": "group_wave_height",
    "endDepth": "group_wave_height",
    # Round Z (2026-08-05, marine perBreakZones contract addition, D6):
    # beach-profile perBreakZones[].breakDistance — same cross-shore
    # distance family as "distance"/"waterlineDistance" above, same
    # group_wave_height precedent. Its nested impactZone/foamZone
    # startDistance/endDistance/startDepth/endDepth fields need NO new
    # entry here: _walk() (below) recurses into any dict/list-of-dicts by
    # structure, matching leaf field names regardless of nesting depth or
    # container name, so they already convert via the startDistance/
    # endDistance/startDepth/endDepth entries above (verified by reading
    # _walk()/_convert_field()/_resolve_group(), not assumed — group
    # resolution is purely leaf-key-name-based, never path-based).
    "breakDistance": "group_wave_height",
    # --- group_wave_period (base second; single-unit group, always a
    # no-op conversion — included for uniformity, not because it ever
    # changes a value) ---
    "wavePeriod": "group_wave_period",
    "swellPeriod": "group_wave_period",
    "windWavePeriod": "group_wave_period",
    "dominantPeriod": "group_wave_period",
    "averagePeriod": "group_wave_period",
    "period": "group_wave_period",
    "periodS": "group_wave_period",
    # --- group_ocean_speed (base meter_per_second) ---
    "windSpeed": "group_ocean_speed",
    "windGust": "group_ocean_speed",
    # --- group_water_level (base meter) ---
    "tideLevel": "group_water_level",
    # C-44 (audit finding F1, 2026-07-25): totalWaterLevelForecast[] items
    # are {time, height, residual} (services/water_level_compositor.py,
    # weewx-clearskies-marine) — "residual" is the non-tidal (storm
    # surge/wind setup) component of the same total-water-level reading
    # "height" carries in that same object, always canonical meters, no
    # collision anywhere else in the marine response models (grep-verified:
    # every other "residual" occurrence is English prose, not a field key).
    # Unlike "height" it needs no contextual container check — a bare key
    # is correct here.
    "residual": "group_water_level",
    # "height" handled contextually — see _resolve_group().
    # --- group_visibility (base nautical_mile) ---
    "visibility": "group_visibility",
    # --- group_temperature (base degree_C) ---
    "airTemp": "group_temperature",
    "waterTemp": "group_temperature",
    "dewpoint": "group_temperature",
    # --- group_pressure (base hPa) ---
    "pressure": "group_pressure",
    "pressureTendency": "group_pressure",
}

# Containers whose *dict items'* "height" field means water level, not
# wave height (TidePrediction / WaterLevel — API-MANUAL §16).
_WATER_LEVEL_HEIGHT_CONTAINERS: frozenset[str] = frozenset(
    {"predictions", "waterLevels", "totalWaterLevelForecast"}
)

# Containers whose dict items' "height" field means wave height
# (SpectralWaveComponent — API-MANUAL §16).
_SPECTRAL_HEIGHT_CONTAINERS: frozenset[str] = frozenset({"spectralComponents", "multiSwell"})

# ---------------------------------------------------------------------------
# Display labels for marine-only target units (compact symbols, matching
# the convention the pre-separation endpoints/marine.py established for
# this exact set: "kt"/"nm"/"ft"/"m"/"s", not units/labels.py's verbose
# weewx-style " knots"/" mph"). Temperature and pressure target units
# reuse units/labels.py's get_label() (°F/°C/inHg/mbar/hPa/kPa — the same
# labels /current already shows the operator for those two land-shared
# groups).
# ---------------------------------------------------------------------------

_MARINE_UNIT_LABELS: dict[str, str] = {
    "foot": "ft",
    "meter": "m",
    "second": "s",
    "knot": "kt",
    "nautical_mile": "nm",
    "meter_per_second": "m/s",
    "mile_per_hour": "mph",
    "km_per_hour": "km/h",
    "statute_mile": "mi",
    "kilometer": "km",
}


def _unit_label(unit: str) -> str:
    if unit in _MARINE_UNIT_LABELS:
        return _MARINE_UNIT_LABELS[unit]
    label = get_label(unit)
    return label if label else unit


# ---------------------------------------------------------------------------
# Target unit resolution
# ---------------------------------------------------------------------------


def _resolve_targets() -> dict[str, str]:
    """Return group -> operator target unit for every group this module converts.

    Defaults mirror the pre-separation endpoints/marine.py's
    ``_marine_target_units()`` (foot/meter for height+water-level by
    system, "knot" and "nautical_mile" constant regardless of system) plus
    the standard weewx group_temperature/group_pressure system presets
    (units/groups.py's US_UNITS/METRIC_UNITS/METRICWX_UNITS), all resolved
    through the same get_group_unit() operator-override lookup every other
    response uses — never a hardcoded factor.
    """
    is_us = get_target_unit() == "US"
    height_default = "foot" if is_us else "meter"
    return {
        "group_wave_height": get_group_unit("group_wave_height", height_default),
        "group_wave_period": get_group_unit("group_wave_period", "second"),
        "group_ocean_speed": get_group_unit("group_ocean_speed", "knot"),
        "group_water_level": get_group_unit("group_water_level", height_default),
        "group_visibility": get_group_unit("group_visibility", "nautical_mile"),
        "group_temperature": get_group_unit(
            "group_temperature", "degree_F" if is_us else "degree_C"
        ),
        "group_pressure": get_group_unit("group_pressure", "inHg" if is_us else "mbar"),
    }


# ---------------------------------------------------------------------------
# Recursive conversion
# ---------------------------------------------------------------------------


def _resolve_group(key: str, key_context: str | None, path: str) -> str | None:
    if key == "height":
        if key_context in _WATER_LEVEL_HEIGHT_CONTAINERS:
            return "group_water_level"
        if key_context in _SPECTRAL_HEIGHT_CONTAINERS:
            return "group_wave_height"
        # Ambiguous name, unrecognised context: fail loud rather than
        # silently guess a group (module docstring "Fail loud" note).
        logger.warning(
            "marine_response_conversion: unrecognised container %r for "
            "ambiguous field 'height' at %s -- left unconverted",
            key_context,
            path,
        )
        return None
    return _FIELD_GROUPS.get(key)


def _convert_field(
    key: str,
    value: Any,
    key_context: str | None,
    path: str,
    targets: dict[str, str],
    units_block: dict[str, str],
) -> Any:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return value
    group = _resolve_group(key, key_context, path)
    if group is None:
        return value
    base_unit = _BASE_UNITS[group]
    target_unit = targets[group]
    units_block[key] = _unit_label(target_unit)
    return convert(float(value), base_unit, target_unit)


def _walk(
    node: Any,
    key_context: str | None,
    path: str,
    targets: dict[str, str],
    units_block: dict[str, str],
) -> Any:
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for key, value in node.items():
            child_path = f"{path}.{key}" if path else key
            if isinstance(value, dict):
                out[key] = _walk(value, key, child_path, targets, units_block)
            elif isinstance(value, list):
                out[key] = [
                    _walk(item, key, f"{child_path}[{idx}]", targets, units_block)
                    if isinstance(item, dict)
                    else item
                    for idx, item in enumerate(value)
                ]
            else:
                out[key] = _convert_field(key, value, key_context, child_path, targets, units_block)
        return out
    if isinstance(node, list):
        return [
            _walk(item, key_context, f"{path}[{idx}]", targets, units_block)
            if isinstance(item, dict)
            else item
            for idx, item in enumerate(node)
        ]
    return node


def convert_marine_payload(body: Any) -> tuple[Any, dict[str, str]]:
    """Convert every known-group numeric field in *body* from marine-service
    SI to the operator's configured display unit.

    Walks dicts and lists-of-dicts recursively regardless of depth; scalars
    and lists-of-scalars are returned unchanged except for direct numeric
    fields matched against the group table. ``None`` values (every marine
    field is nullable — SURF-PUBLISH-RESULTS-ONLY §3.6) pass through
    unchanged; this function never raises on a null-payload body.

    Returns ``(converted_body, units_block)`` where ``units_block`` is a
    flat ``{fieldName: displayLabel}`` map covering every field name this
    call actually converted (list routes and different bundle shapes carry
    different field sets, so the block is built dynamically rather than a
    fixed per-route table — matching the fact this proxy has no
    per-route schema knowledge).
    """
    targets = _resolve_targets()
    units_block: dict[str, str] = {}
    converted = _walk(body, None, "", targets, units_block)
    return converted, units_block
