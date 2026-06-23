"""Unit tests for weewx_clearskies_api.bootstrap.openaq_client (Phase 9 T9.8).

Tests cover the smart sensor-selection additions introduced in Phase 9:
  - find_best_pm25_sensor() ranked list, 12-month data-age filter, empty results
  - get_nearby_sensors() returns all sensors without the age filter
  - _query_nearby_pm25_locations() passes isMonitor=true in query params
  - _haversine_km() correctness with known real-world coordinates
  - _location_to_sensor_dicts() extracts PM2.5 sensors, skips non-PM2.5

All OpenAQ HTTP calls are intercepted by patching _api_get so no real network
requests are made.  Rate-limit sleep is not patched — callers of _api_get
already pass through the module's sleep, but mocking _api_get means the sleep
in the real function is never reached, so tests run instantly.
"""

from __future__ import annotations

import math
from datetime import date
from unittest.mock import patch

import pytest

from weewx_clearskies_api.bootstrap import openaq_client


# ---------------------------------------------------------------------------
# Realistic fixture data shapes
# ---------------------------------------------------------------------------

# Station coordinates: fictional site near downtown Los Angeles.
_STATION_LAT = 34.052
_STATION_LON = -118.244

# Three monitor locations at increasing distance from the station.
# Coords chosen so haversine produces roughly 2 km, 8 km, 15 km.
_LOC_CLOSE = {
    "id": 1001,
    "name": "Downtown LA Monitor",
    "locality": "Los Angeles",
    "coordinates": {"latitude": 34.070, "longitude": -118.244},
    "datetimeFirst": {"utc": "2022-01-01T00:00:00Z", "local": "..."},
    "datetimeLast": {"utc": "2025-06-01T00:00:00Z", "local": "..."},
    "sensors": [
        {
            "id": 9901,
            "parameter": {"name": "pm25", "displayName": "PM2.5"},
        }
    ],
}

_LOC_MID = {
    "id": 1002,
    "name": "Pasadena Reference Station",
    "locality": "Pasadena",
    "coordinates": {"latitude": 34.120, "longitude": -118.150},
    "datetimeFirst": {"utc": "2021-06-15T00:00:00Z", "local": "..."},
    "datetimeLast": {"utc": "2025-05-01T00:00:00Z", "local": "..."},
    "sensors": [
        {
            "id": 9902,
            "parameter": {"name": "pm25", "displayName": "PM2.5"},
        },
        {
            # Non-PM2.5 sensor on the same location — must be ignored.
            "id": 9903,
            "parameter": {"name": "no2", "displayName": "NO₂"},
        },
    ],
}

_LOC_FAR = {
    "id": 1003,
    "name": "Santa Monica Coastal",
    "locality": "Santa Monica",
    "coordinates": {"latitude": 34.020, "longitude": -118.490},
    "datetimeFirst": {"utc": "2022-03-01T00:00:00Z", "local": "..."},
    "datetimeLast": {"utc": "2025-04-01T00:00:00Z", "local": "..."},
    "sensors": [
        {
            "id": 9904,
            "parameter": {"name": "pm25", "displayName": "PM2.5"},
        }
    ],
}

# A monitor whose data span is only 6 months — should be excluded by find_best
# but included by get_nearby_sensors.
_LOC_SHORT_SPAN = {
    "id": 1004,
    "name": "Inglewood Short-Span Monitor",
    "locality": "Inglewood",
    "coordinates": {"latitude": 33.960, "longitude": -118.350},
    "datetimeFirst": {"utc": "2024-01-01T00:00:00Z", "local": "..."},
    "datetimeLast": {"utc": "2024-07-01T00:00:00Z", "local": "..."},  # 6 months
    "sensors": [
        {
            "id": 9905,
            "parameter": {"name": "pm25", "displayName": "PM2.5"},
        }
    ],
}

# A response where every sensor lacks PM2.5.
_LOC_NO_PM25 = {
    "id": 1005,
    "name": "Air Toxics Monitor (VOCs only)",
    "locality": "Burbank",
    "coordinates": {"latitude": 34.180, "longitude": -118.310},
    "datetimeFirst": {"utc": "2020-01-01T00:00:00Z", "local": "..."},
    "datetimeLast": {"utc": "2025-01-01T00:00:00Z", "local": "..."},
    "sensors": [
        {"id": 9906, "parameter": {"name": "benzene", "displayName": "Benzene"}},
        {"id": 9907, "parameter": {"name": "co", "displayName": "CO"}},
    ],
}


def _make_api_response(locations: list[dict]) -> dict:
    """Build a minimal OpenAQ v3 /locations response dict."""
    return {
        "meta": {"found": len(locations), "page": 1, "limit": 100},
        "results": locations,
    }


# ---------------------------------------------------------------------------
# Helper: collect the params that _api_get was called with
# ---------------------------------------------------------------------------

def _capture_api_get(response: dict):
    """Return a mock _api_get that records (path, params) calls and returns response."""
    calls: list[tuple[str, dict | None]] = []

    def _mock(path: str, params: dict | None = None) -> dict:
        calls.append((path, params))
        return response

    _mock.calls = calls  # type: ignore[attr-defined]
    return _mock


# ===========================================================================
# Group 1: _haversine_km() — known real-world distances
# ===========================================================================


class TestHaversineKm:
    """_haversine_km() returns correct great-circle distances."""

    def test_same_point_returns_zero(self) -> None:
        """Identical coordinates → distance is 0.0 km."""
        result = openaq_client._haversine_km(34.0, -118.0, 34.0, -118.0)
        assert result == pytest.approx(0.0, abs=1e-9)

    def test_la_to_long_beach_approximately_30km(self) -> None:
        """Downtown LA (34.052, -118.244) to Long Beach City Hall (33.770, -118.193).

        Real road distance is ~40 km, great-circle is ~32 km.  Verify within ±5 km.
        """
        result = openaq_client._haversine_km(34.052, -118.244, 33.770, -118.193)
        assert 27.0 <= result <= 37.0, (
            f"Expected ~32 km LA→Long Beach great-circle, got {result:.2f} km"
        )

    def test_nyc_to_london_approximately_5570km(self) -> None:
        """NYC (40.713, -74.006) to London (51.507, -0.128) great-circle ~5570 km."""
        result = openaq_client._haversine_km(40.713, -74.006, 51.507, -0.128)
        assert 5400.0 <= result <= 5700.0, (
            f"Expected ~5570 km NYC→London, got {result:.1f} km"
        )

    def test_antipodal_points_close_to_half_earth_circumference(self) -> None:
        """Antipodal points are ~20,015 km apart (half Earth's circumference)."""
        result = openaq_client._haversine_km(0.0, 0.0, 0.0, 180.0)
        # Earth circumference ~40,075 km → half ~20,038 km
        assert 19_900.0 <= result <= 20_100.0, (
            f"Antipodal distance should be ~20,015 km, got {result:.1f} km"
        )

    def test_result_is_symmetric(self) -> None:
        """Distance A→B equals distance B→A."""
        d_ab = openaq_client._haversine_km(34.052, -118.244, 34.120, -118.150)
        d_ba = openaq_client._haversine_km(34.120, -118.150, 34.052, -118.244)
        assert d_ab == pytest.approx(d_ba, rel=1e-9)

    def test_uses_correct_earth_radius(self) -> None:
        """Distance along 1° latitude arc should be ~111.2 km (Earth radius 6371 km)."""
        # 1° of latitude ≈ 2π * 6371 / 360 = 111.2 km
        result = openaq_client._haversine_km(0.0, 0.0, 1.0, 0.0)
        expected_km = 2 * math.pi * 6371.0 / 360.0
        assert result == pytest.approx(expected_km, rel=0.001)


# ===========================================================================
# Group 2: find_best_pm25_sensor() — ranked list and age filter
# ===========================================================================


class TestFindBestPm25Sensor:
    """find_best_pm25_sensor() filters by 12-month data span and sorts by distance."""

    def test_three_qualifying_sensors_sorted_by_distance(self) -> None:
        """Three locations all with > 12 months data → returned sorted by distance_km."""
        mock_response = _make_api_response([_LOC_CLOSE, _LOC_MID, _LOC_FAR])

        with patch.object(openaq_client, "_api_get", return_value=mock_response):
            result = openaq_client.find_best_pm25_sensor(_STATION_LAT, _STATION_LON)

        # All three locations have exactly one PM2.5 sensor each (MID has two sensors
        # but only one PM2.5), so we get three sensor dicts.
        assert len(result) == 3

        distances = [s["distance_km"] for s in result]
        assert distances == sorted(distances), (
            "Sensors must be returned sorted by distance_km ascending"
        )

    def test_returned_dict_has_required_keys(self) -> None:
        """Each returned sensor dict has all required keys."""
        required_keys = {
            "sensor_id", "location_id", "name", "lat", "lon",
            "distance_km", "datetime_first", "datetime_last", "is_monitor",
        }
        mock_response = _make_api_response([_LOC_CLOSE])

        with patch.object(openaq_client, "_api_get", return_value=mock_response):
            result = openaq_client.find_best_pm25_sensor(_STATION_LAT, _STATION_LON)

        assert len(result) == 1
        assert required_keys.issubset(result[0].keys()), (
            f"Sensor dict missing keys: {required_keys - result[0].keys()!r}"
        )

    def test_is_monitor_always_true(self) -> None:
        """All returned sensors have is_monitor=True (only isMonitor=true queried)."""
        mock_response = _make_api_response([_LOC_CLOSE, _LOC_MID, _LOC_FAR])

        with patch.object(openaq_client, "_api_get", return_value=mock_response):
            result = openaq_client.find_best_pm25_sensor(_STATION_LAT, _STATION_LON)

        for sensor in result:
            assert sensor["is_monitor"] is True, (
                f"Sensor {sensor['sensor_id']} must have is_monitor=True"
            )

    def test_short_span_sensor_excluded(self) -> None:
        """Sensor with only 6 months of data is excluded from find_best results."""
        # Mix: 3 qualifying + 1 short-span
        mock_response = _make_api_response(
            [_LOC_CLOSE, _LOC_MID, _LOC_FAR, _LOC_SHORT_SPAN]
        )
        sensor_ids_in = {9901, 9902, 9904}  # the qualifying ones
        sensor_id_excluded = 9905            # the 6-month one

        with patch.object(openaq_client, "_api_get", return_value=mock_response):
            result = openaq_client.find_best_pm25_sensor(_STATION_LAT, _STATION_LON)

        returned_ids = {s["sensor_id"] for s in result}
        assert sensor_id_excluded not in returned_ids, (
            "Sensor with only 6 months of data must be excluded from find_best results"
        )
        assert sensor_ids_in.issubset(returned_ids), (
            "Sensors with > 12 months of data must all be included"
        )

    def test_empty_response_returns_empty_list(self) -> None:
        """Empty locations response → returns [] without raising."""
        mock_response = _make_api_response([])

        with patch.object(openaq_client, "_api_get", return_value=mock_response):
            result = openaq_client.find_best_pm25_sensor(_STATION_LAT, _STATION_LON)

        assert result == [], (
            "find_best_pm25_sensor must return [] when no locations found, not raise"
        )

    def test_location_with_no_pm25_sensors_excluded(self) -> None:
        """Location whose sensors are all non-PM2.5 contributes nothing to results."""
        mock_response = _make_api_response([_LOC_NO_PM25, _LOC_CLOSE])

        with patch.object(openaq_client, "_api_get", return_value=mock_response):
            result = openaq_client.find_best_pm25_sensor(_STATION_LAT, _STATION_LON)

        returned_ids = {s["sensor_id"] for s in result}
        # VOC-only sensors must not appear
        assert 9906 not in returned_ids
        assert 9907 not in returned_ids
        # PM2.5 sensor from _LOC_CLOSE must appear
        assert 9901 in returned_ids

    def test_missing_datetime_fields_does_not_exclude_sensor(self) -> None:
        """Sensor whose datetimeFirst/datetimeLast are absent is included (no filter applied)."""
        loc_no_dates = {
            "id": 2001,
            "name": "Undated Monitor",
            "locality": "Culver City",
            "coordinates": {"latitude": 34.000, "longitude": -118.400},
            # No datetimeFirst / datetimeLast keys
            "sensors": [
                {"id": 8801, "parameter": {"name": "pm25", "displayName": "PM2.5"}}
            ],
        }
        mock_response = _make_api_response([loc_no_dates])

        with patch.object(openaq_client, "_api_get", return_value=mock_response):
            result = openaq_client.find_best_pm25_sensor(_STATION_LAT, _STATION_LON)

        assert any(s["sensor_id"] == 8801 for s in result), (
            "Sensor without date fields must be included — cannot apply span filter"
        )

    def test_sensor_id_and_location_id_are_integers(self) -> None:
        """sensor_id and location_id in returned dicts are Python ints."""
        mock_response = _make_api_response([_LOC_CLOSE])

        with patch.object(openaq_client, "_api_get", return_value=mock_response):
            result = openaq_client.find_best_pm25_sensor(_STATION_LAT, _STATION_LON)

        assert len(result) >= 1
        for s in result:
            assert isinstance(s["sensor_id"], int), "sensor_id must be int"
            assert isinstance(s["location_id"], int), "location_id must be int"

    def test_distance_km_rounded_to_3_decimal_places(self) -> None:
        """distance_km values are rounded to 3 decimal places."""
        mock_response = _make_api_response([_LOC_CLOSE])

        with patch.object(openaq_client, "_api_get", return_value=mock_response):
            result = openaq_client.find_best_pm25_sensor(_STATION_LAT, _STATION_LON)

        for s in result:
            d = s["distance_km"]
            assert round(d, 3) == d, (
                f"distance_km {d} has more than 3 decimal places"
            )

    def test_only_qualifying_sensor_returned_when_mixed_data_ages(self) -> None:
        """When one sensor qualifies and one does not, only the qualifier is returned."""
        mock_response = _make_api_response([_LOC_CLOSE, _LOC_SHORT_SPAN])

        with patch.object(openaq_client, "_api_get", return_value=mock_response):
            result = openaq_client.find_best_pm25_sensor(_STATION_LAT, _STATION_LON)

        assert len(result) == 1
        assert result[0]["sensor_id"] == 9901


# ===========================================================================
# Group 3: get_nearby_sensors() — no age filter applied
# ===========================================================================


class TestGetNearbySensors:
    """get_nearby_sensors() returns all PM2.5 sensors including short-span ones."""

    def test_returns_all_sensors_including_short_span(self) -> None:
        """Short-span sensor included in get_nearby_sensors (no age filter)."""
        mock_response = _make_api_response(
            [_LOC_CLOSE, _LOC_MID, _LOC_FAR, _LOC_SHORT_SPAN]
        )

        with patch.object(openaq_client, "_api_get", return_value=mock_response):
            result = openaq_client.get_nearby_sensors(_STATION_LAT, _STATION_LON)

        returned_ids = {s["sensor_id"] for s in result}
        assert 9905 in returned_ids, (
            "Short-span sensor (9905) must appear in get_nearby_sensors results"
        )

    def test_returns_more_sensors_than_find_best(self) -> None:
        """get_nearby_sensors returns >= sensors than find_best for same input."""
        mock_response = _make_api_response(
            [_LOC_CLOSE, _LOC_MID, _LOC_FAR, _LOC_SHORT_SPAN]
        )

        with patch.object(openaq_client, "_api_get", return_value=mock_response):
            best = openaq_client.find_best_pm25_sensor(_STATION_LAT, _STATION_LON)
            nearby = openaq_client.get_nearby_sensors(_STATION_LAT, _STATION_LON)

        assert len(nearby) >= len(best), (
            "get_nearby_sensors must return at least as many sensors as find_best"
        )

    def test_get_nearby_sorted_by_distance_ascending(self) -> None:
        """get_nearby_sensors returns sensors sorted by distance_km ascending."""
        mock_response = _make_api_response([_LOC_FAR, _LOC_CLOSE, _LOC_MID])

        with patch.object(openaq_client, "_api_get", return_value=mock_response):
            result = openaq_client.get_nearby_sensors(_STATION_LAT, _STATION_LON)

        distances = [s["distance_km"] for s in result]
        assert distances == sorted(distances), (
            "get_nearby_sensors must return sensors sorted by distance_km ascending"
        )

    def test_empty_response_returns_empty_list(self) -> None:
        """Empty API response → returns [] without raising."""
        mock_response = _make_api_response([])

        with patch.object(openaq_client, "_api_get", return_value=mock_response):
            result = openaq_client.get_nearby_sensors(_STATION_LAT, _STATION_LON)

        assert result == []

    def test_returned_dict_has_required_keys(self) -> None:
        """Each sensor dict from get_nearby_sensors has all required keys."""
        required_keys = {
            "sensor_id", "location_id", "name", "lat", "lon",
            "distance_km", "datetime_first", "datetime_last", "is_monitor",
        }
        mock_response = _make_api_response([_LOC_SHORT_SPAN])

        with patch.object(openaq_client, "_api_get", return_value=mock_response):
            result = openaq_client.get_nearby_sensors(_STATION_LAT, _STATION_LON)

        assert len(result) == 1
        assert required_keys.issubset(result[0].keys()), (
            f"Sensor dict missing keys: {required_keys - result[0].keys()!r}"
        )


# ===========================================================================
# Group 4: _query_nearby_pm25_locations() — query params include isMonitor=true
# ===========================================================================


class TestQueryNearbyPm25LocationsParams:
    """_query_nearby_pm25_locations() passes correct query parameters to _api_get."""

    def test_ismonitor_param_is_true_string(self) -> None:
        """Query params must include isMonitor='true' to filter reference monitors only."""
        mock = _capture_api_get(_make_api_response([]))

        with patch.object(openaq_client, "_api_get", side_effect=mock):
            openaq_client._query_nearby_pm25_locations(_STATION_LAT, _STATION_LON)

        assert len(mock.calls) == 1, "Expected exactly one _api_get call for empty response"
        _path, params = mock.calls[0]
        assert params is not None, "_query_nearby_pm25_locations must pass query params"
        assert "isMonitor" in params, "Query params must include 'isMonitor'"
        assert params["isMonitor"] == "true", (
            "isMonitor must be the string 'true' to trigger reference-monitor filter"
        )

    def test_coordinates_param_matches_lat_lon(self) -> None:
        """coordinates param is formatted as 'lat,lon'."""
        mock = _capture_api_get(_make_api_response([]))

        with patch.object(openaq_client, "_api_get", side_effect=mock):
            openaq_client._query_nearby_pm25_locations(34.052, -118.244)

        _path, params = mock.calls[0]
        assert params["coordinates"] == "34.052,-118.244"

    def test_radius_param_is_25000(self) -> None:
        """radius param is 25000 (25 km in metres, per OpenAQ docs)."""
        mock = _capture_api_get(_make_api_response([]))

        with patch.object(openaq_client, "_api_get", side_effect=mock):
            openaq_client._query_nearby_pm25_locations(_STATION_LAT, _STATION_LON)

        _path, params = mock.calls[0]
        assert params["radius"] == 25000

    def test_path_is_locations_endpoint(self) -> None:
        """_api_get is called with path '/locations'."""
        mock = _capture_api_get(_make_api_response([]))

        with patch.object(openaq_client, "_api_get", side_effect=mock):
            openaq_client._query_nearby_pm25_locations(_STATION_LAT, _STATION_LON)

        path, _params = mock.calls[0]
        assert path == "/locations"

    def test_pagination_fetches_second_page_when_results_incomplete(self) -> None:
        """When meta.found > results returned, a second page is fetched."""
        # Page 1 returns 1 location but claims 2 found.
        page1_response = {
            "meta": {"found": 2, "page": 1, "limit": 100},
            "results": [_LOC_CLOSE],
        }
        page2_response = {
            "meta": {"found": 2, "page": 2, "limit": 100},
            "results": [_LOC_MID],
        }
        responses = [page1_response, page2_response]
        call_count = {"n": 0}

        def _multi_page(path: str, params: dict | None = None) -> dict:
            r = responses[call_count["n"]]
            call_count["n"] += 1
            return r

        with patch.object(openaq_client, "_api_get", side_effect=_multi_page):
            result = openaq_client._query_nearby_pm25_locations(_STATION_LAT, _STATION_LON)

        assert call_count["n"] == 2, "Must fetch page 2 when meta.found > page 1 results"
        assert len(result) == 2, "Must return locations from both pages"


# ===========================================================================
# Group 5: _location_to_sensor_dicts() — PM2.5 filter, multi-sensor locations
# ===========================================================================


class TestLocationToSensorDicts:
    """_location_to_sensor_dicts() extracts PM2.5 sensors and ignores others."""

    def test_non_pm25_sensors_skipped(self) -> None:
        """Location with NO2 + PM2.5 sensors returns only the PM2.5 sensor."""
        result = openaq_client._location_to_sensor_dicts(
            _LOC_MID, _STATION_LAT, _STATION_LON
        )
        returned_ids = {s["sensor_id"] for s in result}
        assert 9903 not in returned_ids, "NO2 sensor must not appear in PM2.5 results"
        assert 9902 in returned_ids, "PM2.5 sensor must appear"

    def test_only_one_pm25_sensor_from_mixed_location(self) -> None:
        """Location with PM2.5 + NO2 → only 1 sensor dict returned."""
        result = openaq_client._location_to_sensor_dicts(
            _LOC_MID, _STATION_LAT, _STATION_LON
        )
        assert len(result) == 1

    def test_all_sensors_non_pm25_returns_empty(self) -> None:
        """Location with only non-PM2.5 sensors returns empty list."""
        result = openaq_client._location_to_sensor_dicts(
            _LOC_NO_PM25, _STATION_LAT, _STATION_LON
        )
        assert result == []

    def test_sensor_dict_distance_km_is_positive_float(self) -> None:
        """distance_km in returned dict is a positive float for non-coincident points."""
        result = openaq_client._location_to_sensor_dicts(
            _LOC_FAR, _STATION_LAT, _STATION_LON
        )
        assert len(result) == 1
        d = result[0]["distance_km"]
        assert isinstance(d, float)
        assert d > 0.0

    def test_datetime_fields_parse_utc_dict_format(self) -> None:
        """datetimeFirst/datetimeLast in {'utc': '...'} format produce ISO date strings."""
        result = openaq_client._location_to_sensor_dicts(
            _LOC_CLOSE, _STATION_LAT, _STATION_LON
        )
        assert len(result) == 1
        assert result[0]["datetime_first"] == "2022-01-01"
        assert result[0]["datetime_last"] == "2025-06-01"

    def test_sensor_without_id_is_skipped(self) -> None:
        """PM2.5 sensor missing the 'id' field is silently skipped."""
        loc_no_id = {
            "id": 3001,
            "name": "Broken Sensor Location",
            "locality": "Hawthorne",
            "coordinates": {"latitude": 33.900, "longitude": -118.330},
            "sensors": [
                # id is missing
                {"parameter": {"name": "pm25", "displayName": "PM2.5"}},
                # this one has an id — should be returned
                {"id": 7701, "parameter": {"name": "pm25", "displayName": "PM2.5"}},
            ],
        }
        result = openaq_client._location_to_sensor_dicts(
            loc_no_id, _STATION_LAT, _STATION_LON
        )
        returned_ids = {s["sensor_id"] for s in result}
        assert 7701 in returned_ids
        assert len(result) == 1, "Sensor without id must be silently skipped"

    def test_pm25_matched_by_display_name(self) -> None:
        """PM2.5 sensor matched by displayName 'PM2.5' (not just 'pm25' in name)."""
        loc_display_name = {
            "id": 3002,
            "name": "AQS Display-Name Monitor",
            "locality": "Compton",
            "coordinates": {"latitude": 33.890, "longitude": -118.220},
            "sensors": [
                {
                    "id": 7702,
                    # name field is empty; displayName contains "PM2.5"
                    "parameter": {"name": "", "displayName": "PM2.5"},
                }
            ],
        }
        result = openaq_client._location_to_sensor_dicts(
            loc_display_name, _STATION_LAT, _STATION_LON
        )
        assert any(s["sensor_id"] == 7702 for s in result), (
            "PM2.5 sensor must be matched by displayName 'PM2.5'"
        )
