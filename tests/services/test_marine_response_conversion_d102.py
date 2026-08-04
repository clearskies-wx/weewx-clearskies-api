"""D10.2 (2026-08-04): the two new SurfForecast fields through the generic
SI->display converter (``services/marine_response_conversion.py``).

Round context: marine ``69d831a`` added ``shadowFaceHeight`` (SI meters, or
null) and ``perPartitionBreaks`` (reusing the beach-profile per-partition
shape) to SurfForecast entries. The converter is field-NAME-keyed and
recursive, so the nested ``perPartitionBreaks`` sub-fields were already
covered by the beach-profile route's entries — but the new top-level
``shadowFaceHeight`` name needed its own ``_FIELD_GROUPS`` entry, without
which it passed through in raw meters while the dashboard labels it with
the operator's height unit (a silent ~3.28x display error in US units).

These are the first direct tests of ``convert_marine_payload`` (the module
had no dedicated test file — pre-existing gap, tracked separately); they
pin only the D10.2-relevant behavior, not the whole inventory.
"""

from __future__ import annotations

import pytest

from weewx_clearskies_api.services import marine_response_conversion as mrc


@pytest.fixture()
def _us_units(monkeypatch: pytest.MonkeyPatch):
    """Pin operator display units to the US defaults (height -> foot) so
    the expected conversion factor is deterministic regardless of the test
    host's config."""
    monkeypatch.setattr(mrc, "get_target_unit", lambda: "US")
    monkeypatch.setattr(mrc, "get_group_unit", lambda _group, default: default)


_M_TO_FT = 3.280839895


def test_shadow_face_height_converts_and_registers_unit(_us_units) -> None:
    body = {"forecast": [{"shadowFaceHeight": 2.0, "bestPeakFaceHeight": 1.0}]}
    converted, units_block = mrc.convert_marine_payload(body)
    entry = converted["forecast"][0]
    assert entry["shadowFaceHeight"] == pytest.approx(2.0 * _M_TO_FT, rel=1e-4)
    # Same treatment as its sibling statistic — both converted, both keyed
    # in the units block with the same label.
    assert entry["bestPeakFaceHeight"] == pytest.approx(1.0 * _M_TO_FT, rel=1e-4)
    assert units_block["shadowFaceHeight"] == units_block["bestPeakFaceHeight"]


def test_shadow_face_height_null_passes_through(_us_units) -> None:
    body = {"forecast": [{"shadowFaceHeight": None}]}
    converted, units_block = mrc.convert_marine_payload(body)
    assert converted["forecast"][0]["shadowFaceHeight"] is None
    # A null is not a numeric conversion — no units key is claimed by it.
    assert "shadowFaceHeight" not in units_block


def test_per_partition_breaks_subfields_convert_recursively(_us_units) -> None:
    """The round relies on the walker converting the SAME sub-field names
    at the NEW nesting site (SurfForecast entry) it already converts on the
    beach-profile route — pin that assumption."""
    body = {
        "forecast": [
            {
                "perPartitionBreaks": [
                    {
                        "partitionIndex": 0,
                        "periodS": 12.4,
                        "directionDeg": 245.0,
                        "heightM": 1.0,
                        "meanFaceHeightM": 2.0,
                        "peakFaceHeightM": 3.0,
                        "meanBreakDistanceM": 100.0,
                        "meanBreakDepthM": 4.0,
                        "classification": "groundswell",
                        "dominantBreakerType": "plunging",
                    }
                ]
            }
        ]
    }
    converted, units_block = mrc.convert_marine_payload(body)
    pb = converted["forecast"][0]["perPartitionBreaks"][0]
    assert pb["heightM"] == pytest.approx(1.0 * _M_TO_FT, rel=1e-4)
    assert pb["meanFaceHeightM"] == pytest.approx(2.0 * _M_TO_FT, rel=1e-4)
    assert pb["peakFaceHeightM"] == pytest.approx(3.0 * _M_TO_FT, rel=1e-4)
    assert pb["meanBreakDistanceM"] == pytest.approx(100.0 * _M_TO_FT, rel=1e-4)
    assert pb["meanBreakDepthM"] == pytest.approx(4.0 * _M_TO_FT, rel=1e-4)
    # Non-length fields untouched.
    assert pb["periodS"] == pytest.approx(12.4)
    assert pb["directionDeg"] == pytest.approx(245.0)
    assert pb["partitionIndex"] == 0
    assert pb["classification"] == "groundswell"
    assert pb["dominantBreakerType"] == "plunging"
    assert "heightM" in units_block
