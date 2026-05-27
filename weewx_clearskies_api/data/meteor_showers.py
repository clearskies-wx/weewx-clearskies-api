"""Static reference table of major annual meteor showers.

Used by compute_meteor_showers() in services/almanac.py.

Radiant coordinates are J2000.0 epoch, mean values for the peak date.
ZHR values are approximate observed maximums under ideal conditions.
"""

from __future__ import annotations

from dataclasses import dataclass


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
