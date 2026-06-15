"""Response-level unit conversion for the clearskies-api.

Ported from weewx-clearskies-realtime proxy.py.  Applies UnitTransformer
to JSON response dicts that contain weather data before they are sent to
the dashboard.

Three response shapes are handled:
  Shape 2  ({data: dict,  units: label_dict, ...}) -- /current envelope
  Shape 2b ({data: list,  units: label_dict, ...}) -- /archive envelope
  Shape 3  ({records|data|results: [...], ...})    -- generic nested list
  Shape 4  (flat record with usUnits key)          -- direct-read / MQTT

The module keeps a module-level _transformer reference populated at startup
by configure().  Call configure() once in __main__.py after creating the
UnitTransformer instance.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from weewx_clearskies_api.units.transformer import (
    _DEFAULT_ORDINATES,
    _degrees_to_index,
)

if TYPE_CHECKING:
    from weewx_clearskies_api.units.transformer import UnitTransformer

logger = logging.getLogger(__name__)

_transformer: "UnitTransformer | None" = None

# Map from the label strings the upstream API returns in its ``units`` block
# to weewx unit-system codes.  Temperature is the primary discriminator
# (unambiguous across all three systems); rain disambiguates Metric from
# MetricWX when the temperature label alone matches both.
_TEMP_LABEL_TO_US_UNITS: dict[str, int] = {
    "°F": 1,   # US
    "°C": 16,  # Metric or MetricWX — disambiguated by rain label below
}
_RAIN_LABEL_TO_US_UNITS: dict[str, int] = {
    "mm": 17,  # MetricWX
    "cm": 16,  # Metric
}


def configure(transformer: "UnitTransformer") -> None:
    """Set the module-level transformer.

    Called once at app startup after UnitTransformer.from_settings() in
    __main__.py.  Idempotent — safe to call again (e.g. in tests).

    Args:
        transformer: Configured UnitTransformer instance.
    """
    global _transformer  # noqa: PLW0603
    _transformer = transformer
    logger.info("response_conversion configured")


def _infer_us_units(units_block: dict[str, object]) -> int:
    """Infer the weewx usUnits code from the API ``units`` label block.

    The clearskies-api does not embed a ``usUnits`` integer in its ``/current``
    response; it only sends a ``units`` dict of field → label strings (e.g.
    ``{"outTemp": "°F", "windSpeed": "mph"}``).  This function reverse-maps
    those labels back to a unit-system code so that
    ``UnitTransformer.transform_record()`` can look up source units.

    Args:
        units_block: The ``units`` dict from the ``/current`` response.

    Returns:
        1 (US), 16 (Metric), or 17 (MetricWX).  Defaults to 1 (US) when the
        labels are absent or do not match any known system.
    """
    temp_label = str(units_block.get("outTemp", ""))
    us_units = _TEMP_LABEL_TO_US_UNITS.get(temp_label, 1)

    # Metric (16) and MetricWX (17) both use °C for temperature.
    # Disambiguate via the rain label: MetricWX uses mm, Metric uses cm.
    if us_units == 16:
        rain_label = str(units_block.get("rain", ""))
        us_units = _RAIN_LABEL_TO_US_UNITS.get(rain_label, 16)

    return us_units


def _cardinal_for_degrees(degrees: object) -> "str | None":
    """Return the canonical 16-point cardinal code for *degrees*, or None.

    Uses the shared ``_degrees_to_index`` formula (same sector boundaries as
    the transformer's ``_direction_label``).  Returns None when *degrees* is
    not a finite number so that a null windDir produces null windDirCardinal.

    The code is language-neutral (one of N, NNE, NE, ENE, E, ESE, SE, SSE,
    S, SSW, SW, WSW, W, WNW, NW, NNW).  The dashboard localises it via
    i18next (ADR-021) — the API never emits a translated string here.
    """
    try:
        deg = float(degrees)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return _DEFAULT_ORDINATES[_degrees_to_index(deg)]


def apply_conversion(
    data: "dict[str, object] | list[object]",
) -> "dict[str, object] | list[object]":
    """Apply unit conversion to API response data.

    Handles four shapes:

    1. A list — each element is recursively converted if it is a dict.
    2. An observation envelope ``{data: dict, units: label_dict, ...}`` — the
       shape returned by ``/current``.  The ``data`` sub-dict is a flat
       observation record; ``units`` is a label block used to infer the source
       unit system.  The transformer is applied to ``data`` directly and the
       result replaces it in the returned envelope.
    3. An archive envelope ``{data: list, units: label_dict, ...}`` — the shape
       returned by ``/archive``.  Each record in the list is converted with
       full-precision ``val["value"]`` extraction (for chart rendering).
       Exception: ``beaufort`` is kept as a ConvertedValue dict (wind rose
       reads it via extractNumber).
    4. A nested-list envelope ``{records|data|results: [...], ...}`` — convert
       each record but don't modify the outer envelope.
    5. A flat record with ``usUnits`` — weewx direct-read or MQTT records.

    Args:
        data: Response dict or list from model_dump().

    Returns:
        Converted dict or list.  Returns *data* unchanged when no transformer
        is configured or when the shape does not match any known pattern.
    """
    if isinstance(data, list):
        return [
            apply_conversion(item) if isinstance(item, dict) else item
            for item in data
        ]

    if not isinstance(data, dict):
        return data

    # --- Shape 2: observation envelope {data: dict, units: label_dict, ...} ---
    # The /current response embeds a flat observation dict under "data" and a
    # label block under "units".  Detect this by checking that "data" is a dict
    # (not a list) AND "units" is also a dict.
    obs_payload = data.get("data")
    units_block = data.get("units")
    if (
        isinstance(obs_payload, dict)
        and isinstance(units_block, dict)
        and _transformer is not None
    ):
        us_units = _infer_us_units(units_block)
        try:
            converted_obs = _transformer.transform_record(obs_payload, us_units)
            # transform_record returns {value, label, formatted} dicts for
            # known numeric observations.  Pass them through as-is so the
            # dashboard's isConvertedValue() check succeeds (aligns REST shape
            # with SSE — see sse.py lines 95-97).
            #
            # Wrap any remaining raw numeric fields (not yet converted) in the
            # same ConvertedValue format so every numeric field is uniform.
            for k, v in converted_obs.items():
                if k == "extras":
                    continue  # extras handled separately below
                if isinstance(v, (int, float)) and v is not True and v is not False:
                    converted_obs[k] = {"value": v, "label": "", "formatted": str(v)}

            # extras sub-dict: wrap its raw numerics the same way.
            extras = converted_obs.get("extras")
            if isinstance(extras, dict):
                for k, v in extras.items():
                    if isinstance(v, (int, float)) and v is not True and v is not False:
                        extras[k] = {"value": v, "label": "", "formatted": str(v)}

            # Inject canonical 16-point cardinal codes alongside windDir /
            # windGustDir so the dashboard can localise them via i18next
            # (ADR-021) without recomputing the sector on the client side.
            # null windDir → null windDirCardinal (not omitted).
            # Extract degrees from the ConvertedValue dict (value key).
            wind_dir = converted_obs.get("windDir")
            wind_gust_dir = converted_obs.get("windGustDir")
            deg = wind_dir["value"] if isinstance(wind_dir, dict) and "value" in wind_dir else wind_dir
            gust_deg = wind_gust_dir["value"] if isinstance(wind_gust_dir, dict) and "value" in wind_gust_dir else wind_gust_dir
            converted_obs["windDirCardinal"] = _cardinal_for_degrees(deg)
            converted_obs["windGustDirCardinal"] = _cardinal_for_degrees(gust_deg)

            return {**data, "data": converted_obs}
        except Exception:  # noqa: BLE001
            logger.debug("Observation envelope conversion failed; passing through raw")

    # --- Shape 2b: archive envelope {data: list, units: label_dict, ...} ---
    # Same envelope as Shape 2 but "data" is a list of records (not a single
    # dict).  Infer usUnits from the "units" label block, then transform each
    # record with that unit system.  This injects derived fields (beaufort,
    # comfortIndex) that Shape 3 alone would miss because individual records
    # lack a usUnits key.
    if (
        isinstance(obs_payload, list)
        and isinstance(units_block, dict)
        and _transformer is not None
    ):
        us_units = _infer_us_units(units_block)
        converted_list = []
        for record in obs_payload:
            if not isinstance(record, dict):
                converted_list.append(record)
                continue
            try:
                converted = _transformer.transform_record(record, us_units)
                # Extract raw converted values from ConvertedValue dicts.
                # Unlike Shape 2 (which passes ConvertedValue dicts through
                # as-is), archive records need full-precision scalar values
                # for chart rendering.
                # Exception: keep 'beaufort' as a ConvertedValue dict — the
                # wind rose binning reads it via extractNumber({value, label, formatted}).
                flattened_rec: dict[str, object] = {}
                for key, val in converted.items():
                    if key == "beaufort":
                        flattened_rec[key] = val
                    elif isinstance(val, dict) and "value" in val:
                        flattened_rec[key] = val["value"]
                    else:
                        flattened_rec[key] = val
                # Preserve metadata fields stripped by transform_record
                # (dateTime, usUnits, interval) — not observations, but
                # required by the response schema.
                for meta_key in ("interval", "dateTime", "usUnits"):
                    if meta_key in record and meta_key not in flattened_rec:
                        flattened_rec[meta_key] = record[meta_key]
                converted_list.append(flattened_rec)
            except Exception:  # noqa: BLE001
                converted_list.append(record)
        return {**data, "data": converted_list}

    # --- Shape 3: nested-list envelope {records|data|results: [...], ...} ---
    # Convert each record in the list but don't modify the outer envelope.
    for key in ("records", "data", "results"):
        if key in data and isinstance(data[key], list):
            converted_list = [
                apply_conversion(r) if isinstance(r, dict) else r
                for r in data[key]  # type: ignore[union-attr]
            ]
            return {**data, key: converted_list}

    # --- Shape 4: flat record with usUnits (direct-read / MQTT / archive) ---
    us_units_raw = data.get("usUnits")
    if us_units_raw is not None and _transformer is not None:
        try:
            return _transformer.transform_record(data, int(us_units_raw))  # type: ignore[arg-type]
        except (ValueError, TypeError):
            pass

    return data
