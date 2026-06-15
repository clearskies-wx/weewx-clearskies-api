"""High-level unit transformer.

UnitTransformer converts raw weather data dicts (from the REST archive path
or the direct adapter path) into display-ready dicts with converted values,
labels, and formatted strings.

Design notes:
- Stateless per-call; the transformer itself holds only configuration.
- transform_record() (REST/archive path) and transform_field() (direct adapter
  path) return the same output shape:
  {"value": float|None, "label": str, "formatted": str}.
- Use from_settings() to construct from a UnitsSettings object (api.conf
  [units] section).  Pass target_units / label_overrides / format_overrides /
  ordinates directly for tests or custom construction.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .conversion import convert
from .derived import beaufort, comfort_index
from .groups import OBS_GROUP, UNIT_SYSTEMS, VALID_UNITS, get_source_unit  # noqa: F401
from .labels import format_value, get_label

if TYPE_CHECKING:
    from weewx_clearskies_api.config.settings import UnitsSettings

# Metadata fields in archive records that carry no physical unit.
_METADATA_FIELDS: frozenset[str] = frozenset({"dateTime", "usUnits", "interval"})


# Default 16-point compass ordinate labels (weewx default order).
# These are the canonical language-neutral codes the dashboard localises via
# i18next (ADR-021).  Operator [[ordinates]] overrides affect only the
# display label (self._ordinates in UnitTransformer); they do NOT affect
# windDirCardinal/windGustDirCardinal, which always index this constant list.
_DEFAULT_ORDINATES: list[str] = [
    "N", "NNE", "NE", "ENE",
    "E", "ESE", "SE", "SSE",
    "S", "SSW", "SW", "WSW",
    "W", "WNW", "NW", "NNW",
]


def _degrees_to_index(degrees: float) -> int:
    """Convert compass degrees to a 0-based 16-sector index.

    Divides the circle into 16 equal 22.5° sectors, offset by 11.25° so that
    N spans 348.75°–11.25° (wrapping through 0°) rather than 0°–22.5°.

    This is the single authoritative sector formula shared by
    ``_direction_label`` (operator-overridable display labels) and the
    ``windDirCardinal`` / ``windGustDirCardinal`` output fields (canonical
    i18n codes always indexed against ``_DEFAULT_ORDINATES``).

    Returns:
        Integer in [0, 15].
    """
    return int((degrees + 11.25) / 22.5) % 16


class UnitTransformer:
    """Transforms raw weather values to operator display units.

    Args:
        target_units:   group_name → target unit string.
                        Keys are group names (e.g. "group_temperature"),
                        values are unit strings (e.g. "degree_C").
        label_overrides:   unit → label override (from operator [[Labels]]).
        format_overrides:  unit → format string override (from [[StringFormats]]).
        ordinates:         16 compass direction labels, N through NNW.
    """

    def __init__(
        self,
        target_units: dict[str, str],
        label_overrides: dict[str, str] | None = None,
        format_overrides: dict[str, str] | None = None,
        ordinates: list[str] | None = None,
    ) -> None:
        # Validate every target unit against the known valid-unit set for its group.
        for group, unit in target_units.items():
            if group in VALID_UNITS and unit not in VALID_UNITS[group]:
                raise ValueError(f"Invalid unit '{unit}' for {group}")
        self._targets = target_units
        self._label_overrides = label_overrides
        self._format_overrides = format_overrides
        self._ordinates = ordinates if ordinates is not None else _DEFAULT_ORDINATES

    @classmethod
    def from_settings(cls, settings: "UnitsSettings") -> "UnitTransformer":
        """Construct a UnitTransformer from a UnitsSettings object.

        Reads target units, label overrides, format overrides, and ordinates
        from the api.conf [units] section (ADR-042).  Falls back to built-in
        defaults for any subsection absent from the config.

        Args:
            settings: UnitsSettings parsed from the [units] section of api.conf.

        Returns:
            Configured UnitTransformer instance.
        """
        target_units = dict(settings.groups) if settings.groups else {}
        label_overrides = dict(settings.labels) if settings.labels else None
        format_overrides = (
            dict(settings.string_formats) if settings.string_formats else None
        )
        ordinates: list[str] | None = (
            list(settings.directions) if settings.directions else None
        )
        return cls(
            target_units=target_units,
            label_overrides=label_overrides,
            format_overrides=format_overrides,
            ordinates=ordinates,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def build_units_block(self) -> dict[str, str]:
        """Build the per-field units label dict from the transformer's target units.

        Iterates OBS_GROUP (observation → group mapping). For each observation
        whose group has a configured target unit, looks up the display label
        (accounting for operator label overrides). Returns a dict suitable for
        the REST response 'units' envelope.
        """
        block: dict[str, str] = {}
        for obs, group in OBS_GROUP.items():
            target = self._targets.get(group)
            if target is None:
                continue
            block[obs] = get_label(target, self._label_overrides)
        return block

    def transform_record(self, data: dict[str, object], us_units: int) -> dict[str, object]:
        """Transform an archive record dict from the REST API.

        Args:
            data:     observation_name → raw_value (values may be None).
            us_units: unit-system code (1=US, 16=Metric, 17=MetricWX).

        Returns:
            dict where known observations become
            {"value": float|None, "label": str, "formatted": str}
            and unknown / metadata fields are passed through unchanged.
        """
        result: dict[str, object] = {}

        for obs, raw_value in data.items():
            if obs in _METADATA_FIELDS:
                continue

            # extras sub-dict: recursively transform any entries that are
            # known observations; unknown / non-numeric entries pass through raw.
            if obs == "extras" and isinstance(raw_value, dict):
                extras_out: dict[str, object] = {}
                for sub_key, sub_val in raw_value.items():
                    transformed = self._transform_single_obs(sub_key, sub_val, us_units)
                    extras_out[sub_key] = transformed
                result[obs] = extras_out
                continue

            group = OBS_GROUP.get(obs)
            if group is None:
                # Unknown observation — pass raw value through.
                result[obs] = raw_value
                continue

            result[obs] = self._transform_single_obs(obs, raw_value, us_units)

        # --- Derived fields ---
        # Computed from raw values + us_units so we work in the original unit
        # system rather than back-converting from display units.

        raw_wind = data.get("windSpeed")
        if raw_wind is not None:
            src_wind = get_source_unit("windSpeed", us_units)
            if src_wind is not None:
                try:
                    result["beaufort"] = beaufort(float(raw_wind), src_wind)
                except (ValueError, TypeError):
                    pass

        raw_temp = data.get("outTemp")
        if raw_temp is not None:
            src_temp = get_source_unit("outTemp", us_units)
            if src_temp is not None:
                try:
                    result["comfortIndex"] = comfort_index(float(raw_temp), src_temp)
                except (ValueError, TypeError):
                    pass

        return result

    def transform_field(
        self,
        obs_name: str,
        raw_value: float | str | None,
        source_unit: str,
    ) -> dict[str, object]:
        """Transform a single field with known source unit (direct adapter path).

        Args:
            obs_name:    observation name (e.g. "outTemp").
            raw_value:   raw value; may be a string because the direct adapter
                         may forward string-encoded values.
            source_unit: source unit (e.g. "degree_F").

        Returns:
            {"value": float|None, "label": str, "formatted": str}
        """
        group = OBS_GROUP.get(obs_name)
        target_unit = self._targets.get(group, "") if group else ""

        if raw_value is None:
            return {
                "value": None,
                "label": get_label(target_unit, self._label_overrides),
                "formatted": "N/A",
            }

        # Parse string → float (direct adapter may send strings).
        try:
            numeric = float(raw_value)
        except (ValueError, TypeError):
            return {"value": None, "label": "", "formatted": str(raw_value)}

        if group is None:
            return {"value": numeric, "label": "", "formatted": str(raw_value)}

        if not target_unit:
            # No target unit configured for this group (pass-through groups
            # such as radiation, humidity, UV).  Source == display unit, so
            # apply formatting with the source unit rather than leaking an
            # unrounded float string to the SSE stream.  Mirrors the
            # transform_record() pass-through branch exactly.
            return {
                "value": numeric,
                "label": get_label(source_unit, self._label_overrides),
                "formatted": format_value(numeric, source_unit, self._format_overrides),
            }

        # Wind direction special case.
        if group == "group_direction":
            compass = self._direction_label(numeric)
            return {"value": numeric, "label": compass, "formatted": compass}

        converted = convert(numeric, source_unit, target_unit)
        assert converted is not None
        return {
            "value": converted,
            "label": get_label(target_unit, self._label_overrides),
            "formatted": format_value(converted, target_unit, self._format_overrides),
        }

    def add_derived_fields(self, record: dict[str, object]) -> None:
        """Add Beaufort and comfortIndex from already-converted display values.

        Mutates *record* in place.  Called after all individual fields have
        been transformed (direct adapter path), so values are already in
        display units.

        Args:
            record: Converted record dict (field → ConvertedValue dict or raw).
        """
        wind_entry = record.get("windSpeed")
        if isinstance(wind_entry, dict) and wind_entry.get("value") is not None:
            target_speed = self._targets.get("group_speed")
            if target_speed is not None:
                try:
                    record["beaufort"] = beaufort(
                        float(wind_entry["value"]),  # type: ignore[arg-type]
                        target_speed,
                    )
                except (ValueError, TypeError):
                    pass

        temp_entry = record.get("outTemp")
        if isinstance(temp_entry, dict) and temp_entry.get("value") is not None:
            target_temp = self._targets.get("group_temperature")
            if target_temp is not None:
                try:
                    record["comfortIndex"] = comfort_index(
                        float(temp_entry["value"]),  # type: ignore[arg-type]
                        target_temp,
                    )
                except (ValueError, TypeError):
                    pass

        # --- weatherText (ADR-044) via shared composer (smoothed inputs) ---
        # Lazy import mirrors the existing lazy-import pattern; avoids the
        # conditions_text ↔ units circular-dependency at import time.
        # sky_condition.update() is NOT called here — that is the packet-tap's
        # responsibility (see enrichment/sky_tap.py).  Calling it here too
        # would double-count radiation samples from the REST/archive path.
        from weewx_clearskies_api.enrichment.weather_text import (  # noqa: PLC0415
            compose_weather_text as _compose_wt,
        )

        record["weatherText"] = {
            "value": _compose_wt() or "",
            "label": "",
            "formatted": "",
        }

        # --- 10-minute rolling wind average and max-gust (T3a.3) ---
        # Lazy import avoids a circular dependency (transformer ↔ enrichment).
        # Fields are omitted entirely before MIN_COVERAGE_SECONDS of data has
        # accumulated (get_wind_avg / get_gust_max return None); never inject null.
        from weewx_clearskies_api.enrichment.wind_rolling_window import (  # noqa: I001, PLC0415
            get_gust_max as _get_gust_max,
            get_wind_avg as _get_wind_avg,
        )
        from .labels import format_value as _fmt, get_label as _lbl

        target_speed = self._targets.get("group_speed")
        disp_unit = target_speed or "mile_per_hour"

        avg = _get_wind_avg()
        if avg is not None:
            try:  # noqa: SIM105
                record["windSpeedAvg10m"] = {
                    "value": avg,
                    "label": _lbl(disp_unit, self._label_overrides),
                    "formatted": _fmt(avg, disp_unit, self._format_overrides),
                }
            except Exception:  # noqa: BLE001, S110
                pass

        gust = _get_gust_max()
        if gust is not None:
            try:  # noqa: SIM105
                record["windGustMax10m"] = {
                    "value": gust,
                    "label": _lbl(disp_unit, self._label_overrides),
                    "formatted": _fmt(gust, disp_unit, self._format_overrides),
                }
            except Exception:  # noqa: BLE001, S110
                pass

        # --- Lightning strike history (T2a.3) ---
        # Distance values pass through in the station's configured unit — no
        # unit conversion needed here.  The field is always set (empty list
        # when no strikes in the 24-hour window) so the dashboard can clear
        # a previously displayed strike overlay on quiet periods.
        try:
            from weewx_clearskies_api.enrichment.lightning_strike_buffer import (  # noqa: PLC0415
                get_strike_history as _get_strikes,
            )
            record["lightningStrikeHistory"] = _get_strikes()
        except Exception:  # noqa: BLE001, S110
            pass

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _transform_single_obs(
        self,
        obs: str,
        raw_value: object,
        us_units: int,
    ) -> object:
        """Transform one observation name + raw value to a ConvertedValue dict.

        Returns a ``{"value": float|None, "label": str, "formatted": str}``
        dict for observations that are in OBS_GROUP.  Returns *raw_value*
        unchanged for observations not in OBS_GROUP (string passthrough, unknown
        fields, etc.).

        This is the per-field kernel used by both ``transform_record()`` (for
        top-level fields) and the ``extras`` sub-dict recursion path, so the
        conversion/formatting logic lives in exactly one place.
        """
        group = OBS_GROUP.get(obs)
        if group is None:
            # Not a known weewx observation — pass through unchanged.
            return raw_value

        target_unit = self._targets.get(group)
        if target_unit is None:
            # No target configured for this group (pass-through groups such as
            # radiation, humidity, UV).  Apply formatting with the source unit
            # so callers don't receive unrounded floats.
            source_unit = get_source_unit(obs, us_units)
            if source_unit is None or raw_value is None:
                return raw_value
            return {
                "value": float(raw_value),  # type: ignore[arg-type]
                "label": get_label(source_unit, self._label_overrides),
                "formatted": format_value(
                    float(raw_value),  # type: ignore[arg-type]
                    source_unit,
                    self._format_overrides,
                ),
            }

        source_unit = get_source_unit(obs, us_units)

        if source_unit is None or raw_value is None:
            return {
                "value": None,
                "label": get_label(target_unit, self._label_overrides),
                "formatted": "N/A",
            }

        # Wind direction: degrees are degrees; format as compass label.
        if group == "group_direction":
            assert isinstance(raw_value, (int, float))
            deg = float(raw_value)
            compass = self._direction_label(deg)
            return {"value": deg, "label": compass, "formatted": compass}

        assert isinstance(raw_value, (int, float))
        converted = convert(float(raw_value), source_unit, target_unit)
        assert converted is not None
        return {
            "value": converted,
            "label": get_label(target_unit, self._label_overrides),
            "formatted": format_value(converted, target_unit, self._format_overrides),
        }

    def _direction_label(self, degrees: float) -> str:
        """Convert compass degrees to the operator's ordinate label.

        Uses the shared ``_degrees_to_index`` formula so the sector boundary
        is identical to the canonical cardinal codes emitted by the BFF.
        Indexes ``self._ordinates`` (operator-overridable) rather than
        ``_DEFAULT_ORDINATES``, so operator [[ordinates]] config is honoured.
        """
        return self._ordinates[_degrees_to_index(degrees)]
