"""Round S (2026-08-05): SurfScoringBreakdown reshape (ADR-101) through the
generic SI->display converter (``services/marine_response_conversion.py``).

The marine service now emits ``scoring`` as ``{size, shape, conditions,
power, consistency}`` (int, unitless 0-100) plus ``weights`` (dict of
0-1 floats, same five keys) -- the old ``waveHeight``/``wavePeriod``/
``waveOrganization`` scoring-context suppression rule is retired (deleted
in this round, see module docstring) because none of the six new field
names collide with any entry in ``_FIELD_GROUPS``. It also adds a
top-level ``qualityScore`` (int, unitless 0-100) on the SurfForecast
entry itself -- the numeric total the five factors combine into, added
alongside this reshape (no numeric total previously existed on the wire).
These tests pin that the proxy passes the new shape through completely
unconverted -- no group assigned, no units-block entry claimed --
following the dev-owned fixture convention established by
``test_marine_response_conversion_d102.py`` (commit c1a8212, same module,
same pattern: implementer-authored tests landed in the same commit as the
code change).
"""

from __future__ import annotations

import pytest

from weewx_clearskies_api.services import marine_response_conversion as mrc


@pytest.fixture()
def _us_units(monkeypatch: pytest.MonkeyPatch):
    """Pin operator display units to the US defaults so any accidental
    conversion (a regression this test exists to catch) would show up as a
    changed value rather than being masked by a no-op unit."""
    monkeypatch.setattr(mrc, "get_target_unit", lambda: "US")
    monkeypatch.setattr(mrc, "get_group_unit", lambda _group, default: default)


def test_scoring_factors_pass_through_unconverted(_us_units) -> None:
    body = {
        "forecast": [
            {
                "time": "2026-08-05T12:00:00Z",
                "qualityScore": 78,
                "scoring": {
                    "size": 72,
                    "shape": 55,
                    "conditions": 80,
                    "power": 64,
                    "consistency": 90,
                    "weights": {
                        "size": 0.25,
                        "shape": 0.25,
                        "conditions": 0.2,
                        "power": 0.2,
                        "consistency": 0.1,
                    },
                },
            }
        ]
    }
    converted, units_block = mrc.convert_marine_payload(body)
    entry = converted["forecast"][0]
    scoring = entry["scoring"]

    # The total (SurfForecast.qualityScore, added 2026-08-05 alongside this
    # reshape) is unitless 0-100 too -- same "no group, no conversion"
    # treatment as qualityStars.
    assert entry["qualityScore"] == 78
    assert "qualityScore" not in units_block

    # Every factor is unchanged -- no unit conversion applied to unitless
    # 0-100 scores.
    assert scoring["size"] == 72
    assert scoring["shape"] == 55
    assert scoring["conditions"] == 80
    assert scoring["power"] == 64
    assert scoring["consistency"] == 90
    assert scoring["weights"] == {
        "size": 0.25,
        "shape": 0.25,
        "conditions": 0.2,
        "power": 0.2,
        "consistency": 0.1,
    }

    # None of the six new field names claim a units-block entry -- they
    # were never assigned a group.
    for key in ("size", "shape", "conditions", "power", "consistency", "weights"):
        assert key not in units_block


def test_scoring_object_does_not_suppress_sibling_physical_fields(_us_units) -> None:
    """The retired scoring-context suppression rule used to key off the
    literal string "scoring" as a container name -- confirm a sibling
    field on the SAME forecast entry (outside the scoring object) still
    converts normally, i.e. no context leakage from the (now-deleted)
    special-casing."""
    body = {
        "forecast": [
            {
                "waveHeightAtBreak": 2.0,
                "scoring": {
                    "size": 50,
                    "shape": 50,
                    "conditions": 50,
                    "power": 50,
                    "consistency": 50,
                    "weights": {
                        "size": 0.25,
                        "shape": 0.25,
                        "conditions": 0.2,
                        "power": 0.2,
                        "consistency": 0.1,
                    },
                },
            }
        ]
    }
    converted, units_block = mrc.convert_marine_payload(body)
    entry = converted["forecast"][0]
    assert entry["waveHeightAtBreak"] == pytest.approx(2.0 * 3.280839895, rel=1e-4)
    assert "waveHeightAtBreak" in units_block
    assert entry["scoring"]["size"] == 50
