"""Branding service — read operator branding config (ADR-022, Gap #10).

Reads branding values from BrandingSettings and SocialSettings objects
(populated from api.conf [branding] and [social] sections) and returns a
BrandingConfig model.
"""

from __future__ import annotations

from weewx_clearskies_api.config.settings import BrandingSettings, SocialSettings
from weewx_clearskies_api.models.responses import BrandingConfig, LogoBranding, SocialConfig


def get_branding(
    branding_settings: BrandingSettings,
    social_settings: SocialSettings | None = None,
) -> BrandingConfig:
    """Build a BrandingConfig from validated BrandingSettings and SocialSettings.

    Both settings objects have already been validated at startup, so no
    additional validation is needed here.

    Args:
        branding_settings: Validated BrandingSettings from api.conf [branding].
        social_settings: Validated SocialSettings from api.conf [social].
                         Defaults to empty SocialSettings when not provided.

    Returns:
        BrandingConfig with all configured branding and social values.
    """
    if social_settings is None:
        social_settings = SocialSettings({})

    # Build logo block only when at least a light URL is configured.
    logo: LogoBranding | None = None
    if branding_settings.logo_light_url:
        logo = LogoBranding(
            lightUrl=branding_settings.logo_light_url,
            darkUrl=branding_settings.logo_dark_url if branding_settings.logo_dark_url else None,
        )

    social = SocialConfig(
        facebook=social_settings.facebook_url,
        twitter=social_settings.twitter_url,
        instagram=social_settings.instagram_url,
        youtube=social_settings.youtube_url,
    )

    return BrandingConfig(
        accent=branding_settings.accent,  # type: ignore[arg-type]
        defaultThemeMode=branding_settings.default_theme_mode,  # type: ignore[arg-type]
        logo=logo,
        customCssUrl=branding_settings.custom_css_url,
        siteTitle=branding_settings.site_title,
        copyrightEntity=branding_settings.copyright_entity,
        faviconUrl=branding_settings.favicon_url,
        social=social,
    )
