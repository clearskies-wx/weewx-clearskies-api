"""Known-answer tests for StructureConfig coordinate parsing (api.conf).

Regression guard for the defect where the API's StructureConfig read the
`coordinates` field by iterating it directly (`[float(c[0]), float(c[1])]
for c in section.get("coordinates", [])`) while the apply writer
(endpoints/setup.py) persists it to api.conf as a JSON *string* (configobj
cannot round-trip a native nested list). On read, load_marine_config() then
iterated the raw string character by character and raised.

The marine service's StructureConfig already decoded the JSON string; the
API's did not. These tests pin the API reader to both shapes: the
JSON-string shape it gets from api.conf (via configobj) and the native-list
shape it gets from an in-memory apply payload.
"""

from __future__ import annotations

import io
import json

import configobj

from weewx_clearskies_api.config.marine_config import StructureConfig

# The real Huntington Beach Pier footprint (first two nodes) — the shape a
# discovery-sourced structure carries.
_COORDS = [[-118.0024017, 33.6568667], [-118.0023571, 33.6568353]]


def test_coordinates_json_string_from_api_conf_roundtrip():
    """api.conf stores coordinates as a JSON string (configobj auto-quotes it);
    the reader must json.loads it back into the nested float list."""
    section = {
        "type": "pier",
        "material": "semi_permeable",
        "length_m": "566.8",
        "bearing_degrees": "221.0",
        "distance_m": "124.5",
        "coordinates": json.dumps(_COORDS),  # exactly what setup.py writes
    }
    s = StructureConfig(section)
    assert s.coordinates == _COORDS
    assert all(isinstance(pt[0], float) and isinstance(pt[1], float) for pt in s.coordinates)


def test_coordinates_native_list_from_apply_payload():
    """An in-memory apply payload carries coordinates as a native nested list;
    that must still parse unchanged."""
    section = {"type": "pier", "material": "semi_permeable", "coordinates": _COORDS}
    s = StructureConfig(section)
    assert s.coordinates == _COORDS


def test_coordinates_absent_or_empty():
    """Absent or empty coordinates → [] (structure excluded from OBSTACLE)."""
    assert StructureConfig({"type": "pier", "material": "concrete"}).coordinates == []
    assert StructureConfig(
        {"type": "pier", "material": "concrete", "coordinates": ""}
    ).coordinates == []


def test_full_configobj_roundtrip_through_configobj():
    """End-to-end: write a JSON-string coordinates value through configobj (as
    the apply writer does), read it back, and confirm StructureConfig parses
    it. Guards the real api.conf on-disk shape, not just an in-memory dict."""
    c = configobj.ConfigObj()
    c["s"] = {"type": "pier", "material": "semi_permeable", "coordinates": json.dumps(_COORDS)}
    buf = io.BytesIO()
    c.write(buf)
    buf.seek(0)
    read_back = configobj.ConfigObj(buf)["s"]
    # configobj yields the value as a single quoted string, not a comma-split list.
    assert isinstance(read_back["coordinates"], str)
    s = StructureConfig(read_back)
    assert s.coordinates == _COORDS


def test_legacy_bearing_to_spot_degrees_key_is_tolerated_and_ignored():
    """Deletion tolerance KAT: an api.conf structure section written by a
    prior version that still carries the now-deleted `bearing_to_spot_degrees`
    key must parse cleanly — no ValidationError, no crash, no attribute set —
    and the field must not exist on the resulting StructureConfig. Guards
    against a legacy artifact breaking on read after the field's removal."""
    section = {
        "type": "pier",
        "material": "semi_permeable",
        "length_m": "100.0",
        "bearing_degrees": "45.0",
        "distance_m": "50.0",
        "bearing_to_spot_degrees": "270.0",  # stale key from a prior version
        "coordinates": _COORDS,
    }
    s = StructureConfig(section)
    assert s.coordinates == _COORDS
    assert not hasattr(s, "bearing_to_spot_degrees")
    s.validate("test_loc")  # must not raise
