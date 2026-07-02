"""Chinese conditions text composer (I18N T3.4).

Handles both Simplified (zh-CN) and Traditional (zh-TW) Chinese.

CMA (China Meteorological Administration) and CWA (Central Weather
Administration, Taiwan) use space-separated single-term conditions:
  中雨 东南风 3~4级 = moderate rain, SE wind, grade 3-4

For current-observation text, components are space-separated with
no connector words.  Chinese fullwidth comma (，) separates major
clauses; spaces separate within a clause.
"""

from __future__ import annotations

from weewx_clearskies_api import i18n


def compose(
    components: dict[str, str | None],
    locale: str,
) -> str:
    """Compose conditions text using CMA/CWA-style patterns.

    *components* maps component names ("temperature", "sky", "wind",
    "precipitation") to their translated labels (or None if absent).
    """
    order = ["sky", "temperature", "precipitation", "wind"]

    parts: list[str] = []
    for key in order:
        val = components.get(key)
        if val:
            parts.append(val)

    if not parts:
        return ""

    separator = i18n.t("composition.separator", locale)
    if separator == "composition.separator":
        separator = "，"

    return separator.join(parts)
