"""Unit tests for providers/_common/wms_capabilities.py (3b-14).

Tests the shared WMS GetCapabilities XML parser used by all four WMS-T radar
providers (iem_nexrad, noaa_mrms, msc_geomet, dwd_radolan).

Coverage per 3b-14 brief §Test coverage + WMS GetCapabilities fixture notes:

  Real fixture loading (period notation form):
  - IEM NEXRAD get_capabilities.xml (layer nexrad-n0q-wmst):
    TIME = 2011-02-16/2026-12-31/PT5M (long historical range).
    parse_wms_time_dimension() expands to timestamps up to _MAX_PERIOD_FRAMES.
  - NOAA MRMS get_capabilities.xml (layer radar_base_reflectivity_time):
    TIME = 2026-05-11T23:16:00.0Z/2026-05-12T01:06:59.0Z/PT1S (1s period).
  - MSC GeoMet get_capabilities.xml (layer RADAR_1KM_RRAI, real — sibling RADAR_1KM_RSNO):
    TIME = 2026-05-11T21:54:00Z/2026-05-12T00:54:00Z/PT6M.
  - DWD RADOLAN get_capabilities.xml (layer Niederschlagsradar, real — sibling RADOLAN-RW):
    TIME = 2026-05-08T00:00:00.000Z/2026-05-12T03:15:00.000Z/PT5M.

  Synthetic inline fixture (comma-separated list form):
  - WMS spec allows comma-separated timestamps in Dimension; no live provider
    returned this form at capture time (all 4 WMS-T providers use period notation).
  - Hand-crafted inline XML with comma-separated timestamps covers this code path.
  - Documented per synthetic-from-real fixture pattern in agent def.

  Both TIME dimension forms per WMS 1.3.0 spec:
  - Period notation: start/end/period (all 4 live captures).
  - Comma-separated: covered by synthetic inline XML.

  Error handling:
  - Malformed XML → ProviderProtocolError.
  - Layer not found → ProviderProtocolError with "layer not found" detail.
  - No TIME dimension → ProviderProtocolError with "no TIME dimension" detail.
  - Empty TIME dimension → ProviderProtocolError with "empty" detail.
  - Zero-second period → ProviderProtocolError (division-by-zero guard).

  Period expansion logic:
  - PT5M period → correct 5-minute intervals.
  - PT1H period → correct hourly intervals.
  - All results end with 'Z' suffix (ADR-020 UTC requirement).
  - Latest timestamp is last in returned list.

  Namespace handling:
  - WMS 1.3.0 namespace (http://www.opengis.net/wms) respected.
  - No-namespace XML also parsed (ArcGIS pattern).

ADR references: ADR-015, ADR-020, ADR-038.
Fixture paths: tests/fixtures/providers/radar/{iem_nexrad,noaa_mrms,msc_geomet,dwd_radolan}/get_capabilities.xml
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

_FIXTURES_DIR = Path(__file__).parent.parent.parent / "fixtures" / "providers" / "radar"

# ---------------------------------------------------------------------------
# Inline synthetic fixture — comma-separated TIME dimension form
# ---------------------------------------------------------------------------
# No live provider returned comma-separated at capture time (2026-05-11/12).
# This fixture is hand-crafted to exercise the comma-separated code path in
# _parse_time_values(). See dwd_radolan/fixtures.md note on REFERENCE_TIME.
_COMMA_SEPARATED_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<WMS_Capabilities version="1.3.0"
  xmlns="http://www.opengis.net/wms">
  <Capability>
    <Layer>
      <Layer queryable="1">
        <Name>test-radar-layer</Name>
        <Title>Test Comma-Separated Layer</Title>
        <Dimension name="time" units="ISO8601">
          2026-05-11T00:00:00Z,2026-05-11T00:05:00Z,2026-05-11T00:10:00Z,
          2026-05-11T00:15:00Z,2026-05-11T00:20:00Z
        </Dimension>
      </Layer>
    </Layer>
  </Capability>
</WMS_Capabilities>
"""

# Inline XML with no WMS namespace (ArcGIS-style — NOAA MRMS is backed by ArcGIS)
_NO_NAMESPACE_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<WMS_Capabilities version="1.3.0">
  <Capability>
    <Layer>
      <Layer queryable="1">
        <Name>ns-test-layer</Name>
        <Dimension name="time" units="ISO8601">2026-05-11T12:00:00Z/2026-05-11T14:00:00Z/PT30M</Dimension>
      </Layer>
    </Layer>
  </Capability>
</WMS_Capabilities>
"""

_MALFORMED_XML = b"<WMS_Capabilities>not closed"

_NO_TIME_DIMENSION_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<WMS_Capabilities version="1.3.0" xmlns="http://www.opengis.net/wms">
  <Capability>
    <Layer>
      <Layer>
        <Name>no-time-layer</Name>
        <Dimension name="elevation" units="CRS:88">0</Dimension>
      </Layer>
    </Layer>
  </Capability>
</WMS_Capabilities>
"""

_EMPTY_TIME_DIMENSION_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<WMS_Capabilities version="1.3.0" xmlns="http://www.opengis.net/wms">
  <Capability>
    <Layer>
      <Layer>
        <Name>empty-time-layer</Name>
        <Dimension name="time" units="ISO8601">  </Dimension>
      </Layer>
    </Layer>
  </Capability>
</WMS_Capabilities>
"""

_ZERO_PERIOD_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<WMS_Capabilities version="1.3.0" xmlns="http://www.opengis.net/wms">
  <Capability>
    <Layer>
      <Layer>
        <Name>zero-period-layer</Name>
        <Dimension name="time" units="ISO8601">2026-05-11T00:00:00Z/2026-05-11T01:00:00Z/PT0S</Dimension>
      </Layer>
    </Layer>
  </Capability>
</WMS_Capabilities>
"""


def _load_fixture(provider: str, filename: str) -> bytes:
    """Load a fixture file for a radar provider as raw bytes."""
    path = _FIXTURES_DIR / provider / filename
    return path.read_bytes()


# ===========================================================================
# 1. Real fixture loading — period notation form (all 4 WMS-T providers)
# ===========================================================================


class TestIEMNEXRADFixture:
    """IEM NEXRAD real fixture: nexrad-n0q-wmst layer, period notation."""

    def test_parse_returns_list_of_strings(self) -> None:
        """parse_wms_time_dimension returns list[str] for IEM NEXRAD fixture."""
        from weewx_clearskies_api.providers._common.wms_capabilities import parse_wms_time_dimension  # noqa: PLC0415

        xml_bytes = _load_fixture("iem_nexrad", "get_capabilities.xml")
        result = parse_wms_time_dimension(
            xml_bytes,
            layer="nexrad-n0q-wmst",
            provider_id="iem_nexrad",
            domain="radar",
        )
        assert isinstance(result, list)
        assert len(result) > 0, "IEM NEXRAD fixture should expand to at least 1 timestamp"

    def test_parse_result_is_capped_at_max_period_frames(self) -> None:
        """IEM NEXRAD long historical range (2011-2026/PT5M) capped at _MAX_PERIOD_FRAMES."""
        from weewx_clearskies_api.providers._common.wms_capabilities import (  # noqa: PLC0415
            _MAX_PERIOD_FRAMES,
            parse_wms_time_dimension,
        )

        xml_bytes = _load_fixture("iem_nexrad", "get_capabilities.xml")
        result = parse_wms_time_dimension(
            xml_bytes,
            layer="nexrad-n0q-wmst",
            provider_id="iem_nexrad",
            domain="radar",
        )
        # The range 2011-2026 at PT5M is enormous; must be capped
        assert len(result) <= _MAX_PERIOD_FRAMES, (
            f"IEM NEXRAD period expansion should be capped at {_MAX_PERIOD_FRAMES}; "
            f"got {len(result)}"
        )
        assert len(result) == _MAX_PERIOD_FRAMES, (
            f"Expected exactly {_MAX_PERIOD_FRAMES} (capped); got {len(result)}"
        )

    def test_all_results_end_with_z(self) -> None:
        """All IEM NEXRAD timestamps end with 'Z' suffix (ADR-020 UTC requirement)."""
        from weewx_clearskies_api.providers._common.wms_capabilities import parse_wms_time_dimension  # noqa: PLC0415

        xml_bytes = _load_fixture("iem_nexrad", "get_capabilities.xml")
        result = parse_wms_time_dimension(
            xml_bytes,
            layer="nexrad-n0q-wmst",
            provider_id="iem_nexrad",
            domain="radar",
        )
        for ts in result:
            assert ts.endswith("Z"), f"Timestamp {ts!r} must end with Z (ADR-020)"

    def test_missing_layer_raises_provider_protocol_error(self) -> None:
        """Layer not in IEM NEXRAD capabilities → ProviderProtocolError."""
        from weewx_clearskies_api.providers._common.errors import ProviderProtocolError  # noqa: PLC0415
        from weewx_clearskies_api.providers._common.wms_capabilities import parse_wms_time_dimension  # noqa: PLC0415

        xml_bytes = _load_fixture("iem_nexrad", "get_capabilities.xml")
        with pytest.raises(ProviderProtocolError, match="not found"):
            parse_wms_time_dimension(
                xml_bytes,
                layer="nonexistent-layer-xyz",
                provider_id="iem_nexrad",
                domain="radar",
            )


class TestNOAAMRMSFixture:
    """NOAA MRMS real fixture: radar_base_reflectivity_time layer, PT1S period."""

    def test_parse_returns_nonempty_list(self) -> None:
        """parse_wms_time_dimension returns non-empty list for NOAA MRMS fixture."""
        from weewx_clearskies_api.providers._common.wms_capabilities import parse_wms_time_dimension  # noqa: PLC0415

        xml_bytes = _load_fixture("noaa_mrms", "get_capabilities.xml")
        result = parse_wms_time_dimension(
            xml_bytes,
            layer="radar_base_reflectivity_time",
            provider_id="noaa_mrms",
            domain="radar",
        )
        assert len(result) > 0, "NOAA MRMS fixture should expand to at least 1 timestamp"

    def test_all_results_end_with_z(self) -> None:
        """All NOAA MRMS timestamps end with 'Z' suffix (ADR-020)."""
        from weewx_clearskies_api.providers._common.wms_capabilities import parse_wms_time_dimension  # noqa: PLC0415

        xml_bytes = _load_fixture("noaa_mrms", "get_capabilities.xml")
        result = parse_wms_time_dimension(
            xml_bytes,
            layer="radar_base_reflectivity_time",
            provider_id="noaa_mrms",
            domain="radar",
        )
        for ts in result:
            assert ts.endswith("Z"), f"Timestamp {ts!r} must end with Z (ADR-020)"

    def test_pt1s_period_does_not_exceed_max_frames(self) -> None:
        """NOAA MRMS PT1S period doesn't produce runaway expansion."""
        from weewx_clearskies_api.providers._common.wms_capabilities import (  # noqa: PLC0415
            _MAX_PERIOD_FRAMES,
            parse_wms_time_dimension,
        )

        xml_bytes = _load_fixture("noaa_mrms", "get_capabilities.xml")
        result = parse_wms_time_dimension(
            xml_bytes,
            layer="radar_base_reflectivity_time",
            provider_id="noaa_mrms",
            domain="radar",
        )
        assert len(result) <= _MAX_PERIOD_FRAMES, (
            f"PT1S expansion must be capped at {_MAX_PERIOD_FRAMES}; got {len(result)}"
        )


class TestMSCGeoMetFixture:
    """MSC GeoMet fixture: RADAR_1KM_RRAI layer (real — sibling RADAR_1KM_RSNO), PT6M period."""

    def test_parse_returns_31_frames_for_3h_pt6m(self) -> None:
        """MSC GeoMet 3h range at PT6M → 31 frames (21:54..00:54, inclusive)."""
        from weewx_clearskies_api.providers._common.wms_capabilities import parse_wms_time_dimension  # noqa: PLC0415

        xml_bytes = _load_fixture("msc_geomet", "get_capabilities.xml")
        result = parse_wms_time_dimension(
            xml_bytes,
            layer="RADAR_1KM_RRAI",
            provider_id="msc_geomet",
            domain="radar",
        )
        # 2026-05-11T21:54:00Z → 2026-05-12T00:54:00Z at PT6M = 31 timestamps (inclusive)
        assert len(result) == 31, f"Expected 31 frames for 3h/PT6M range; got {len(result)}"

    def test_first_frame_time_is_correct(self) -> None:
        """MSC GeoMet first frame = 2026-05-11T21:54:00Z (start of period range)."""
        from weewx_clearskies_api.providers._common.wms_capabilities import parse_wms_time_dimension  # noqa: PLC0415

        xml_bytes = _load_fixture("msc_geomet", "get_capabilities.xml")
        result = parse_wms_time_dimension(
            xml_bytes,
            layer="RADAR_1KM_RRAI",
            provider_id="msc_geomet",
            domain="radar",
        )
        assert result[0] == "2026-05-11T21:54:00Z", (
            f"First frame should be 2026-05-11T21:54:00Z, got {result[0]!r}"
        )

    def test_last_frame_time_is_correct(self) -> None:
        """MSC GeoMet last frame = 2026-05-12T00:54:00Z (end of period range)."""
        from weewx_clearskies_api.providers._common.wms_capabilities import parse_wms_time_dimension  # noqa: PLC0415

        xml_bytes = _load_fixture("msc_geomet", "get_capabilities.xml")
        result = parse_wms_time_dimension(
            xml_bytes,
            layer="RADAR_1KM_RRAI",
            provider_id="msc_geomet",
            domain="radar",
        )
        assert result[-1] == "2026-05-12T00:54:00Z", (
            f"Last frame should be 2026-05-12T00:54:00Z, got {result[-1]!r}"
        )

    def test_all_results_end_with_z(self) -> None:
        """All MSC GeoMet timestamps end with 'Z' suffix (ADR-020)."""
        from weewx_clearskies_api.providers._common.wms_capabilities import parse_wms_time_dimension  # noqa: PLC0415

        xml_bytes = _load_fixture("msc_geomet", "get_capabilities.xml")
        result = parse_wms_time_dimension(
            xml_bytes,
            layer="RADAR_1KM_RRAI",
            provider_id="msc_geomet",
            domain="radar",
        )
        for ts in result:
            assert ts.endswith("Z"), f"Timestamp {ts!r} must end with Z (ADR-020)"

    def test_sibling_layer_rsno_also_parseable(self) -> None:
        """RADAR_1KM_RSNO (snow sibling — real layer from live capture) also parses correctly."""
        from weewx_clearskies_api.providers._common.wms_capabilities import parse_wms_time_dimension  # noqa: PLC0415

        xml_bytes = _load_fixture("msc_geomet", "get_capabilities.xml")
        result = parse_wms_time_dimension(
            xml_bytes,
            layer="RADAR_1KM_RSNO",
            provider_id="msc_geomet",
            domain="radar",
        )
        # Same range as RADAR_1KM_RRAI in fixture (same Dimension value across sibling layers).
        assert len(result) == 31, f"RADAR_1KM_RSNO should yield 31 frames; got {len(result)}"


class TestDWDRADOLANFixture:
    """DWD RADOLAN fixture: Niederschlagsradar layer (real — sibling RADOLAN-RW), PT5M period."""

    def test_parse_returns_nonempty_list(self) -> None:
        """parse_wms_time_dimension returns non-empty list for DWD fixture."""
        from weewx_clearskies_api.providers._common.wms_capabilities import parse_wms_time_dimension  # noqa: PLC0415

        xml_bytes = _load_fixture("dwd_radolan", "get_capabilities.xml")
        result = parse_wms_time_dimension(
            xml_bytes,
            layer="Niederschlagsradar",
            provider_id="dwd_radolan",
            domain="radar",
        )
        assert len(result) > 0, "DWD RADOLAN fixture should expand to at least 1 timestamp"

    def test_dwd_rx_is_capped_at_max_period_frames(self) -> None:
        """DWD ~4-day range at PT5M capped at _MAX_PERIOD_FRAMES (300)."""
        from weewx_clearskies_api.providers._common.wms_capabilities import (  # noqa: PLC0415
            _MAX_PERIOD_FRAMES,
            parse_wms_time_dimension,
        )

        xml_bytes = _load_fixture("dwd_radolan", "get_capabilities.xml")
        result = parse_wms_time_dimension(
            xml_bytes,
            layer="Niederschlagsradar",
            provider_id="dwd_radolan",
            domain="radar",
        )
        # 4+ days at PT5M = 1152+ frames; must be capped
        assert len(result) == _MAX_PERIOD_FRAMES, (
            f"Expected cap at {_MAX_PERIOD_FRAMES}; got {len(result)}"
        )

    def test_all_results_end_with_z(self) -> None:
        """All DWD timestamps end with 'Z' suffix (ADR-020)."""
        from weewx_clearskies_api.providers._common.wms_capabilities import parse_wms_time_dimension  # noqa: PLC0415

        xml_bytes = _load_fixture("dwd_radolan", "get_capabilities.xml")
        result = parse_wms_time_dimension(
            xml_bytes,
            layer="Niederschlagsradar",
            provider_id="dwd_radolan",
            domain="radar",
        )
        for ts in result:
            assert ts.endswith("Z"), f"Timestamp {ts!r} must end with Z"

    def test_sibling_layer_radolan_rw_also_parseable(self) -> None:
        """RADOLAN-RW (hourly sibling — real layer from live capture) also parses correctly."""
        from weewx_clearskies_api.providers._common.wms_capabilities import parse_wms_time_dimension  # noqa: PLC0415

        xml_bytes = _load_fixture("dwd_radolan", "get_capabilities.xml")
        result = parse_wms_time_dimension(
            xml_bytes,
            layer="RADOLAN-RW",
            provider_id="dwd_radolan",
            domain="radar",
        )
        assert len(result) > 0, "RADOLAN-RW should yield timestamps from its period range"


# ===========================================================================
# 2. Synthetic fixture — comma-separated TIME dimension form
# ===========================================================================


class TestCommaSeparatedTimeDimension:
    """Comma-separated TIME dimension (WMS spec allows this; no live provider used it at capture)."""

    def test_comma_separated_returns_five_timestamps(self) -> None:
        """5 comma-separated timestamps in synthetic XML → 5-element list."""
        from weewx_clearskies_api.providers._common.wms_capabilities import parse_wms_time_dimension  # noqa: PLC0415

        result = parse_wms_time_dimension(
            _COMMA_SEPARATED_XML,
            layer="test-radar-layer",
            provider_id="test",
            domain="radar",
        )
        assert len(result) == 5, f"Expected 5 timestamps, got {len(result)}"

    def test_comma_separated_first_timestamp_correct(self) -> None:
        """First comma-separated timestamp = 2026-05-11T00:00:00Z."""
        from weewx_clearskies_api.providers._common.wms_capabilities import parse_wms_time_dimension  # noqa: PLC0415

        result = parse_wms_time_dimension(
            _COMMA_SEPARATED_XML,
            layer="test-radar-layer",
            provider_id="test",
            domain="radar",
        )
        assert result[0] == "2026-05-11T00:00:00Z", f"Expected 2026-05-11T00:00:00Z, got {result[0]!r}"

    def test_comma_separated_last_timestamp_correct(self) -> None:
        """Last comma-separated timestamp = 2026-05-11T00:20:00Z."""
        from weewx_clearskies_api.providers._common.wms_capabilities import parse_wms_time_dimension  # noqa: PLC0415

        result = parse_wms_time_dimension(
            _COMMA_SEPARATED_XML,
            layer="test-radar-layer",
            provider_id="test",
            domain="radar",
        )
        assert result[-1] == "2026-05-11T00:20:00Z", f"Expected 2026-05-11T00:20:00Z, got {result[-1]!r}"

    def test_comma_separated_all_end_with_z(self) -> None:
        """All comma-separated timestamps end with 'Z' (ADR-020)."""
        from weewx_clearskies_api.providers._common.wms_capabilities import parse_wms_time_dimension  # noqa: PLC0415

        result = parse_wms_time_dimension(
            _COMMA_SEPARATED_XML,
            layer="test-radar-layer",
            provider_id="test",
            domain="radar",
        )
        for ts in result:
            assert ts.endswith("Z"), f"Timestamp {ts!r} must end with Z"


# ===========================================================================
# 3. Namespace handling
# ===========================================================================


class TestNamespaceHandling:
    """WMS namespace handling — both namespaced and no-namespace XML."""

    def test_no_namespace_xml_parsed_correctly(self) -> None:
        """No-namespace XML (ArcGIS pattern) → correct timestamp list."""
        from weewx_clearskies_api.providers._common.wms_capabilities import parse_wms_time_dimension  # noqa: PLC0415

        result = parse_wms_time_dimension(
            _NO_NAMESPACE_XML,
            layer="ns-test-layer",
            provider_id="test",
            domain="radar",
        )
        # 2026-05-11T12:00:00Z to 2026-05-11T14:00:00Z at PT30M = 5 timestamps
        assert len(result) == 5, f"Expected 5 frames for PT30M/2h range; got {len(result)}"
        assert result[0] == "2026-05-11T12:00:00Z"
        assert result[-1] == "2026-05-11T14:00:00Z"


# ===========================================================================
# 4. Error handling
# ===========================================================================


class TestErrorHandling:
    """parse_wms_time_dimension raises ProviderProtocolError on bad input."""

    def test_malformed_xml_raises_provider_protocol_error(self) -> None:
        """Malformed XML bytes → ProviderProtocolError (not stdlib ParseError)."""
        from weewx_clearskies_api.providers._common.errors import ProviderProtocolError  # noqa: PLC0415
        from weewx_clearskies_api.providers._common.wms_capabilities import parse_wms_time_dimension  # noqa: PLC0415

        with pytest.raises(ProviderProtocolError):
            parse_wms_time_dimension(
                _MALFORMED_XML,
                layer="any-layer",
                provider_id="test",
                domain="radar",
            )

    def test_layer_not_found_raises_provider_protocol_error(self) -> None:
        """Layer not found in capabilities tree → ProviderProtocolError with 'not found' text."""
        from weewx_clearskies_api.providers._common.errors import ProviderProtocolError  # noqa: PLC0415
        from weewx_clearskies_api.providers._common.wms_capabilities import parse_wms_time_dimension  # noqa: PLC0415

        xml_bytes = _load_fixture("iem_nexrad", "get_capabilities.xml")
        with pytest.raises(ProviderProtocolError, match="not found"):
            parse_wms_time_dimension(
                xml_bytes,
                layer="totally-nonexistent-layer",
                provider_id="iem_nexrad",
                domain="radar",
            )

    def test_no_time_dimension_raises_provider_protocol_error(self) -> None:
        """Layer with no TIME Dimension → ProviderProtocolError with relevant detail."""
        from weewx_clearskies_api.providers._common.errors import ProviderProtocolError  # noqa: PLC0415
        from weewx_clearskies_api.providers._common.wms_capabilities import parse_wms_time_dimension  # noqa: PLC0415

        with pytest.raises(ProviderProtocolError):
            parse_wms_time_dimension(
                _NO_TIME_DIMENSION_XML,
                layer="no-time-layer",
                provider_id="test",
                domain="radar",
            )

    def test_empty_time_dimension_raises_provider_protocol_error(self) -> None:
        """Empty TIME Dimension element → ProviderProtocolError."""
        from weewx_clearskies_api.providers._common.errors import ProviderProtocolError  # noqa: PLC0415
        from weewx_clearskies_api.providers._common.wms_capabilities import parse_wms_time_dimension  # noqa: PLC0415

        with pytest.raises(ProviderProtocolError):
            parse_wms_time_dimension(
                _EMPTY_TIME_DIMENSION_XML,
                layer="empty-time-layer",
                provider_id="test",
                domain="radar",
            )

    def test_zero_second_period_raises_provider_protocol_error(self) -> None:
        """PT0S period in period notation → ProviderProtocolError (zero-duration guard)."""
        from weewx_clearskies_api.providers._common.errors import ProviderProtocolError  # noqa: PLC0415
        from weewx_clearskies_api.providers._common.wms_capabilities import parse_wms_time_dimension  # noqa: PLC0415

        with pytest.raises(ProviderProtocolError):
            parse_wms_time_dimension(
                _ZERO_PERIOD_XML,
                layer="zero-period-layer",
                provider_id="test",
                domain="radar",
            )


# ===========================================================================
# 5. Period expansion arithmetic
# ===========================================================================


class TestPeriodExpansionArithmetic:
    """Verify period expansion arithmetic using controlled inline XML."""

    _PT5M_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<WMS_Capabilities version="1.3.0" xmlns="http://www.opengis.net/wms">
  <Capability>
    <Layer><Layer>
      <Name>pt5m-layer</Name>
      <Dimension name="time" units="ISO8601">2026-05-11T10:00:00Z/2026-05-11T10:30:00Z/PT5M</Dimension>
    </Layer></Layer>
  </Capability>
</WMS_Capabilities>
"""

    _PT1H_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<WMS_Capabilities version="1.3.0" xmlns="http://www.opengis.net/wms">
  <Capability>
    <Layer><Layer>
      <Name>pt1h-layer</Name>
      <Dimension name="time" units="ISO8601">2026-05-11T00:00:00Z/2026-05-11T03:00:00Z/PT1H</Dimension>
    </Layer></Layer>
  </Capability>
</WMS_Capabilities>
"""

    def test_pt5m_period_yields_seven_timestamps(self) -> None:
        """10:00 to 10:30 at PT5M → 7 timestamps (inclusive of both endpoints)."""
        from weewx_clearskies_api.providers._common.wms_capabilities import parse_wms_time_dimension  # noqa: PLC0415

        result = parse_wms_time_dimension(
            self._PT5M_XML,
            layer="pt5m-layer",
            provider_id="test",
            domain="radar",
        )
        assert len(result) == 7, f"Expected 7 timestamps for PT5M 30-min range; got {len(result)}"

    def test_pt5m_period_first_is_start(self) -> None:
        """PT5M expansion: first timestamp = 2026-05-11T10:00:00Z."""
        from weewx_clearskies_api.providers._common.wms_capabilities import parse_wms_time_dimension  # noqa: PLC0415

        result = parse_wms_time_dimension(self._PT5M_XML, layer="pt5m-layer", provider_id="test", domain="radar")
        assert result[0] == "2026-05-11T10:00:00Z"

    def test_pt5m_period_last_is_end(self) -> None:
        """PT5M expansion: last timestamp = 2026-05-11T10:30:00Z."""
        from weewx_clearskies_api.providers._common.wms_capabilities import parse_wms_time_dimension  # noqa: PLC0415

        result = parse_wms_time_dimension(self._PT5M_XML, layer="pt5m-layer", provider_id="test", domain="radar")
        assert result[-1] == "2026-05-11T10:30:00Z"

    def test_pt1h_period_yields_four_timestamps(self) -> None:
        """00:00 to 03:00 at PT1H → 4 timestamps (00:00, 01:00, 02:00, 03:00)."""
        from weewx_clearskies_api.providers._common.wms_capabilities import parse_wms_time_dimension  # noqa: PLC0415

        result = parse_wms_time_dimension(
            self._PT1H_XML,
            layer="pt1h-layer",
            provider_id="test",
            domain="radar",
        )
        assert len(result) == 4, f"Expected 4 timestamps for PT1H 3h range; got {len(result)}"

    def test_pt1h_period_second_timestamp_is_one_hour_after_first(self) -> None:
        """PT1H period: second timestamp is exactly 1 hour after first."""
        from weewx_clearskies_api.providers._common.wms_capabilities import parse_wms_time_dimension  # noqa: PLC0415

        result = parse_wms_time_dimension(self._PT1H_XML, layer="pt1h-layer", provider_id="test", domain="radar")
        assert result[0] == "2026-05-11T00:00:00Z"
        assert result[1] == "2026-05-11T01:00:00Z"
