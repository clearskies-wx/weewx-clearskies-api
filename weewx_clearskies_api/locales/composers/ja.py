"""Japanese conditions text composer (I18N T3.4).

Implements JMA-influenced composition for current-observation text.

JMA forecast text uses compound expressions with three operators:
  時々 (tokidoki) = occasionally: 曇り時々晴れ
  一時 (ichiji) = temporarily: 曇り一時雨
  のち (nochi) = then/later: 晴れのち曇り

For real-time observation (not forecast) text, these temporal operators
apply when both sky and precipitation are present — the precipitation
modifies the sky condition with 一時 (temporarily):
  晴天、一時小雨 = sunny, temporarily light rain

When only sky + wind or sky + temperature are present, standard
Japanese punctuation (、) separates components with no connector words.
CJK text conventions: no spaces between components, use 、 as separator.
"""

from __future__ import annotations

from weewx_clearskies_api import i18n


def compose(
    components: dict[str, str | None],
    locale: str,
) -> str:
    """Compose conditions text using JMA-influenced patterns.

    *components* maps component names ("temperature", "sky", "wind",
    "precipitation") to their translated labels (or None if absent).
    """
    sky = components.get("sky")
    temperature = components.get("temperature")
    wind = components.get("wind")
    precipitation = components.get("precipitation")

    parts: list[str] = []

    if temperature:
        parts.append(temperature)

    if sky and precipitation:
        # JMA-style compound: sky 一時 precipitation
        ichiji = i18n.t("composition.temporal_temporarily", locale)
        if ichiji == "composition.temporal_temporarily":
            ichiji = "一時"
        parts.append(f"{sky}{ichiji}{precipitation}")
    else:
        if sky:
            parts.append(sky)
        if precipitation:
            parts.append(precipitation)

    if wind:
        parts.append(wind)

    if not parts:
        return ""

    separator = i18n.t("composition.separator", locale)
    if separator == "composition.separator":
        separator = "、"

    return separator.join(parts)
