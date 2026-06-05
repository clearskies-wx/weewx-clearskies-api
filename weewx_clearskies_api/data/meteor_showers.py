"""Static reference table of major annual meteor showers.

Used by compute_meteor_showers() in services/almanac.py.

Radiant coordinates are J2000.0 epoch, mean values for the peak date.
ZHR values are approximate observed maximums under ideal conditions.

The default catalog is loaded from a JSON file at runtime via load_catalog().
METEOR_SHOWERS is the embedded fallback used when the JSON file is absent.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MeteorShowerData:
    name: str
    peak_month: int
    peak_day: int
    duration_days: int
    radiant_ra_deg: float
    radiant_dec_deg: float
    zhr: int
    parent_body: str
    id: str = ""
    description: str = ""
    velocity_kms: float = 0.0
    solar_longitude_max: float = 0.0
    image: str = ""


METEOR_SHOWERS: list[MeteorShowerData] = [
    MeteorShowerData("Quadrantids", 1, 3, 4, 230.1, 48.5, 120, "2003 EH1"),
    MeteorShowerData("Lyrids", 4, 22, 3, 271.4, 33.6, 18, "C/1861 G1 Thatcher"),
    MeteorShowerData("Eta Aquariids", 5, 6, 10, 338.0, -1.0, 50, "1P/Halley"),
    MeteorShowerData("Delta Aquariids", 7, 30, 20, 340.0, -16.0, 25, "96P/Machholz"),
    MeteorShowerData("Perseids", 8, 12, 14, 48.0, 58.0, 100, "109P/Swift-Tuttle"),
    MeteorShowerData("Draconids", 10, 8, 2, 262.0, 54.0, 10, "21P/Giacobini-Zinner"),
    MeteorShowerData("Orionids", 10, 21, 7, 95.0, 16.0, 20, "1P/Halley"),
    MeteorShowerData("Taurids", 11, 5, 30, 54.0, 22.0, 10, "2P/Encke"),
    MeteorShowerData("Leonids", 11, 17, 4, 152.0, 22.0, 15, "55P/Tempel-Tuttle"),
    MeteorShowerData("Geminids", 12, 14, 6, 112.0, 33.0, 150, "3200 Phaethon"),
    MeteorShowerData("Ursids", 12, 22, 3, 217.0, 76.0, 10, "8P/Tuttle"),
    MeteorShowerData(
        "Southern Delta Aquariids", 7, 28, 15, 339.0, -16.4, 16, "96P/Machholz"
    ),
]


def load_catalog(catalog_path: str | None = None) -> list[MeteorShowerData]:
    """Load the meteor shower catalog from a JSON file.

    Tries to load from catalog_path (or the bundled default JSON if None).
    Falls back to the embedded METEOR_SHOWERS list on FileNotFoundError.
    Logs a warning per malformed entry and skips it; valid entries are kept.

    Args:
        catalog_path: Path to a JSON catalog file.  None = bundled default
            (weewx_clearskies_api/data/meteor_showers.json, shipped with the
            package).

    Returns:
        List of MeteorShowerData instances.  Never raises — always returns
        at least the embedded fallback list.
    """
    # Resolve the path: None -> bundled JSON alongside this module.
    resolved = (
        Path(__file__).with_suffix(".json") if catalog_path is None else Path(catalog_path)
    )

    try:
        raw_text = resolved.read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.warning(
            "Meteor shower catalog not found at %s; using embedded fallback (%d showers).",
            resolved,
            len(METEOR_SHOWERS),
        )
        return list(METEOR_SHOWERS)
    except OSError as exc:
        logger.warning(
            "Could not read meteor shower catalog at %s: %s; using embedded fallback.",
            resolved,
            exc,
        )
        return list(METEOR_SHOWERS)

    try:
        entries = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        logger.warning(
            "Meteor shower catalog at %s is not valid JSON: %s; using embedded fallback.",
            resolved,
            exc,
        )
        return list(METEOR_SHOWERS)

    if not isinstance(entries, list):
        logger.warning(
            "Meteor shower catalog at %s must be a JSON array; using embedded fallback.",
            resolved,
        )
        return list(METEOR_SHOWERS)

    results: list[MeteorShowerData] = []
    required_fields = {
        "name", "peak_month", "peak_day", "duration_days",
        "radiant_ra_deg", "radiant_dec_deg", "zhr", "parent_body",
    }

    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            logger.warning(
                "Meteor shower catalog entry %d is not a JSON object; skipping.", i
            )
            continue

        missing = required_fields - entry.keys()
        if missing:
            logger.warning(
                "Meteor shower catalog entry %d (%r) missing required fields %s; skipping.",
                i,
                entry.get("name", "<unnamed>"),
                sorted(missing),
            )
            continue

        try:
            shower = MeteorShowerData(
                name=str(entry["name"]),
                peak_month=int(entry["peak_month"]),
                peak_day=int(entry["peak_day"]),
                duration_days=int(entry["duration_days"]),
                radiant_ra_deg=float(entry["radiant_ra_deg"]),
                radiant_dec_deg=float(entry["radiant_dec_deg"]),
                zhr=int(entry["zhr"]),
                parent_body=str(entry["parent_body"]),
                id=str(entry.get("id", "")),
                description=str(entry.get("description", "")),
                velocity_kms=float(entry.get("velocity_kms", 0.0)),
                solar_longitude_max=float(entry.get("solar_longitude_max", 0.0)),
                image=str(entry.get("image", "")),
            )
        except (TypeError, ValueError, KeyError) as exc:
            logger.warning(
                "Meteor shower catalog entry %d (%r) could not be parsed: %s; skipping.",
                i,
                entry.get("name", "<unnamed>"),
                exc,
            )
            continue

        results.append(shower)

    if not results:
        logger.warning(
            "Meteor shower catalog at %s yielded no valid entries; using embedded fallback.",
            resolved,
        )
        return list(METEOR_SHOWERS)

    logger.debug("Loaded %d meteor showers from %s.", len(results), resolved)
    return results
