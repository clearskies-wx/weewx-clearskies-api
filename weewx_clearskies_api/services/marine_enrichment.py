"""Post-conversion enrichment for companion-proxied marine responses
(MARINE-SEP-CONCERNS.md C-24 / C-25 / C-29 / C-37, Phase 6 re-merge round).

Called exclusively from ``services/companion_proxy.py``'s
``_apply_post_conversion_enrichment()`` seam -- AFTER
``services/marine_response_conversion.py`` has converted the marine
service's SI payload to the operator's display units, BEFORE envelope
wrapping. Order is not negotiable: the composition below consumes
already-converted display-unit numbers, exactly as
``enrichment/surf_scorer.py``'s ``_compose_conditions_text()`` did before
the marine service separation.

Scope, one function group per concern:

  C-24 -- station-hardware observations. The marine service has no weewx
    archive (co-location with weewx is the reason the API has one --
    ADR-034), so it always answers from cache/forecast. Where a location is
    within ``dedup_radius_km`` of the station
    (``services/marine_location_resolver.is_station_served()``, unchanged,
    populated at startup from the same config this module reads), this
    module overwrites the wind/temperature/pressure fields with the
    operator's own instrument reading -- exactly the merge
    ``endpoints/marine.py`` performed before T6.5 deletes it. Values are
    passed through UNCONVERTED, matching that endpoint's own documented
    interim behavior ("no marine-group conversion applied here") -- this is
    a faithful restoration, not a place to fix that quirk.

    **HELD, not implemented here (coordinator ruling, 2026-07-25):** surf
    t=0 windSource/windQuality/scoring.organizationWind station
    restoration. Recomputing score_surf()'s composite formula in the API
    would duplicate a scoring implementation across two services (trigger
    1/5); patching only the wind-derived fields produces a
    self-contradictory response (windSource="station" beside a
    waveOrganization still computed from forecast wind). Escalated to the
    operator. Surf entries are NOT touched by this module today -- t=0
    windSource legitimately stays "forecast_provider" until that ruling
    lands. Do not add a station-injection call site here without new
    direction.

  C-25 -- active alerts. Alerts never move to the marine service (plan
    T1.1 item 7, QC Gate 1, ARCHITECTURE.md:140 "unconditional"). Restored
    from ``providers/alerts/nws.py`` on ``/marine`` list
    (``MarineLocationSummary.activeAlerts``) and ``/beach-safety/{id}``
    detail (``assessment.activeAlerts``) -- the only two places that ever
    carried alerts pre-separation.

  C-29 -- locale resolution + sentence composition. The marine service
    performs all scoring/classification (which locale KEY applies, e.g.
    "surf.quality.4"); this module only resolves keys to strings via
    ``i18n.t()`` and assembles sentences from already-converted
    display-unit ingredients via the relocated ``_compose_conditions_text()``
    helpers below (essentially unchanged from ``enrichment/surf_scorer.py``
    / ``enrichment/fishing_scorer.py``, which T6.7 deletes). No scoring or
    classification logic is reproduced here -- only presentation.

  C-37 -- ``currentResidual``. The marine service emits
    ``{valueM, quality, source}`` (canonical meters, no description). This
    module converts ``valueM`` to the operator's display unit and composes
    ``description`` in the pre-separation format
    (``"+0.23 ft vs prediction"``).

**Wire shape, confirmed against the landed marine service** (commit
``cc2be6a``, ``weewx-clearskies-marine`` -- supersedes this module's
original placeholder field-name guesses, kept out of the diff history only
because the module was rewritten in place rather than patched twice):

  - Any dict carrying ``qualityKey`` and/or ``windQualityKey`` is a surf
    forecast entry (``SurfForecast``-shaped). ``qualityKey`` is ``None`` in
    the unavailable case; ``windQualityKey`` is always populated (wind is a
    real observation, independent of whether a face height exists). Both
    resolve to ``qualityLabel`` / ``windQuality`` -- the outward names the
    dashboard has always seen -- and are always written (even to ``None``),
    never left absent.
  - ``conditionsTextParts`` (surf): ``{"unavailable": bool, "heightM":
    float|None, "periodS": float|None, "directionDeg": float|None,
    "windSpeedMps": float|None, "compass": str|None, "swellSummaryKey":
    str|None}``, all fields null except ``unavailable`` in the unavailable
    case. ``heightM``/``periodS``/``windSpeedMps`` are raw SI -- these field
    names are NOT in marine_response_conversion.py's ``_FIELD_GROUPS``
    table, so the T6.2 conversion step never touches them; this module
    converts ``heightM``/``windSpeedMps`` itself (``periodS`` needs no
    conversion -- single-unit group). ``directionDeg`` is unused (``compass``
    is already resolved). ``swellSummaryKey`` is an already-selected locale
    key (the marine service now does the swell-dominance bucket selection
    that used to happen inline in ``_compose_conditions_text()``).
  - ``periodLabelKey`` (top-level on a fishing entry) resolves to
    ``periodLabel``; ``speciesScores[].statusKey`` resolves to
    ``speciesScores[].status``.
  - ``conditionsTextParts`` (fishing): ``{"overallLabelKey": str,
    "pressurePhraseKey": str, "tidePhraseKey": str, "solunarClauseKey":
    str|None, "activeSpeciesNames": list[str]}``.
  - ``currentResidual``: ``{"valueM": float, "quality": str, "source":
    str}`` -- no ``value``, no ``description``; this module adds both,
    converting ``valueM`` (also outside ``_FIELD_GROUPS``, also raw SI at
    this point).
"""

from __future__ import annotations

import logging
from typing import Any

import configobj

from weewx_clearskies_api import i18n
from weewx_clearskies_api.config.marine_config import (
    MarineConfig,
    MarineLocation,
    load_marine_config,
)
from weewx_clearskies_api.services.units import get_group_unit, get_target_unit
from weewx_clearskies_api.units.conversion import convert as _convert_unit

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level config wiring (populated at startup, mirrors endpoints/
# marine.py's wire_marine_config() -- that module is deleted at T6.5, so
# this seam needs its own independent copy of the same MarineConfig).
# ---------------------------------------------------------------------------

_marine_config: MarineConfig | None = None

# ADR-090 beach-safety-relevant alert event types (substring,
# case-insensitive) -- ported from endpoints/beach_safety.py, which T6.8
# deletes.
_BEACH_SAFETY_ALERT_EVENTS: tuple[str, ...] = (
    "beach hazards statement",
    "high surf advisory",
    "high surf warning",
    "rip current statement",
    "coastal flood advisory",
    "coastal flood warning",
)


def wire_marine_enrichment_config(settings: object) -> None:
    """Store the parsed MarineConfig for use by this enrichment module.

    Mirrors endpoints/marine.py's wire_marine_config() defensive-resolution
    contract exactly (MarineConfig instance / object with .marine_config /
    raw configobj.ConfigObj -> None on anything else). Called from
    __main__.py alongside the other wire_*_config() calls.
    """
    global _marine_config  # noqa: PLW0603

    if isinstance(settings, MarineConfig):
        _marine_config = settings
        return

    attr = getattr(settings, "marine_config", None)
    if isinstance(attr, MarineConfig):
        _marine_config = attr
        return

    if isinstance(settings, configobj.ConfigObj):
        try:
            _marine_config = load_marine_config(settings)
        except ValueError:
            logger.error(
                "marine_enrichment: invalid [marine] configuration; "
                "station/alert restoration disabled",
                exc_info=True,
            )
            _marine_config = None
        return

    _marine_config = None


def _find_location(location_id: Any) -> MarineLocation | None:
    if _marine_config is None or not isinstance(location_id, str):
        return None
    for location in _marine_config.locations:
        if location.id == location_id:
            return location
    return None


# ---------------------------------------------------------------------------
# C-24 -- station-hardware observation restoration (/marine list + detail
# only; surf t=0 is HELD, see module docstring).
# ---------------------------------------------------------------------------


def _fetch_station_observation() -> Any | None:
    """Best-effort read of the most recent weewx archive row.

    Mirrors endpoints/marine.py's per-request Session(get_engine()) pattern
    exactly (lead ruling recorded there: a conditional path inside a
    helper is not a reason to thread a Depends()-injected Session through
    the whole proxy call chain). Returns None on any failure -- one
    location's station read failing must not fail the whole response.
    """
    try:
        from sqlalchemy.orm import Session  # noqa: PLC0415

        from weewx_clearskies_api.db.registry import get_registry  # noqa: PLC0415
        from weewx_clearskies_api.db.session import get_engine  # noqa: PLC0415
        from weewx_clearskies_api.services.archive import get_current  # noqa: PLC0415

        registry = get_registry()
        with Session(get_engine()) as station_db:
            return get_current(station_db, registry)
    except Exception:
        logger.warning("marine_enrichment: station observation read failed", exc_info=True)
        return None


def _restore_marine_list(data: list[Any]) -> list[Any]:
    """C-24 (list summary fields) + C-25 (activeAlerts) for GET /marine."""
    for item in data:
        if not isinstance(item, dict):
            continue
        location = _find_location(item.get("locationId"))
        if location is None:
            continue

        item["activeAlerts"] = _fetch_active_alerts(location)

        try:
            from weewx_clearskies_api.services.marine_location_resolver import (  # noqa: PLC0415
                is_station_served,
            )

            if not is_station_served(location.id):
                continue
        except Exception:
            logger.warning(
                "marine_enrichment: is_station_served() failed for %r", location.id,
                exc_info=True,
            )
            continue

        obs = _fetch_station_observation()
        if obs is None:
            continue

        updates: dict[str, float] = {}
        if obs.windSpeed is not None:
            updates["windSpeed"] = obs.windSpeed
        if obs.windDir is not None:
            updates["windDirection"] = obs.windDir
        if obs.outTemp is not None:
            updates["airTemp"] = obs.outTemp
        if not updates:
            continue

        current_conditions = item.get("currentConditions")
        if isinstance(current_conditions, dict):
            current_conditions.update(updates)
        else:
            item["currentConditions"] = {"stationId": location.id, **updates}
    return data


def _restore_marine_detail(data: dict[str, Any]) -> dict[str, Any]:
    """C-24 (detail observation fields) for GET /marine/{location_id}.

    No alerts here -- MarineBundle never carried activeAlerts (C-25 note).
    """
    location = _find_location(data.get("locationId"))
    if location is None:
        return data

    try:
        from weewx_clearskies_api.services.marine_location_resolver import (  # noqa: PLC0415
            is_station_served,
        )

        if not is_station_served(location.id):
            return data
    except Exception:
        logger.warning(
            "marine_enrichment: is_station_served() failed for %r", location.id, exc_info=True
        )
        return data

    obs = _fetch_station_observation()
    if obs is None:
        return data

    updates: dict[str, float] = {}
    if obs.windSpeed is not None:
        updates["windSpeed"] = obs.windSpeed
    if obs.windDir is not None:
        updates["windDirection"] = obs.windDir
    if obs.windGust is not None:
        updates["windGust"] = obs.windGust
    if obs.outTemp is not None:
        updates["airTemp"] = obs.outTemp
    if obs.barometer is not None:
        updates["pressure"] = obs.barometer
    if not updates:
        return data

    observation = data.get("observation")
    if isinstance(observation, dict):
        observation.update(updates)
    else:
        data["observation"] = {"stationId": location.id, **updates}
    return data


# ---------------------------------------------------------------------------
# C-25 -- alerts
# ---------------------------------------------------------------------------


def _fetch_active_alerts(location: MarineLocation) -> list[dict[str, str]] | None:
    """Best-effort NWS alert fetch, classified for the /marine list card.

    Ported from endpoints/marine.py's _fetch_active_alerts() /
    _classify_alert_type() (deleted at T6.5) -- unchanged logic, only the
    call site moved.
    """
    try:
        from weewx_clearskies_api.providers.alerts import nws  # noqa: PLC0415

        marine_zone_ids = [location.nws_marine_zone_id] if location.nws_marine_zone_id else None
        alerts = nws.fetch(
            lat=location.lat,
            lon=location.lon,
            user_agent_contact=None,
            marine_zone_ids=marine_zone_ids,
        )
        return [
            {"headline": alert.headline, "alertType": _classify_alert_type(alert.event)}
            for alert in alerts
        ]
    except Exception:
        logger.warning(
            "marine_enrichment: NWS alerts fetch failed for marine location %r",
            location.id,
            exc_info=True,
        )
        return None


def _classify_alert_type(event: str) -> str:
    """Ported verbatim from endpoints/marine.py's _classify_alert_type()."""
    event_lower = event.lower()
    if any(
        k in event_lower
        for k in ("small craft", "gale", "storm warning", "hurricane force", "special marine")
    ):
        return "marineZone"
    if "coastal flood" in event_lower:
        return "coastalFlood"
    if any(k in event_lower for k in ("beach hazard", "rip current", "high surf")):
        return "beachHazard"
    return "marineZone"


def _filter_beach_safety_alerts(alerts: list[Any]) -> list[Any]:
    """Ported verbatim from endpoints/beach_safety.py (deleted at T6.8)."""
    filtered = []
    for alert in alerts:
        event = (getattr(alert, "event", "") or "").strip().lower()
        if any(keyword in event for keyword in _BEACH_SAFETY_ALERT_EVENTS):
            filtered.append(alert)
    return filtered


def _restore_beach_safety_detail(data: dict[str, Any]) -> dict[str, Any]:
    """C-25 (assessment.activeAlerts) for GET /beach-safety/{location_id}.

    The list route never carried alerts and is unaffected (C-25 note).
    """
    location = _find_location(data.get("locationId"))
    assessment = data.get("assessment")
    if location is None or not isinstance(assessment, dict):
        return data

    try:
        from weewx_clearskies_api.providers.alerts import nws  # noqa: PLC0415

        marine_zone_ids = [location.nws_marine_zone_id] if location.nws_marine_zone_id else None
        alerts = nws.fetch(
            lat=location.lat,
            lon=location.lon,
            user_agent_contact=None,
            marine_zone_ids=marine_zone_ids,
        )
        assessment["activeAlerts"] = [
            a.headline for a in _filter_beach_safety_alerts(alerts)
        ]
    except Exception:
        logger.warning(
            "marine_enrichment: alerts fetch failed for beach-safety location %r",
            location.id,
            exc_info=True,
        )
        # Faithful to the pre-separation endpoint's own try/except: on
        # failure activeAlerts is left at whatever the marine service sent
        # (always [] per the pinned contract), never crashes the response.
    return data


# ---------------------------------------------------------------------------
# C-29 -- locale resolution + sentence composition (surf)
#
# Wire shape confirmed against the landed marine service (commit cc2be6a,
# weewx-clearskies-marine), superseding this module's original placeholder
# assumptions:
#
#   qualityKey: str | None            -- null in the unavailable case
#   windQualityKey: str               -- always populated, even when
#                                         qualityKey is null (wind is a real
#                                         observation, independent of
#                                         whether a face height exists)
#   conditionsTextParts: {
#     unavailable: bool,
#     heightM, periodS, directionDeg, windSpeedMps: float | None -- SI,
#         NOT walked by marine_response_conversion.py's generic converter
#         (these field names don't match its _FIELD_GROUPS table) -- this
#         module converts heightM/windSpeedMps itself; periodS needs no
#         conversion (single-unit group); directionDeg is unused (compass
#         below is already resolved).
#     compass: str | None,
#     swellSummaryKey: str | None,    -- already-selected locale key
#         (swell_clean/swell_mixed/swell_chop) -- the marine service does
#         the bucket selection now, not this module.
#   }
#   all conditionsTextParts fields null except unavailable=true in the
#   unavailable case.
# ---------------------------------------------------------------------------


def _format_range(low: float, high: float, locale: str) -> str:
    return f"{i18n.format_number(low, 0, locale)}-{i18n.format_number(high, 0, locale)}"


def _compose_surf_conditions_text(
    *,
    height_display: float,
    height_unit_label: str,
    period_s: float,
    compass: str,
    wind_label: str,
    wind_speed_display: float | None,
    wind_unit_label: str,
    swell_summary_key: str,
    locale: str,
) -> str:
    """Relocated essentially unchanged from enrichment/surf_scorer.py's
    _compose_conditions_text() (deleted at T6.7). The swell-dominance
    bucket selection that function used to do inline
    (``swell_score >= _SWELL_DOMINANCE_PURE_SCORE`` etc.) now happens in
    the marine service, which hands over the already-selected locale key
    (``swell_summary_key``) instead of the raw ratio -- this function only
    resolves it.
    """
    height_low = max(0.0, height_display - 1.0)
    height_high = height_display + 1.0
    wave_part = i18n.t("surf.conditions.wave_summary", locale).format(
        range=_format_range(height_low, height_high, locale),
        unit=height_unit_label,
        period=i18n.format_number(period_s, 0, locale),
        compass=compass,
    )

    if wind_speed_display is not None:
        wind_low = max(0.0, wind_speed_display - 2.5)
        wind_high = wind_speed_display + 2.5
        wind_part = i18n.t("surf.conditions.wind_with_speed", locale).format(
            wind_label=wind_label,
            range=_format_range(wind_low, wind_high, locale),
            unit=wind_unit_label,
        )
    else:
        wind_part = i18n.t("surf.conditions.wind_no_speed", locale).format(wind_label=wind_label)

    summary_part = i18n.t(swell_summary_key, locale)

    return f"{wave_part} {wind_part} {summary_part}"


# Marine-only display labels for group_ocean_speed target units -- same
# table marine_response_conversion.py uses (its _MARINE_UNIT_LABELS),
# duplicated here because this module converts windSpeedMps itself (see
# the section docstring above) and needs the matching label.
_OCEAN_SPEED_UNIT_LABELS: dict[str, str] = {
    "knot": "kt",
    "meter_per_second": "m/s",
    "mile_per_hour": "mph",
    "km_per_hour": "km/h",
}


def _height_target_and_label() -> tuple[str, str]:
    is_us = get_target_unit() == "US"
    target = get_group_unit("group_wave_height", "foot" if is_us else "meter")
    return target, ("ft" if target == "foot" else "m")


def _ocean_speed_target_and_label() -> tuple[str, str]:
    target = get_group_unit("group_ocean_speed", "knot")
    return target, _OCEAN_SPEED_UNIT_LABELS.get(target, target)


def _enrich_surf_entry(entry: dict[str, Any], locale: str) -> None:
    """Resolve a single SurfForecast-shaped entry's keys+parts in place,
    restoring the outward field names (qualityLabel/windQuality/
    conditionsText) the dashboard has always seen. Always writes the
    outward fields (even to None) so their presence in the response is
    unchanged -- popping a null key must not make the outward field vanish
    from the JSON.
    """
    quality_key = entry.pop("qualityKey", None)
    entry["qualityLabel"] = i18n.t(quality_key, locale) if isinstance(quality_key, str) else None

    wind_quality_key = entry.pop("windQualityKey", None)
    wind_quality_label = (
        i18n.t(wind_quality_key, locale) if isinstance(wind_quality_key, str) else None
    )
    entry["windQuality"] = wind_quality_label

    parts = entry.pop("conditionsTextParts", None)
    if not isinstance(parts, dict):
        logger.warning(
            "marine_enrichment: surf entry has no conditionsTextParts; "
            "conditionsText left unset: %r",
            entry,
        )
        return

    if parts.get("unavailable"):
        entry["conditionsText"] = i18n.t("surf.conditions.unavailable", locale)
        return

    height_m = parts.get("heightM")
    period_s = parts.get("periodS")
    compass = parts.get("compass")
    swell_summary_key = parts.get("swellSummaryKey")
    if (
        not isinstance(height_m, int | float)
        or not isinstance(period_s, int | float)
        or not isinstance(compass, str)
        or not isinstance(swell_summary_key, str)
    ):
        logger.warning(
            "marine_enrichment: conditionsTextParts missing required fields; "
            "leaving conditionsText unset: %r",
            parts,
        )
        return

    wind_speed_mps = parts.get("windSpeedMps")

    height_target, height_label = _height_target_and_label()
    wind_target, wind_label = _ocean_speed_target_and_label()

    entry["conditionsText"] = _compose_surf_conditions_text(
        height_display=float(_convert_unit(float(height_m), "meter", height_target) or 0.0),
        height_unit_label=height_label,
        period_s=float(period_s),
        compass=compass,
        wind_label=wind_quality_label or "",
        wind_speed_display=(
            _convert_unit(float(wind_speed_mps), "meter_per_second", wind_target)
            if isinstance(wind_speed_mps, int | float)
            else None
        ),
        wind_unit_label=wind_label,
        swell_summary_key=swell_summary_key,
        locale=locale,
    )


def _walk_surf_entries(node: Any, locale: str) -> None:
    """Recursively find and enrich every SurfForecast-shaped dict in *node*.

    Generic tree walk (same philosophy as marine_response_conversion.py's
    _walk()) rather than hardcoding /surf list vs /surf/{id} detail shapes
    -- this proxy has no per-route schema knowledge, and a forecast entry
    carrying qualityKey/windQualityKey/conditionsTextParts is
    self-identifying regardless of nesting depth.
    """
    if isinstance(node, dict):
        if "qualityKey" in node or "windQualityKey" in node or "conditionsTextParts" in node:
            _enrich_surf_entry(node, locale)
        for value in node.values():
            _walk_surf_entries(value, locale)
    elif isinstance(node, list):
        for item in node:
            _walk_surf_entries(item, locale)


# ---------------------------------------------------------------------------
# C-29 -- locale resolution + sentence composition (fishing)
# ---------------------------------------------------------------------------


def _join_names(names: list[str], locale: str) -> str:
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        connector = i18n.t("fishing.conditions.list_and", locale)
        return f"{names[0]}{connector}{names[1]}"
    separator = i18n.t("fishing.conditions.list_separator", locale)
    final_connector = i18n.t("fishing.conditions.list_final_and", locale)
    return separator.join(names[:-1]) + f"{final_connector}{names[-1]}"


def _compose_fishing_conditions_text(
    *,
    overall_label_key: str,
    pressure_phrase_key: str,
    tide_phrase_key: str,
    solunar_clause_key: str | None,
    active_species_names: list[str],
    locale: str,
) -> str:
    """Relocated essentially unchanged from enrichment/fishing_scorer.py's
    _compose_conditions_text() (deleted at T6.7). The marine service
    already selected which locale key applies for each phrase (overall
    bucket, pressure bucket, tide state, solunar period) -- this module
    only resolves and assembles, matching the surf composer's division of
    labor.
    """
    solunar_clause = i18n.t(solunar_clause_key, locale) if solunar_clause_key else ""

    text = i18n.t("fishing.conditions.summary", locale).format(
        overall_label=i18n.t(overall_label_key, locale),
        pressure_phrase=i18n.t(pressure_phrase_key, locale),
        tide_phrase=i18n.t(tide_phrase_key, locale),
        solunar_clause=solunar_clause,
    )
    if active_species_names:
        text += " " + i18n.t("fishing.conditions.species_active", locale).format(
            names=_join_names(active_species_names, locale)
        )
    return text


def _enrich_fishing_entry(entry: dict[str, Any], locale: str) -> None:
    period_label_key = entry.pop("periodLabelKey", None)
    if isinstance(period_label_key, str):
        entry["periodLabel"] = i18n.t(period_label_key, locale)

    species_scores = entry.get("speciesScores")
    if isinstance(species_scores, list):
        for species in species_scores:
            if not isinstance(species, dict):
                continue
            status_key = species.pop("statusKey", None)
            if isinstance(status_key, str):
                species["status"] = i18n.t(status_key, locale)

    parts = entry.pop("conditionsTextParts", None)
    if not isinstance(parts, dict):
        return

    overall_key = parts.get("overallLabelKey")
    pressure_key = parts.get("pressurePhraseKey")
    tide_key = parts.get("tidePhraseKey")
    if not isinstance(overall_key, str) or not isinstance(pressure_key, str) or not isinstance(
        tide_key, str
    ):
        logger.warning(
            "marine_enrichment: fishing conditionsTextParts missing required "
            "keys; leaving conditionsText untouched: %r",
            parts,
        )
        return

    solunar_key = parts.get("solunarClauseKey")
    active_names = parts.get("activeSpeciesNames")

    entry["conditionsText"] = _compose_fishing_conditions_text(
        overall_label_key=overall_key,
        pressure_phrase_key=pressure_key,
        tide_phrase_key=tide_key,
        solunar_clause_key=solunar_key if isinstance(solunar_key, str) else None,
        active_species_names=[n for n in active_names if isinstance(n, str)]
        if isinstance(active_names, list)
        else [],
        locale=locale,
    )


def _walk_fishing_entries(node: Any, locale: str) -> None:
    """Same generic-tree-walk philosophy as _walk_surf_entries()."""
    if isinstance(node, dict):
        if "periodLabelKey" in node or "conditionsTextParts" in node:
            _enrich_fishing_entry(node, locale)
        for value in node.values():
            _walk_fishing_entries(value, locale)
    elif isinstance(node, list):
        for item in node:
            _walk_fishing_entries(item, locale)


# ---------------------------------------------------------------------------
# C-37 -- currentResidual
# ---------------------------------------------------------------------------

# Ported verbatim from services/water_level_compositor.py (marine-owned
# after separation; deleted from the API in a later Phase 6 task) -- a pure
# unit-label lookup, not a scoring threshold, so relocating it duplicates
# nothing that must stay single-sourced.
_RESIDUAL_UNIT_LABEL: dict[str, str] = {"foot": "ft", "meter": "m"}


def _residual_target_unit() -> str:
    return get_group_unit("group_water_level", "foot" if get_target_unit() == "US" else "meter")


def _enrich_current_residual(node: dict[str, Any]) -> None:
    value_m = node.pop("valueM", None)
    if not isinstance(value_m, int | float):
        return
    target_unit = _residual_target_unit()
    value_display = _convert_unit(float(value_m), "meter", target_unit)
    if value_display is None:
        return
    unit_label = _RESIDUAL_UNIT_LABEL.get(target_unit, target_unit)
    sign = "+" if value_display >= 0 else ""
    node["value"] = round(value_display, 2)
    node["description"] = f"{sign}{value_display:.2f} {unit_label} vs prediction"


def _walk_current_residual(node: Any) -> None:
    if isinstance(node, dict):
        if "valueM" in node and "quality" in node:
            _enrich_current_residual(node)
        for value in node.values():
            _walk_current_residual(value)
    elif isinstance(node, list):
        for item in node:
            _walk_current_residual(item)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def apply_marine_enrichment(data: Any, *, manifest_path: str) -> Any:
    """Apply C-24/C-25/C-29/C-37 restoration to an already-converted marine
    payload. Called from companion_proxy.py's
    _apply_post_conversion_enrichment().

    *data* may be None (modelStatus: "unavailable" null payload) -- passed
    through untouched, matching every other stage of this pipeline's
    null-safety contract (marine_response_conversion.py's own docstring).

    No ``units_block`` parameter: every number this module composes text
    from (surf's conditionsTextParts, currentResidual's valueM) arrives as
    raw SI and this module converts it directly via services/units.py's
    get_group_unit()/get_target_unit() -- the same operator-configured
    target resolution marine_response_conversion.py uses, just called a
    second time locally rather than threaded through, since the fields it
    touches (heightM/windSpeedMps/valueM) aren't in that module's
    _FIELD_GROUPS table and were never converted by it in the first place.
    """
    if data is None:
        return None

    locale = i18n.get_active_locale()

    if manifest_path == "/marine" and isinstance(data, list):
        data = _restore_marine_list(data)
    elif manifest_path == "/marine/{location_id}" and isinstance(data, dict):
        data = _restore_marine_detail(data)
    elif manifest_path == "/beach-safety/{location_id}" and isinstance(data, dict):
        data = _restore_beach_safety_detail(data)

    if manifest_path in ("/surf", "/surf/{location_id}"):
        _walk_surf_entries(data, locale)

    if manifest_path in ("/fishing", "/fishing/{location_id}"):
        _walk_fishing_entries(data, locale)

    if manifest_path in ("/tides", "/tides/{location_id}"):
        _walk_current_residual(data)

    return data
