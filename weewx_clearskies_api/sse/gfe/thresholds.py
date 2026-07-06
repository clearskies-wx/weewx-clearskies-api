"""GFE threshold tables — language-independent data only.

Ported faithfully from the NWS Graphical Forecast Editor (GFE) text
formatter source (public domain, 17 USC S105). Source repository:
`Unidata/awips2`, base path
`cave/com.raytheon.viz.gfe/localization/gfe/userPython/textUtilities/`.
Full analysis: `docs/reference/nws-text-system/gfe-source-code-analysis.md`.
Decision record: ADR-082 (NWS GFE Text Generation System with WorldCast
Technology).

All keys in this module are locale-independent identifiers (e.g.
``"mostly_cloudy"``, not "Mostly Cloudy"). Display strings are resolved
from locale JSON files at generation time by `sse/gfe/*_phrases.py`
modules — this module holds no display strings.

Every value below must match the GFE source exactly. Do not round,
adjust, or "improve" any threshold — see the GFE code reuse directive in
ADR-082 settled decision #9.
"""

import math
from typing import Final, NamedTuple

# ---------------------------------------------------------------------------
# 1. Sky coverage (GFE source SS1, `ScalarPhrases.py` `sky_valueList()`)
# ---------------------------------------------------------------------------


class SkyBucket(NamedTuple):
    """One row of the 6-bucket sky coverage table.

    ``upper_pct`` is the inclusive upper bound of the sky cover percentage
    range for this bucket (buckets are contiguous, starting at 0).
    """

    upper_pct: int
    day_key: str
    night_key: str


SKY_VALUE_LIST: Final[list[SkyBucket]] = [
    SkyBucket(5, "sunny", "clear"),
    SkyBucket(25, "sunny", "mostly_clear"),
    SkyBucket(50, "mostly_sunny", "partly_cloudy"),
    SkyBucket(69, "partly_sunny", "mostly_cloudy"),
    SkyBucket(87, "mostly_cloudy", "mostly_cloudy"),
    SkyBucket(100, "cloudy", "cloudy"),
]

# Adjacent-transition suppression (`similarSkyWords_list`) — transitions
# between these pairs are too similar to report as a trend.
SIMILAR_SKY_WORDS_DAY: Final[list[tuple[str, str]]] = [
    ("sunny", "mostly_sunny"),
    ("mostly_sunny", "partly_sunny"),
    ("partly_sunny", "mostly_cloudy"),
    ("mostly_cloudy", "cloudy"),
]

SIMILAR_SKY_WORDS_NIGHT: Final[list[tuple[str, str]]] = [
    ("clear", "mostly_clear"),
    ("mostly_clear", "partly_cloudy"),
    ("mostly_cloudy", "cloudy"),
]

# `pop_sky_lower_threshold` — when PoP >= this value (for > 50% of
# sub-periods), the sky phrase is omitted entirely.
POP_SKY_LOWER_THRESHOLD: Final[int] = 55

# ---------------------------------------------------------------------------
# 2. Temperature (GFE source SS2, `ScalarPhrases.py`)
# ---------------------------------------------------------------------------

# `tempDiff_threshold` — above this spread (deg F), report an exact range
# ("23 to 29") instead of decade phrasing.
TEMP_DIFF_THRESHOLD: Final[int] = 4

# `tempPhrase_boundary_dict` — position within a decade, keyed by ones digit.
TEMP_BOUNDARY_DICT: Final[dict[int, str]] = {
    0: "lower",
    1: "lower",
    2: "lower",
    3: "lower",
    4: "mid",
    5: "mid",
    6: "mid",
    7: "upper",
    8: "upper",
    9: "upper",
}


class TempException(NamedTuple):
    """One row of the `tempPhrase_exceptions` table.

    ``min_bounds``/``max_bounds`` are inclusive (low, high) ranges that the
    period's min/max temperature must fall within for this row to apply.
    ``equality_key`` is the phrase used when min == max (``None`` when the
    source table leaves the equality phrase blank); ``range_key`` is used
    otherwise.
    """

    min_bounds: tuple[int, int]
    max_bounds: tuple[int, int]
    equality_key: str | None
    range_key: str


TEMP_EXCEPTION_TABLE: Final[list[TempException]] = [
    TempException((100, 200), (100, 200), "around_min", "min_to_max"),
    TempException((90, 99), (100, 200), None, "min_to_max"),
    TempException((1, 19), (1, 29), "around_min", "min_to_max"),
    TempException((0, 0), (0, 29), "near_zero", "zero_to_max"),
    TempException((-200, 0), (0, 0), "near_zero", "min_to_zero"),
    TempException((-200, -1), (1, 200), "near_zero", "min_to_max_zero"),
    TempException((-200, -1), (-200, -1), "around_min", "max_to_min_zero"),
    # Exact-decade rows: the same (equality, range) pattern repeats for
    # each round decade 20-90 in the GFE source table.
    *(
        TempException((decade, decade), (decade, decade), "around_min", "min_to_max")
        for decade in (20, 30, 40, 50, 60, 70, 80, 90)
    ),
]

# `temp_trend_nlValue` — hourly-vs-period-extreme difference (deg F) that
# triggers "temperatures falling/rising into the ..." trend wording.
TEMP_TREND_THRESHOLD: Final[int] = 20

# `steady_temp_threshold` — diurnal range (deg F) below which a period is
# described as "near steady temperature" instead of high/low phrasing.
STEADY_TEMP_THRESHOLD: Final[int] = 4


class ExtremeTempRule(NamedTuple):
    """One row of `extremeTemps_words`.

    ``conditions`` is an ordered, ANDed tuple of ``(field, op, value)``
    triples that must all evaluate true for ``descriptor`` to apply.
    Rules are evaluated in list order; the first matching rule wins.
    Recognized fields: ``"temp"``, ``"heat_index"``,
    ``"heat_index_minus_temp"``, ``"wind_chill"``.
    """

    conditions: tuple[tuple[str, str, float], ...]
    descriptor: str


EXTREME_TEMP_DESCRIPTORS: Final[dict[str, list[ExtremeTempRule]]] = {
    "day": [
        ExtremeTempRule(
            (("temp", ">", 99), ("heat_index_minus_temp", ">", 7)), "very_hot_and_humid"
        ),
        ExtremeTempRule((("temp", ">", 99),), "very_hot"),
        ExtremeTempRule((("temp", ">", 95), ("heat_index_minus_temp", ">", 6)), "hot_and_humid"),
        ExtremeTempRule((("temp", ">", 95),), "hot"),
        ExtremeTempRule((("temp", "<", 20), ("wind_chill", "<", -9)), "bitterly_cold"),
        ExtremeTempRule((("temp", "<", 20),), "very_cold"),
        ExtremeTempRule((("heat_index", ">=", 108),), "hot_and_humid"),
        ExtremeTempRule((("wind_chill", "<=", 0),), "bitterly_cold"),
    ],
    "night": [
        ExtremeTempRule((("temp", "<", 5), ("wind_chill", "<=", 0)), "bitterly_cold"),
        ExtremeTempRule((("temp", "<", 5),), "very_cold"),
        ExtremeTempRule((("wind_chill", "<=", 0),), "bitterly_cold"),
    ],
}

# `colder_warmer_dict` — comparative trend language, keyed by trend
# direction/magnitude, then an ordered list of ((low, high), descriptor)
# ranges over the reference temperature (deg F). First matching range wins.
COLDER_WARMER_DICT: Final[dict[str, list[tuple[tuple[int, int], str]]]] = {
    "low_colder": [((-80, 45), "colder"), ((45, 70), "cooler"), ((70, 150), "not_as_warm")],
    "low_much_colder": [
        ((-80, 45), "much_colder"),
        ((45, 70), "much_cooler"),
        ((70, 150), "not_as_warm"),
    ],
    "low_warmer": [((-80, 35), "not_as_cold"), ((35, 50), "not_as_cool"), ((50, 150), "warmer")],
    "high_colder": [
        ((-80, 45), "colder"),
        ((45, 75), "cooler"),
        ((75, 90), "not_as_warm"),
        ((90, 150), "not_as_hot"),
    ],
    "high_warmer": [
        ((-80, 45), "not_as_cold"),
        ((45, 65), "not_as_cool"),
        ((65, 150), "warmer"),
    ],
}

# ---------------------------------------------------------------------------
# 3. Wind (GFE source SS3, `VectorRelatedPhrases.py`)
# ---------------------------------------------------------------------------

# `null_nlValue_dict["Wind"]` — below this (mph), wind is reported as null
# ("light winds" / "light").
WIND_NULL_THRESHOLD: Final[int] = 5

# `gust_wind_difference_nlValue` — report gusts only when
# gusts - maxWind exceeds this (mph).
WIND_GUST_DIFFERENCE: Final[int] = 10

# `vector_summary_valueStr` — raw GFE summary descriptor thresholds (mph),
# ascending, upper-bound exclusive except the final (open-ended) entry.
# NOTE: this is the unmodified GFE table. The hybrid Beaufort/GFE wind
# scale used for rendering (ADR-082 settled decision #11) is a separate
# table defined in `sse/gfe/wind_phrases.py`, not here.
WIND_SUMMARY_DESCRIPTORS: Final[list[tuple[int, str]]] = [
    (25, ""),
    (30, "breezy"),
    (40, "windy"),
    (50, "very_windy"),
    (74, "strong_winds"),
    (999, "hurricane_force_winds"),
]

# `marine_wind_mag` — marine wind descriptor thresholds (knots), ascending,
# lower-bound inclusive.
MARINE_WIND_DESCRIPTORS: Final[list[tuple[int, str]]] = [
    (34, "gales_to"),
    (45, "storm_force_to"),
    (64, "hurricane_force_to"),
]

# ---------------------------------------------------------------------------
# 4. Weather types (GFE source SS4, `WxPhrases.py`)
# ---------------------------------------------------------------------------

# `wxHierarchies` — weather type codes, highest priority first.
WX_TYPE_HIERARCHY: Final[list[str]] = [
    "WP",
    "T",
    "R",
    "RW",
    "L",
    "ZR",
    "ZL",
    "S",
    "SW",
    "IP",
    "F",
    "ZF",
    "IF",
    "IC",
    "H",
    "BS",
    "BN",
    "K",
    "BD",
    "FR",
    "ZY",
    "BA",
]

# `wxHierarchies["coverage"]` — coverage codes, strongest first.
WX_COVERAGE_HIERARCHY: Final[list[str]] = [
    "Def",
    "Wide",
    "Brf",
    "Frq",
    "Ocnl",
    "Pds",
    "Inter",
    "Lkly",
    "Num",
    "Sct",
    "Chc",
    "Areas",
    "SChc",
    "WSct",
    "Iso",
    "Patchy",
]

# `wxHierarchies["intensity"]`
WX_INTENSITY_CODES: Final[dict[str, str]] = {
    "+": "heavy",
    "m": "moderate",
    "-": "light",
    "--": "very_light",
}

# `similarCoverageLists` — coverage codes considered too similar to report
# a transition between.
WX_SIMILAR_COVERAGE: Final[list[list[str]]] = [
    ["SChc", "Iso"],
    ["Chc", "Sct"],
    ["Lkly", "Num"],
    ["Brf", "Frq", "Ocnl", "Pds", "Inter", "Def", "Wide"],
]

# `pop_related_flag` — weather types whose coverage is derived from PoP.
WX_POP_RELATED_TYPES: Final[list[str]] = ["ZR", "R", "RW", "S", "SW", "T", "IP"]

# Weather conjunction rules (`wxConjunction`, `withPossible`, `withPhrase`,
# `withPocketsOf`, `possiblyMixedWith`, `mixedWith`).
WX_CONJUNCTION_RULES: Final[dict[str, str]] = {
    "default": "and",
    "definite_with_likely": "with",
    "or_attribute": "or",
    "thunderstorm_with_little_rain": "with_little_or_no_rain",
    "mixed_precipitation": "mixed_with",
    "uncertain_secondary": "with_possible",
    "pockets": "with_pockets_of",
}

# ---------------------------------------------------------------------------
# 5. PoP (GFE source SS10, `ScalarPhrases.py`)
# ---------------------------------------------------------------------------

# `pop_lower_threshold` — first 24 hours.
POP_LOWER_THRESHOLD: Final[int] = 15

# `pop_lower_threshold` — after the first 24 hours (extended range).
POP_LOWER_THRESHOLD_EXTENDED: Final[int] = 25

# `pop_wx_lower_threshold` — used by WxPhrases to suppress weather mention.
POP_WX_LOWER_THRESHOLD: Final[int] = 20


class PopCoverageBand(NamedTuple):
    """One row of the PoP-to-coverage derivation table (ADR-082).

    ``lower_pct``/``upper_pct`` are inclusive bounds on the period PoP
    value. ``pop_related_coverage`` is the coverage key for PoP-related
    weather types (rain, snow, thunderstorms, ...); ``areal_coverage`` is
    the coverage key for areal weather types (fog, haze, smoke, ...).
    ``None`` means suppressed (below threshold) or separated into its own
    PoP phrase rather than expressed as a coverage term — see
    `checkIncludePoP` in the GFE source (PoP >= 60% is reported standalone).
    """

    lower_pct: int
    upper_pct: int
    pop_related_coverage: str | None
    areal_coverage: str | None


POP_TO_COVERAGE_TABLE: Final[list[PopCoverageBand]] = [
    # Below POP_LOWER_THRESHOLD (15%, first 24h) / POP_LOWER_THRESHOLD_EXTENDED
    # (25%, extended) weather is not mentioned at all. This band represents
    # the always-suppressed floor; callers apply the period-relative
    # threshold before consulting this table.
    PopCoverageBand(0, 14, None, None),
    PopCoverageBand(15, 24, "SChc", "Iso"),
    PopCoverageBand(25, 54, "Chc", "Sct"),
    PopCoverageBand(55, 74, "Lkly", "Num"),
    # >= 75%: PoP-related types are separated into their own popMax_phrase
    # rather than expressed as a coverage term; areal types resolve to
    # Widespread/Definite.
    PopCoverageBand(75, 100, None, "Wide"),
]

# `getPopType` — PoP type qualification. Types not listed individually
# ([ZL, ZR, ZF, ZY], plus IP) always resolve to the generic "precipitation".
POP_TYPE_QUALIFICATION: Final[dict[str, str]] = {
    "R": "rain",
    "RW": "showers",
    "S": "snow",
    "SW": "snow",
    "T": "thunderstorms",
    "IP": "precipitation",
    "ZL": "precipitation",
    "ZR": "precipitation",
    "ZF": "precipitation",
    "ZY": "precipitation",
}

# ---------------------------------------------------------------------------
# 6. Snow and ice (GFE source SS9, `ScalarPhrases.py`)
# ---------------------------------------------------------------------------

# `pop_snow_lower_threshold` — snow accumulation phrasing requires PoP at
# or above this value AND an accumulating weather type (S, SW, IP, IC).
POP_SNOW_LOWER_THRESHOLD: Final[int] = 60


class SnowAccumulationTier(NamedTuple):
    """One row of `snow_words`.

    Fields are ``None`` when the corresponding condition does not apply to
    that row. Rows are evaluated in order; the first matching row wins.
    The final row (all fields ``None``) is the "otherwise" fallback.
    """

    max_lt: float | None
    min_lt: float | None
    delta_lt: float | None
    phrase_key: str


SNOW_ACCUMULATION_TIERS: Final[list[SnowAccumulationTier]] = [
    SnowAccumulationTier(max_lt=0.5, min_lt=None, delta_lt=None, phrase_key="no_accumulation"),
    SnowAccumulationTier(
        max_lt=1, min_lt=None, delta_lt=None, phrase_key="little_or_no_accumulation"
    ),
    SnowAccumulationTier(max_lt=3, min_lt=1, delta_lt=None, phrase_key="up_to_max"),
    SnowAccumulationTier(max_lt=None, min_lt=None, delta_lt=2, phrase_key="around_max"),
    SnowAccumulationTier(max_lt=None, min_lt=None, delta_lt=None, phrase_key="min_to_max"),
]


class DescriptiveSnowTier(NamedTuple):
    """One row of `descriptive_snow_words`.

    ``max_upper`` is the exclusive upper bound of max accumulation (inches)
    for this tier; ``None`` means unbounded (the final, open-ended tier).
    ``phrase_key`` is ``None`` when the source table has no descriptor for
    that range (accumulations under 1 inch produce no descriptive phrase).
    """

    max_upper: float | None
    phrase_key: str | None


DESCRIPTIVE_SNOW_TIERS: Final[list[DescriptiveSnowTier]] = [
    DescriptiveSnowTier(1, None),
    DescriptiveSnowTier(2, "light_snow_accumulations"),
    DescriptiveSnowTier(5, "moderate_snow_accumulations"),
    DescriptiveSnowTier(None, "heavy_snow_accumulations"),
]


class IceAccumulationTier(NamedTuple):
    """One row of `iceAccumulation_words`.

    ``upper_bound`` is the exclusive upper bound of ice accumulation
    (inches) for this tier; ``None`` means unbounded — the source rounds
    to the nearest whole integer at and above 1.8 inches.
    """

    upper_bound: float | None
    phrase_key: str


ICE_ACCUMULATION_TIERS: Final[list[IceAccumulationTier]] = [
    IceAccumulationTier(0.2, "less_than_one_quarter"),
    IceAccumulationTier(0.4, "one_quarter"),
    IceAccumulationTier(0.7, "one_half"),
    IceAccumulationTier(0.9, "three_quarters"),
    IceAccumulationTier(1.3, "one"),
    IceAccumulationTier(1.8, "one_and_a_half"),
    IceAccumulationTier(None, "integer_rounded"),
]

# ---------------------------------------------------------------------------
# 7. Marine (GFE source SS17, `MarinePhrases.py`) — tables only, no
#    generation code and no marine provider module in this scope.
# ---------------------------------------------------------------------------


class WaveHeightRange(NamedTuple):
    """One row of `wave_range`. ``average_ft`` is the sample point the GFE
    source table is keyed on; lookups interpolate/select the nearest
    documented point, matching the source's behavior.
    """

    average_ft: float
    phrase_key: str


WAVE_HEIGHT_RANGES: Final[list[WaveHeightRange]] = [
    WaveHeightRange(0, "less_than_1_foot"),
    WaveHeightRange(1, "1_foot_or_less"),
    WaveHeightRange(1.5, "1_to_2_feet"),
    WaveHeightRange(2, "1_to_3_feet"),
    WaveHeightRange(3, "2_to_4_feet"),
    WaveHeightRange(5, "3_to_6_feet"),
    WaveHeightRange(8, "6_to_10_feet"),
    WaveHeightRange(12, "10_to_14_feet"),
    WaveHeightRange(20, "15_to_20_feet"),
    WaveHeightRange(100, "over_20_feet"),
]


class ChopCategory(NamedTuple):
    """One row of `chop_words`. ``upper_kt`` is the inclusive upper bound
    of wind speed (knots) for this category; the final category uses
    ``math.inf`` for the open-ended "> 32 kt" row.
    """

    upper_kt: float
    phrase_key: str


CHOP_CATEGORIES: Final[list[ChopCategory]] = [
    ChopCategory(7, "smooth"),
    ChopCategory(12, "light_chop"),
    ChopCategory(17, "moderate_chop"),
    ChopCategory(22, "choppy"),
    ChopCategory(27, "rough"),
    ChopCategory(32, "very_rough"),
    ChopCategory(math.inf, "extremely_rough"),
]

# Wind speed (kt) above which combined seas (WaveHeight) are reported
# instead of wind waves.
MARINE_SEAS_THRESHOLD: Final[int] = 34

# ---------------------------------------------------------------------------
# 8. Fire weather (GFE source SS18, `FirePhrases.py`) — tables only.
#    Activation tier per element is governed by ADR-082 settled decision
#    #6, not by this module.
# ---------------------------------------------------------------------------


class SmokeDispersalCategory(NamedTuple):
    """One row of the smoke dispersal (VentRate) table. ``upper_bound`` is
    the exclusive upper bound of VentRate (knot-ft); the final category
    uses ``math.inf``.
    """

    upper_bound: float
    phrase_key: str


SMOKE_DISPERSAL_CATEGORIES: Final[list[SmokeDispersalCategory]] = [
    SmokeDispersalCategory(40_000, "poor"),
    SmokeDispersalCategory(60_000, "fair"),
    SmokeDispersalCategory(100_000, "good"),
    SmokeDispersalCategory(150_000, "very_good"),
    SmokeDispersalCategory(math.inf, "excellent"),
]


class HainesIndexCategory(NamedTuple):
    """One row of the Haines Index table. ``upper_index`` is the inclusive
    upper bound of the index value for this category.
    """

    upper_index: int
    phrase_key: str


HAINES_INDEX_CATEGORIES: Final[list[HainesIndexCategory]] = [
    HainesIndexCategory(3, "very_low_potential"),
    HainesIndexCategory(4, "low_potential"),
    HainesIndexCategory(5, "moderate_potential"),
    HainesIndexCategory(10, "high_potential"),
]

# MaxRH threshold (%) above which humidity recovery is immediately
# "Excellent", bypassing the 24h-diff tiers below.
HUMIDITY_RECOVERY_MAXRH_SHORTCUT: Final[int] = 50


class HumidityRecoveryCategory(NamedTuple):
    """One row of the humidity recovery table (used when the MaxRH
    shortcut does not apply). ``upper_diff_pct`` is the inclusive upper
    bound of the percentage difference from the reading 24h prior.
    """

    upper_diff_pct: float
    phrase_key: str


HUMIDITY_RECOVERY_CATEGORIES: Final[list[HumidityRecoveryCategory]] = [
    HumidityRecoveryCategory(25, "poor"),
    HumidityRecoveryCategory(55, "moderate"),
    HumidityRecoveryCategory(70, "good"),
    HumidityRecoveryCategory(100, "excellent"),
]


class LalLevel(NamedTuple):
    """One row of the Lightning Activity Level (LAL) table."""

    level: int
    phrase_key: str


LAL_LEVELS: Final[list[LalLevel]] = [
    LalLevel(1, "no_tstms"),
    LalLevel(2, "strikes_1_8"),
    LalLevel(3, "strikes_9_15"),
    LalLevel(4, "strikes_16_25"),
    LalLevel(5, "strikes_25_plus"),
    LalLevel(6, "dry_lightning"),
]

# Coverage-code-to-LAL mapping. Source (gfe-source-code-analysis.md SS18.4):
# "Iso/SChc -> 2-3, Patchy -> 2, Areas/Chc/Sct -> 4, Lkly through Wide -> 5,
# any Dry T -> 6." Only the codes named explicitly in the source summary
# are mapped here; the analysis document does not enumerate the full
# FirePhrases.py coverage ordering used for the "Lkly through Wide" span
# (it is a different, narrower ordering than WX_COVERAGE_HIERARCHY, since
# Chc/Sct are already claimed by the "Areas/Chc/Sct -> 4" bucket). Ranges
# are represented as (min_level, max_level) inclusive tuples. Flagged to
# the lead for confirmation before `fire_phrases.py` consumes this table.
COVERAGE_TO_LAL: Final[dict[str, tuple[int, int]]] = {
    "Iso": (2, 3),
    "SChc": (2, 3),
    "Patchy": (2, 2),
    "Areas": (4, 4),
    "Chc": (4, 4),
    "Sct": (4, 4),
    "Lkly": (5, 5),
    "Wide": (5, 5),
}

# Any thunderstorm with the "Dry" attribute forces LAL 6 regardless of
# coverage.
DRY_THUNDERSTORM_LAL_OVERRIDE: Final[int] = 6

# ---------------------------------------------------------------------------
# 9. Configuration thresholds (GFE source SS7, `ConfigVariables.py`; coverage
#    weights from SS15.4, `SampleAnalysis.py` `coverage_weights`)
# ---------------------------------------------------------------------------

# `null_nlValue_dict` — below this value, the element is reported as null.
NULL_VALUES: Final[dict[str, float]] = {
    "wind": 5,
    "wind_gust": 20,
    "wave_height": 3,
    "heat_index": 108,
    "wind_chill": -100,
}

# `scalar_difference_nlValue_dict` — report a change only when it exceeds
# this value.
SCALAR_DIFFERENCE_VALUES: Final[dict[str, float]] = {
    "wind_gust": 10,
    "pop": 200,  # effectively never reported as its own sub-phrase
    "wave_height": 5,
    "max_t": 10,
}

# `increment_nlValue_dict` — rounding increments per element.
ROUNDING_INCREMENTS: Final[dict[str, float]] = {
    "wind": 5,
    "pop": 10,
    "snow_amt": 0.1,
    "qpf": 0.01,
    "mix_hgt": 100,
}

# `coverage_weights` — used by `getDominantValues` to rank competing
# weather types within a period. Higher weight = more significant.
COVERAGE_WEIGHTS: Final[dict[str, int]] = {
    "Def": 6,
    "Wide": 5,
    "Brf": 4,
    "Frq": 4,
    "Ocnl": 4,
    "Pds": 4,
    "Inter": 4,
    "Lkly": 4,
    "Num": 3,
    "Sct": 2,
    "Chc": 2,
    "Areas": 2,
    "SChc": 1,
    "WSct": 1,
    "Iso": 1,
    "Patchy": 1,
}
