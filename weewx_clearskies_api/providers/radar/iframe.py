"""Iframe radar provider module (ADR-015, ADR-038, 3b-16).

Single responsibility: config slot that publishes an operator-supplied
iframe URL for regions without a tile-API path (e.g. BoM Australia,
MetService NZ).

No API calls, no tile bytes, no frames, no credentials.
The operator sets [radar] iframe_url in api.conf; this module wraps it
into a ProviderCapability for the /capabilities endpoint.

Capability is built via make_capability(iframe_url) rather than a
module-level CAPABILITY constant because the URL comes from operator
config and ProviderCapability is a frozen dataclass.
"""

from __future__ import annotations

from weewx_clearskies_api.providers._common.capability import ProviderCapability

PROVIDER_ID = "iframe"
DOMAIN = "radar"


def make_capability(iframe_url: str) -> ProviderCapability:
    """Build a ProviderCapability for the iframe radar provider.

    Args:
        iframe_url: Operator-supplied URL to embed in the dashboard iframe.

    Returns:
        Frozen ProviderCapability with iframe_url set and all tile/WMS
        fields left as None (no tile API behind this provider).
    """
    return ProviderCapability(
        provider_id=PROVIDER_ID,
        domain=DOMAIN,
        supplied_canonical_fields=(),   # radar has no canonical-entity mapping
        geographic_coverage="operator-defined",
        auth_required=(),
        default_poll_interval_seconds=0,  # no polling — static URL
        operator_notes=(
            "Operator-supplied iframe URL for regions without a tile-API path "
            "(e.g. BoM Australia, MetService NZ). Embeds the external radar viewer "
            "directly; loses theming/composition vs native tile providers. "
            "Configure via [radar] iframe_url in api.conf."
        ),
        iframe_url=iframe_url,
    )
