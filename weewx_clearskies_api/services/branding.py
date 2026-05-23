"""Branding service — read operator branding config (ADR-022, Gap #10).

Reads branding values from the BrandingSettings object (populated from api.conf
[branding] section) and returns a BrandingConfig model.

v0.1 scope: accent, defaultThemeMode, and customCssUrl only.  Logo is always
None — the upload pipeline ships in Phase 2 per ADR-022 §out-of-scope.
"""

from __future__ import annotations

from weewx_clearskies_api.config.settings import BrandingSettings
from weewx_clearskies_api.models.responses import BrandingConfig


def get_branding(branding_settings: BrandingSettings) -> BrandingConfig:
    """Build a BrandingConfig from the validated BrandingSettings.

    The BrandingSettings object has already been validated at startup
    (accent and default_theme_mode are in their allowed sets), so no
    additional validation is needed here.

    Args:
        branding_settings: Validated BrandingSettings from api.conf [branding].

    Returns:
        BrandingConfig with values from api.conf and None for logo (v0.1).
    """
    return BrandingConfig(
        accent=branding_settings.accent,  # type: ignore[arg-type]
        defaultThemeMode=branding_settings.default_theme_mode,  # type: ignore[arg-type]
        logo=None,  # v0.1: no logo upload pipeline (Phase 2 per ADR-022)
        customCssUrl=branding_settings.custom_css_url,
    )
