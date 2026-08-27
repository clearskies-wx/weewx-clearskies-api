"""Unit tests for providers/imagery/esri_topo.py (Round IMAGERY-MAP, 2026-08-26).

esri_topo.py never makes an outbound HTTP call — it is a pure config-only
provider, cloned from the exact shape of providers/imagery/esri.py. These
tests confirm the static shape only; no respx mocking is needed because
there is no HTTP client in this module at all.

Coverage:
  - CAPABILITY shape: provider_id="map", domain, geographic_coverage="global",
    tile_url_template, attribution.
  - get_config() returns the exact World_Topo_Map XYZ URL template + pinned
    attribution text (KAT: provider "map" -> config carries the
    World_Topo_Map template + pinned attribution).
  - Module never imports/instantiates ProviderHTTPClient (confirms the
    "config-only, browser-direct" design — no server-side proxy exists to
    accidentally fetch/cache Esri tile bytes).
  - Mutation-style check: the "map" config differs from "esri" ONLY in
    tileUrl/attribution (same proxyMode="direct" shape, same CAPABILITY
    fields elsewhere).
"""

from __future__ import annotations


class TestEsriTopoCapability:
    def test_capability_provider_id_is_map(self) -> None:
        from weewx_clearskies_api.providers.imagery.esri_topo import CAPABILITY  # noqa: PLC0415

        assert CAPABILITY.provider_id == "map"

    def test_capability_domain_is_imagery(self) -> None:
        from weewx_clearskies_api.providers.imagery.esri_topo import CAPABILITY  # noqa: PLC0415

        assert CAPABILITY.domain == "imagery"

    def test_capability_geographic_coverage_is_global(self) -> None:
        from weewx_clearskies_api.providers.imagery.esri_topo import CAPABILITY  # noqa: PLC0415

        assert CAPABILITY.geographic_coverage == "global"

    def test_capability_no_auth_required(self) -> None:
        from weewx_clearskies_api.providers.imagery.esri_topo import CAPABILITY  # noqa: PLC0415

        assert CAPABILITY.auth_required == ()

    def test_capability_tile_url_template_is_xyz_shape(self) -> None:
        from weewx_clearskies_api.providers.imagery.esri_topo import CAPABILITY  # noqa: PLC0415

        assert CAPABILITY.tile_url_template is not None
        assert "{z}" in CAPABILITY.tile_url_template
        assert "{x}" in CAPABILITY.tile_url_template
        assert "{y}" in CAPABILITY.tile_url_template

    def test_capability_attribution_required_is_true(self) -> None:
        """Esri ToS requires attribution (same posture as esri.py)."""
        from weewx_clearskies_api.providers.imagery.esri_topo import CAPABILITY  # noqa: PLC0415

        assert CAPABILITY.attribution is not None
        assert CAPABILITY.attribution.attribution_required is True

    def test_capability_supplied_canonical_fields_is_empty(self) -> None:
        """Imagery has no canonical-entity mapping — display-only (phase hard rule)."""
        from weewx_clearskies_api.providers.imagery.esri_topo import CAPABILITY  # noqa: PLC0415

        assert len(CAPABILITY.supplied_canonical_fields) == 0


class TestEsriTopoGetConfig:
    def test_get_config_returns_tile_url_and_attribution(self) -> None:
        from weewx_clearskies_api.providers.imagery.esri_topo import (  # noqa: PLC0415
            ATTRIBUTION,
            TILE_URL_TEMPLATE,
            get_config,
        )

        result = get_config()
        assert result == {"tileUrl": TILE_URL_TEMPLATE, "attribution": ATTRIBUTION}

    def test_tile_url_template_matches_live_verified_world_topo_map_service(self) -> None:
        """Pinned 2026-08-26 against services.arcgisonline.com World_Topo_Map MapServer."""
        from weewx_clearskies_api.providers.imagery.esri_topo import TILE_URL_TEMPLATE  # noqa: PLC0415

        assert TILE_URL_TEMPLATE == (
            "https://services.arcgisonline.com/arcgis/rest/services/"
            "World_Topo_Map/MapServer/tile/{z}/{y}/{x}"
        )

    def test_attribution_matches_live_verified_copyright_text(self) -> None:
        """Pinned verbatim from the live service's copyrightText (2026-08-26)."""
        from weewx_clearskies_api.providers.imagery.esri_topo import ATTRIBUTION  # noqa: PLC0415

        assert ATTRIBUTION == (
            "Sources: Esri, HERE, Garmin, Intermap, increment P Corp., GEBCO, USGS, FAO, NPS, "
            "NRCAN, GeoBase, IGN, Kadaster NL, Ordnance Survey, Esri Japan, METI, Esri China "
            "(Hong Kong), (c) OpenStreetMap contributors, and the GIS User Community"
        )


class TestEsriTopoNeverFetchesUpstream:
    def test_module_has_no_http_client_symbol(self) -> None:
        """esri_topo.py has no ProviderHTTPClient instance — pure config, no proxy."""
        import weewx_clearskies_api.providers.imagery.esri_topo as esri_topo_mod  # noqa: PLC0415

        assert not hasattr(esri_topo_mod, "_http_client")
        assert not hasattr(esri_topo_mod, "get_tile")


class TestEsriTopoDiffersFromEsriOnlyInTileUrlAndAttribution:
    """Mutation-style check (brief §Verification): the "map" config response
    differs from "esri" ONLY in tileUrl/attribution."""

    def test_get_config_shape_matches_esri_except_url_and_attribution(self) -> None:
        from weewx_clearskies_api.providers.imagery import esri, esri_topo  # noqa: PLC0415

        esri_config = esri.get_config()
        topo_config = esri_topo.get_config()

        assert set(esri_config.keys()) == set(topo_config.keys()) == {"tileUrl", "attribution"}
        assert esri_config["tileUrl"] != topo_config["tileUrl"]
        assert esri_config["attribution"] != topo_config["attribution"]

    def test_capability_shape_matches_esri_except_provider_id_and_tile_fields(self) -> None:
        from weewx_clearskies_api.providers.imagery import esri, esri_topo  # noqa: PLC0415

        assert esri.CAPABILITY.domain == esri_topo.CAPABILITY.domain
        assert esri.CAPABILITY.geographic_coverage == esri_topo.CAPABILITY.geographic_coverage
        assert esri.CAPABILITY.auth_required == esri_topo.CAPABILITY.auth_required
        assert (
            esri.CAPABILITY.supplied_canonical_fields
            == esri_topo.CAPABILITY.supplied_canonical_fields
        )
        assert esri.CAPABILITY.provider_id != esri_topo.CAPABILITY.provider_id
        assert esri.CAPABILITY.tile_url_template != esri_topo.CAPABILITY.tile_url_template
