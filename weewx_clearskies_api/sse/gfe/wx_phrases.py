"""Weather/precipitation phrase generation for the GFE forecast text engine
(ADR-082).

Ported from `WxPhrases.py` (GFE source analysis, `docs/reference/
nws-text-system/gfe-source-code-analysis.md` SS4), covering:

- Weather type + coverage + intensity composition (SS4.1-4.5).
- Special type descriptors — flurries / sprinkles (SS4.4).
- Intensity suppression for shower types and for heavy rain/snow, which are
  covered by :func:`heavy_precip_phrase` instead (SS4.5).
- Weather conjunction rules and the serial-comma join for 3+ items (SS4.8).
- PoP (probability of precipitation) qualification phrasing (SS10, SS4.7).
- Heavy precipitation descriptors (SS4.9).

Type and coverage GFE codes (``"R"``, ``"RW"``, ``"Chc"``, ``"Sct"``, ...)
are not self-describing, so this module carries its own
code -> locale-key-stem tables (:data:`WX_TYPE_NAMES`,
:data:`WX_COVERAGE_NAMES`) built from the GFE source analysis SS4.1/4.2.
These are new derived data, not duplicated threshold values — the
threshold tables in `sse/gfe/thresholds.py` only carry the raw GFE codes.

All display text resolves through ``i18n.t()`` against the caller-supplied
locale. Locale JSON files do not yet carry the ``wx.*`` keys this module
introduces (a separate i18n task); until they do, standalone label lookups
fall through to a humanized (title-cased) rendering of the raw key, and
sentence templates fall through to a literal English default.
"""

from __future__ import annotations

from weewx_clearskies_api import i18n
from weewx_clearskies_api.sse.gfe.thresholds import (
    POP_TYPE_QUALIFICATION,
    POP_WX_LOWER_THRESHOLD,
    WX_CONJUNCTION_RULES,
    WX_COVERAGE_HIERARCHY,
    WX_INTENSITY_CODES,
    WX_POP_RELATED_TYPES,
    WX_TYPE_HIERARCHY,
)

# Type code -> locale-key stem (GFE source analysis SS4.1).
WX_TYPE_NAMES: dict[str, str] = {
    "WP": "waterspouts",
    "T": "thunderstorms",
    "R": "rain",
    "RW": "rain_showers",
    "L": "drizzle",
    "ZR": "freezing_rain",
    "ZL": "freezing_drizzle",
    "S": "snow",
    "SW": "snow_showers",
    "IP": "ice_pellets",
    "F": "fog",
    "ZF": "freezing_fog",
    "IF": "ice_fog",
    "IC": "ice_crystals",
    "H": "haze",
    "BS": "blowing_snow",
    "BN": "blowing_sand",
    "K": "smoke",
    "BD": "blowing_dust",
    "FR": "frost",
    "ZY": "freezing_spray",
    "BA": "unknown_precipitation",
}

# Coverage code -> locale-key stem (GFE source analysis SS4.2). Stems that
# read naturally with a following noun already fold in "of" (e.g.
# "chance_of" -> "chance of light rain") to match SS4's example phrasing.
WX_COVERAGE_NAMES: dict[str, str] = {
    "Def": "definite",
    "Wide": "widespread",
    "Brf": "brief",
    "Frq": "frequent",
    "Ocnl": "occasional",
    "Pds": "periods_of",
    "Inter": "intermittent",
    "Lkly": "likely",
    "Num": "numerous",
    "Sct": "scattered",
    "Chc": "chance_of",
    "Areas": "areas_of",
    "SChc": "slight_chance_of",
    "WSct": "widely_scattered",
    "Iso": "isolated",
    "Patchy": "patchy",
}

# Special type-word overrides (GFE source analysis SS4.4): a (type,
# intensity) pair replaces the usual "{intensity} {type}" wording entirely.
_SPECIAL_TYPE_DESCRIPTORS: dict[tuple[str, str], str] = {
    ("SW", "--"): "flurries",
    ("RW", "--"): "sprinkles",
}

# Shower types suppress the inline intensity word — coverage carries the
# transient/intensity connotation instead (SS4.5).
_SHOWER_TYPES: frozenset[str] = frozenset({"RW", "SW"})

# Heavy rain/snow (R+, S+) suppress the inline intensity word too — heavy
# intensity is reported via heavy_precip_phrase() instead (SS4.5).
_HEAVY_INLINE_SUPPRESSED: frozenset[str] = frozenset({"R", "S"})

# Heavy precipitation descriptor groups (GFE source analysis SS4.9).
_HEAVY_PRECIP_MAP: dict[str, tuple[str, str]] = {
    "R": ("heavy_rain", "rain may be heavy at times"),
    "RW": ("heavy_rain", "rain may be heavy at times"),
    "S": ("heavy_snow", "snow may be heavy at times"),
    "SW": ("heavy_snow", "snow may be heavy at times"),
    "IP": ("heavy_precip", "precipitation may be heavy at times"),
    "ZR": ("heavy_precip", "precipitation may be heavy at times"),
    "L": ("heavy_precip", "precipitation may be heavy at times"),
    "ZL": ("heavy_precip", "precipitation may be heavy at times"),
}


def _t(key: str, locale: str) -> str:
    """Resolve *key* through i18n, falling back to a humanized key.

    Used for standalone label lookups. When no translation exists yet, the
    last dot-segment of the key is title-cased and underscores become
    spaces (``"wx.type.rain_showers"`` -> ``"Rain Showers"``) rather than
    surfacing the raw dotted key.
    """
    resolved = i18n.t(key, locale)
    if resolved == key:
        return key.rsplit(".", 1)[-1].replace("_", " ").title()
    return resolved


def _t_lower(key: str, fallback_word: str, locale: str) -> str:
    """Resolve *key* through i18n, falling back to a lowercase literal word.

    Used for conjunctions/connectors, which must stay lowercase mid-sentence
    (unlike :func:`_t`'s title-cased fallback for standalone labels).
    """
    resolved = i18n.t(key, locale)
    if resolved == key:
        return fallback_word.replace("_", " ")
    return resolved


def _t_template(key: str, default: str, locale: str) -> str:
    """Resolve *key* through i18n, falling back to a literal *default* template.

    Used for sentence templates containing ``{placeholder}`` tokens, where
    title-casing (as :func:`_t` does) would destroy the template.
    """
    resolved = i18n.t(key, locale)
    return default if resolved == key else resolved


def _format_number(value: float) -> str:
    """Render a numeric value without a trailing ``.0`` for whole numbers."""
    return str(int(value)) if float(value).is_integer() else str(value)


def _type_word(wx_type: str, locale: str) -> str:
    """Resolve the display word for *wx_type* from its hierarchy position."""
    if wx_type not in WX_TYPE_HIERARCHY:
        return _t(f"wx.type.{wx_type}", locale)
    name_key = WX_TYPE_NAMES.get(wx_type, wx_type)
    return _t(f"wx.type.{name_key}", locale)


def _coverage_word(coverage: str, locale: str) -> str:
    """Resolve the display word for *coverage* from its hierarchy position."""
    if coverage not in WX_COVERAGE_HIERARCHY:
        return _t(f"wx.coverage.{coverage}", locale)
    name_key = WX_COVERAGE_NAMES.get(coverage, coverage)
    return _t(f"wx.coverage.{name_key}", locale)


def _suppress_intensity(wx_type: str, intensity: str) -> bool:
    """Whether the intensity word should be omitted from the inline phrase."""
    if wx_type in _SHOWER_TYPES:
        return True
    return intensity == "+" and wx_type in _HEAVY_INLINE_SUPPRESSED


def weather_phrase(wx_type: str, coverage: str, intensity: str, pop: float, locale: str) -> str:
    """Compose a weather phrase from type + coverage + intensity.

    PoP-related types (:data:`WX_POP_RELATED_TYPES`) below
    :data:`POP_WX_LOWER_THRESHOLD` are suppressed entirely (empty string) —
    the GFE source uses this threshold to suppress weather mention rather
    than fabricate a low-confidence phrase.

    Special type descriptors (GFE source analysis SS4.4) fully replace the
    usual "{coverage} {intensity} {type}" composition: very-light snow
    showers -> "flurries", very-light rain showers -> "sprinkles". Shower
    types and heavy rain/snow suppress the inline intensity word (SS4.5) —
    see :func:`heavy_precip_phrase` for the latter.
    """
    if wx_type in WX_POP_RELATED_TYPES and pop < POP_WX_LOWER_THRESHOLD:
        return ""

    coverage_word = _coverage_word(coverage, locale)

    special = _SPECIAL_TYPE_DESCRIPTORS.get((wx_type, intensity))
    if special is not None:
        type_word = _t(f"wx.type.{special}", locale)
        return f"{coverage_word} {type_word}".strip()

    type_word = _type_word(wx_type, locale)

    if _suppress_intensity(wx_type, intensity):
        parts = [coverage_word, type_word]
    else:
        intensity_key = WX_INTENSITY_CODES.get(intensity)
        intensity_word = _t(f"wx.intensity.{intensity_key}", locale) if intensity_key else None
        parts = (
            [coverage_word, intensity_word, type_word]
            if intensity_word
            else [
                coverage_word,
                type_word,
            ]
        )

    return " ".join(part for part in parts if part)


def combine_weather_phrases(phrases: list[str], locale: str) -> str:
    """Join multiple weather phrases with conjunctions (GFE source analysis SS4.8).

    2 items join with the default conjunction ("rain and snow"); 3+ items
    use a serial (Oxford) comma before the final conjunction ("rain, snow,
    and freezing rain"). Empty phrases (suppressed by :func:`weather_phrase`)
    are dropped before joining.
    """
    items = [phrase for phrase in phrases if phrase]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]

    connector = _t_lower(
        f"wx.conjunction.{WX_CONJUNCTION_RULES['default']}",
        WX_CONJUNCTION_RULES["default"],
        locale,
    )
    if len(items) == 2:
        return f"{items[0]} {connector} {items[1]}"
    return f"{', '.join(items[:-1])}, {connector} {items[-1]}"


def pop_qualification(pop: float, wx_type: str, locale: str) -> str | None:
    """Return a PoP qualification phrase, or ``None`` below threshold.

    Below :data:`POP_WX_LOWER_THRESHOLD`, returns ``None``. Otherwise looks
    up the type-specific PoP word from :data:`POP_TYPE_QUALIFICATION`
    (falling back to the generic "precipitation" word for types not listed
    individually) and composes ``"chance of {type} {pop} percent"``.
    """
    if pop < POP_WX_LOWER_THRESHOLD:
        return None

    type_key = POP_TYPE_QUALIFICATION.get(wx_type, "precipitation")
    type_word = _t_lower(f"wx.pop_type.{type_key}", type_key, locale)
    template = _t_template("wx.pop_qualification", "chance of {type} {pop} percent", locale)
    return template.format(type=type_word, pop=_format_number(pop))


def heavy_precip_phrase(wx_type: str, intensity: str, locale: str) -> str | None:
    """Return a heavy precipitation phrase, or ``None`` (GFE source analysis SS4.9).

    Only applies when *intensity* is ``"+"`` (heavy). Groups types into
    rain / snow / other-precipitation descriptor phrases; types outside
    those three groups (e.g. fog, haze) have no heavy-precip descriptor and
    return ``None``.
    """
    if intensity != "+":
        return None

    mapping = _HEAVY_PRECIP_MAP.get(wx_type)
    if mapping is None:
        return None

    key_stem, default_text = mapping
    return _t_template(f"wx.{key_stem}", default_text, locale)
