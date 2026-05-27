"""Branding endpoint (ADR-022, Gap #10).

GET /branding — returns operator branding configuration read from api.conf
[branding] and [social] sections.  Used by the dashboard at boot to set accent
colors, default theme mode, site title, logos, favicon, and social URLs.

No query params. No DB dependency. No auth required (public endpoint per
ADR-018 — same access model as /station and /capabilities).

Wire pattern: same module-level state + wire_*() approach as pages.py,
earthquakes.py, etc.  Called from __main__.py step 6.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter

from weewx_clearskies_api.config.settings import BrandingSettings, SocialSettings
from weewx_clearskies_api.models.responses import BrandingResponse, utc_isoformat
from weewx_clearskies_api.services.branding import get_branding

logger = logging.getLogger(__name__)

router = APIRouter()

# Module-level settings — populated at startup from api.conf.
_branding_settings: BrandingSettings = BrandingSettings({})
_social_settings: SocialSettings = SocialSettings({})


def wire_branding_settings(settings_obj: BrandingSettings) -> None:
    """Set the branding settings from api.conf [branding].  Called from __main__.py."""
    global _branding_settings  # noqa: PLW0603
    _branding_settings = settings_obj


def wire_social_settings(settings_obj: SocialSettings) -> None:
    """Set the social settings from api.conf [social].  Called from __main__.py."""
    global _social_settings  # noqa: PLW0603
    _social_settings = settings_obj


@router.get("/branding", summary="Station branding configuration", tags=["Branding"])
def get_branding_config() -> BrandingResponse:
    """Return operator branding configuration.

    Values come from api.conf [branding] and [social] sections, validated at
    startup.  Defaults: accent=blue, defaultThemeMode=auto-os, no logo, no
    custom CSS, empty social URLs.
    """
    branding = get_branding(_branding_settings, _social_settings)
    return BrandingResponse(
        data=branding,
        generatedAt=utc_isoformat(datetime.now(tz=UTC)),
    )
