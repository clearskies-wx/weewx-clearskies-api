"""Unit tests for providers/imagery/esri.py (Phase LM, §LM-1).

esri.py never makes an outbound HTTP call — it is a pure config-only
provider (mirrors the shape of radar/iframe.py's config-slot pattern).
These tests confirm the static shape only; no respx mocking is needed
because there is no HTTP client in this module at all.

Coverage:
  - CAPABILITY shape: provider_id, domain, geographic_coverage="global",
    tile_url_template, attribution.
  - get_config() returns the exact XYZ URL template + attribution text
    (KAT c).
  - Module never imports/instantiates ProviderHTTPClient (confirms the
    "config-only, browser-direct" design — no server-side proxy exists to
    accidentally fetch/cache ESRI bytes, which would violate §LM-1c/ToS).
"""

from __future__ import annotations


class TestEsriCapability:
    def test_capability_provider_id_is_esri(self) -> None:
        from weewx_clearskies_api.providers.imagery.esri import CAPABILITY  # noqa: PLC0415

        assert CAPABILITY.provider_id == "esri"

    def test_capability_domain_is_imagery(self) -> None:
        from weewx_clearskies_api.providers.imagery.esri import CAPABILITY  # noqa: PLC0415

        assert CAPABILITY.domain == "imagery"

    def test_capability_geographic_coverage_is_global(self) -> None:
        from weewx_clearskies_api.providers.imagery.esri import CAPABILITY  # noqa: PLC0415

        assert CAPABILITY.geographic_coverage == "global"

    def test_capability_no_auth_required(self) -> None:
        from weewx_clearskies_api.providers.imagery.esri import CAPABILITY  # noqa: PLC0415

        assert CAPABILITY.auth_required == ()

    def test_capability_tile_url_template_is_xyz_shape(self) -> None:
        from weewx_clearskies_api.providers.imagery.esri import CAPABILITY  # noqa: PLC0415

        assert CAPABILITY.tile_url_template is not None
        assert "{z}" in CAPABILITY.tile_url_template
        assert "{x}" in CAPABILITY.tile_url_template
        assert "{y}" in CAPABILITY.tile_url_template

    def test_capability_attribution_required_is_true(self) -> None:
        """Esri ToS requires attribution (§LM-1, non-commercial use terms)."""
        from weewx_clearskies_api.providers.imagery.esri import CAPABILITY  # noqa: PLC0415

        assert CAPABILITY.attribution is not None
        assert CAPABILITY.attribution.attribution_required is True

    def test_capability_supplied_canonical_fields_is_empty(self) -> None:
        """Imagery has no canonical-entity mapping — display-only (phase hard rule)."""
        from weewx_clearskies_api.providers.imagery.esri import CAPABILITY  # noqa: PLC0415

        assert len(CAPABILITY.supplied_canonical_fields) == 0


class TestEsriGetConfig:
    def test_get_config_returns_tile_url_and_attribution(self) -> None:
        from weewx_clearskies_api.providers.imagery.esri import (  # noqa: PLC0415
            ATTRIBUTION,
            TILE_URL_TEMPLATE,
            get_config,
        )

        result = get_config()
        assert result == {"tileUrl": TILE_URL_TEMPLATE, "attribution": ATTRIBUTION}

    def test_tile_url_template_matches_live_verified_esri_service(self) -> None:
        """Pinned 2026-08-02 against services.arcgisonline.com World_Imagery MapServer."""
        from weewx_clearskies_api.providers.imagery.esri import TILE_URL_TEMPLATE  # noqa: PLC0415

        assert TILE_URL_TEMPLATE == (
            "https://services.arcgisonline.com/arcgis/rest/services/"
            "World_Imagery/MapServer/tile/{z}/{y}/{x}"
        )

    def test_attribution_matches_live_verified_copyright_text(self) -> None:
        """Pinned verbatim from the live service's copyrightText (2026-08-02)."""
        from weewx_clearskies_api.providers.imagery.esri import ATTRIBUTION  # noqa: PLC0415

        assert ATTRIBUTION == (
            "Source: Esri, Vantor, Earthstar Geographics, and the GIS User Community"
        )


class TestEsriNeverFetchesUpstream:
    def test_module_has_no_http_client_symbol(self) -> None:
        """esri.py has no ProviderHTTPClient instance — pure config, no proxy."""
        import weewx_clearskies_api.providers.imagery.esri as esri_mod  # noqa: PLC0415

        assert not hasattr(esri_mod, "_http_client")
        assert not hasattr(esri_mod, "get_tile")
