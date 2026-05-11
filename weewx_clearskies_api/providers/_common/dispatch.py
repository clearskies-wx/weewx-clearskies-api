"""Provider module dispatch.

Maps (domain, provider_id) → the provider module's CAPABILITY + fetch entrypoint.
Phase-2 simple: explicit dict, NOT entry-points (per ADR-038 §Internal contract —
no runtime plugin loading; outside contributors PR into the bundled set).

Adding a new provider = importing the new module and adding one row here.
Aeris alerts wired in 3b round 7; OWM alerts wired in 3b round 8 (third and FINAL
day-1 alerts provider per ADR-016).
Forecast domain: five rows (one per provider per ADR-007 day-1 set).
  Wired: openmeteo (3b-2), nws (3b-3), aeris (3b-4), openweathermap (3b-5),
  wunderground (3b-6 — fifth and FINAL day-1 forecast provider).
AQI domain: openmeteo (3b-9), aeris (3b-10), openweathermap (3b-11), iqair (3b-12 — fourth + FINAL day-1 AQI provider).
"""

from __future__ import annotations

from types import ModuleType

from weewx_clearskies_api.providers.alerts import aeris as alerts_aeris
from weewx_clearskies_api.providers.alerts import nws as alerts_nws
from weewx_clearskies_api.providers.alerts import openweathermap as alerts_openweathermap
from weewx_clearskies_api.providers.aqi import aeris as aqi_aeris
from weewx_clearskies_api.providers.aqi import iqair as aqi_iqair
from weewx_clearskies_api.providers.aqi import openmeteo as aqi_openmeteo
from weewx_clearskies_api.providers.aqi import openweathermap as aqi_openweathermap
from weewx_clearskies_api.providers.forecast import aeris as forecast_aeris
from weewx_clearskies_api.providers.forecast import nws as forecast_nws
from weewx_clearskies_api.providers.forecast import openmeteo as forecast_openmeteo
from weewx_clearskies_api.providers.forecast import openweathermap as forecast_openweathermap
from weewx_clearskies_api.providers.forecast import wunderground as forecast_wunderground

PROVIDER_MODULES: dict[tuple[str, str], ModuleType] = {
    ("alerts", "aeris"): alerts_aeris,
    ("alerts", "nws"): alerts_nws,
    ("alerts", "openweathermap"): alerts_openweathermap,
    ("aqi", "aeris"): aqi_aeris,
    ("aqi", "iqair"): aqi_iqair,
    ("aqi", "openmeteo"): aqi_openmeteo,
    ("aqi", "openweathermap"): aqi_openweathermap,
    ("forecast", "openmeteo"): forecast_openmeteo,
    ("forecast", "nws"): forecast_nws,
    ("forecast", "aeris"): forecast_aeris,
    ("forecast", "openweathermap"): forecast_openweathermap,
    ("forecast", "wunderground"): forecast_wunderground,
}


def get_provider_module(*, domain: str, provider_id: str) -> ModuleType:
    """Return the provider module by (domain, provider_id).

    Args:
        domain: Provider domain e.g. "alerts", "forecast".
        provider_id: Provider id e.g. "nws", "aeris".

    Returns:
        The provider module (has CAPABILITY symbol and fetch() callable).

    Raises:
        KeyError: Unknown (domain, provider_id) pair.
    """
    key = (domain, provider_id)
    if key not in PROVIDER_MODULES:
        raise KeyError(
            f"Unknown provider: domain={domain!r}, provider_id={provider_id!r}. "
            f"Known providers: {sorted(PROVIDER_MODULES.keys())}"
        )
    return PROVIDER_MODULES[key]
