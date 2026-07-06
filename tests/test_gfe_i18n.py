"""Locale coverage and inflection tests for the GFE i18n integration (T6.6).

Verifies the i18n contract the GFE forecast/current-conditions text engine
depends on (API-MANUAL.md SS6 "Locale resolution and translated output",
SS15 "Forecast Text Generation"):

1. Every locale file carries the same key set as `en.json` (the
   authoritative source) for the forecast-related namespaces the GFE
   engine reads (`wind.*`, `wx.*`, `gfe.*`, `forecast.*`), with no empty
   ("not yet translated") string values in those namespaces.
2. `i18n.t_inflected()` resolves Romance-locale gender/number-coded
   dicts correctly and passes plain strings (non-gendered locales)
   through unchanged.
3. `i18n.t_case()` resolves Russian grammatical-case dicts correctly.
4. Fallback behavior: English always resolves; unknown gender codes fall
   back gracefully instead of raising or returning an empty result.

This module was explicitly called for by the text-engine plan's T6.6 task
but was never written (MF-7 audit finding) — this file closes that gap.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from weewx_clearskies_api import i18n

_LOCALES_DIR = Path(__file__).resolve().parent.parent / "weewx_clearskies_api" / "locales"
_ALL_LOCALE_CODES = sorted(p.stem for p in _LOCALES_DIR.glob("*.json"))
_NON_ENGLISH_LOCALES = [code for code in _ALL_LOCALE_CODES if code != "en"]

# Namespaces the GFE forecast/current-conditions text engine reads through
# i18n (sse/gfe/*_phrases.py, sse/conditions_text.py). See API-MANUAL.md
# SS15 for the full field/namespace inventory.
_FORECAST_NAMESPACES = ("wind", "wx", "gfe", "forecast")

# Representative plain-string (non-gendered, non-case-coded) keys drawn
# from each forecast namespace, used to exercise t() end-to-end across
# every locale without tripping over the Romance-gender / Russian-case
# dict values that require t_inflected()/t_case() instead (covered by
# dedicated tests below).
_PLAIN_STRING_KEYS = (
    "wind.calm",
    "wind.windy",
    "wind.very_windy",
    "wind.hurricane_force_winds",
    "wind.gusty",
    "wx.pop_type.rain",
    "wx.conjunction.and",
    "gfe.time.today",
    "gfe.time.tonight",
    "gfe.connector.then_becoming",
    "forecast.temp.highs",
    "forecast.sky.sunny",
)


def _load_locale_file(code: str) -> dict:
    with (_LOCALES_DIR / f"{code}.json").open(encoding="utf-8") as f:
        return json.load(f)


def _flatten_keys(data: dict, prefix: str = "") -> set[str]:
    """Return the set of every dotted key path in *data*, including
    intermediate namespace nodes (not just leaves) — matches how
    `_resolve_key()` walks dot-separated paths in `i18n.py`.
    """
    keys: set[str] = set()
    for key, value in data.items():
        full = f"{prefix}.{key}" if prefix else key
        keys.add(full)
        if isinstance(value, dict):
            keys |= _flatten_keys(value, full)
    return keys


def _all_string_leaves(data: dict) -> list[str]:
    """Return every string value found anywhere in *data*, recursively.

    Recurses into nested namespace dicts and into gender/case-coded
    leaf dicts (e.g. ``{"MS": "...", "FS": "..."}``) alike — both are
    plain ``dict``s at the JSON level, and every string found either way
    is a translatable value that must not be empty.
    """
    leaves: list[str] = []
    for value in data.values():
        if isinstance(value, dict):
            leaves.extend(_all_string_leaves(value))
        elif isinstance(value, str):
            leaves.append(value)
        # Lists (e.g. composition.order) are structural, not
        # translatable text — skipped.
    return leaves


class TestLocaleFilesLoad:
    """Sanity checks on the locale file set itself."""

    def test_thirteen_locales_present(self):
        assert len(_ALL_LOCALE_CODES) == 13

    def test_english_is_present(self):
        assert "en" in _ALL_LOCALE_CODES


class TestForecastNamespaceKeyCoverage:
    """All 13 locales resolve all forecast-related keys with no raw-key
    fallback — verified structurally by comparing each locale's key set
    against `en.json` (the authoritative source) for the four namespaces
    the GFE engine reads.
    """

    @pytest.fixture(scope="class")
    def en_data(self) -> dict:
        return _load_locale_file("en")

    @pytest.mark.parametrize("namespace", _FORECAST_NAMESPACES)
    def test_namespace_present_in_english(self, en_data: dict, namespace: str):
        assert namespace in en_data, f"en.json is missing the {namespace!r} namespace"

    @pytest.mark.parametrize("locale", _NON_ENGLISH_LOCALES)
    @pytest.mark.parametrize("namespace", _FORECAST_NAMESPACES)
    def test_locale_has_same_keys_as_english_for_namespace(
        self, en_data: dict, locale: str, namespace: str
    ):
        locale_data = _load_locale_file(locale)
        assert namespace in locale_data, (
            f"{locale}.json is missing the {namespace!r} namespace entirely"
        )

        en_keys = _flatten_keys(en_data[namespace], namespace)
        locale_keys = _flatten_keys(locale_data[namespace], namespace)
        missing = en_keys - locale_keys
        assert not missing, (
            f"{locale}.json is missing {len(missing)} key(s) from the "
            f"{namespace!r} namespace present in en.json: {sorted(missing)}"
        )

    @pytest.mark.parametrize("locale", _ALL_LOCALE_CODES)
    @pytest.mark.parametrize("namespace", _FORECAST_NAMESPACES)
    def test_no_empty_string_values_in_namespace(self, locale: str, namespace: str):
        data = _load_locale_file(locale)
        leaves = _all_string_leaves(data.get(namespace, {}))
        empties = [i for i, v in enumerate(leaves) if v == ""]
        assert not empties, (
            f"{locale}.json's {namespace!r} namespace has "
            f"{len(empties)} empty string value(s) — an empty string is "
            f"treated as 'not translated' and should never be committed"
        )

    @pytest.mark.parametrize("locale", _ALL_LOCALE_CODES)
    @pytest.mark.parametrize("key", _PLAIN_STRING_KEYS)
    def test_plain_string_key_resolves_without_raw_key_fallback(self, locale: str, key: str):
        """`t()` never returns the raw dotted key for any of these
        representative plain-string forecast keys, in any locale.
        """
        resolved = i18n.t(key, locale)
        assert resolved != key, (
            f"t({key!r}, locale={locale!r}) fell all the way through to "
            f"the raw key — {locale}.json is missing a translation"
        )
        assert resolved != "", f"t({key!r}, locale={locale!r}) resolved to an empty string"


class TestInflectedResolution:
    """`t_inflected()` (GFE T6.4) — Romance-locale gender/number agreement."""

    def test_french_feminine_singular_coverage_word(self):
        result = i18n.t_inflected("wx.coverage.scattered", "FS", "fr")
        assert result == "dispersée"

    def test_spanish_masculine_singular_coverage_word(self):
        result = i18n.t_inflected("wx.coverage.scattered", "MS", "es")
        assert result == "disperso"

    def test_french_masculine_plural_coverage_word(self):
        result = i18n.t_inflected("wx.coverage.scattered", "MP", "fr")
        assert result == "dispersés"

    def test_italian_feminine_plural_coverage_word(self):
        result = i18n.t_inflected("wx.coverage.scattered", "FP", "it")
        assert result != "wx.coverage.scattered"
        # Italian is a gendered locale distinct from French/Spanish —
        # confirm it does not silently fall back to one of them.
        assert result != i18n.t_inflected("wx.coverage.scattered", "FP", "fr")
        assert result != i18n.t_inflected("wx.coverage.scattered", "FP", "es")

    def test_non_gendered_locale_returns_plain_string_unchanged(self):
        """German has no gender/number dict for wx.coverage.* — the value
        is a plain string, so t_inflected() must return it unchanged
        regardless of the gender code passed.
        """
        expected = i18n.t("wx.coverage.scattered", "de")
        assert i18n.t_inflected("wx.coverage.scattered", "FS", "de") == expected
        assert i18n.t_inflected("wx.coverage.scattered", "MP", "de") == expected
        assert i18n.t_inflected("wx.coverage.scattered", "XYZ", "de") == expected

    def test_english_returns_plain_string_unchanged_regardless_of_gender(self):
        expected = i18n.t("wx.coverage.scattered", "en")
        assert i18n.t_inflected("wx.coverage.scattered", "FS", "en") == expected
        assert i18n.t_inflected("wx.coverage.scattered", "MP", "en") == expected


class TestCaseResolution:
    """`t_case()` — Russian grammatical-case agreement."""

    def test_russian_nominative_weather_type(self):
        result = i18n.t_case("wx.type.rain", "nominative", "ru")
        assert result == "дождь"

    def test_russian_instrumental_weather_type_differs_from_nominative(self):
        nominative = i18n.t_case("wx.type.rain", "nominative", "ru")
        instrumental = i18n.t_case("wx.type.rain", "instrumental", "ru")
        assert instrumental != nominative
        assert instrumental == "дождём"

    def test_non_case_coded_locale_returns_plain_string_unchanged(self):
        """English's wx.type.rain is a plain string — t_case() must return
        it unchanged regardless of the requested case.
        """
        expected = i18n.t("wx.type.rain", "en")
        assert i18n.t_case("wx.type.rain", "nominative", "en") == expected
        assert i18n.t_case("wx.type.rain", "instrumental", "en") == expected


class TestFallbackBehavior:
    """English always resolves; unmapped inputs degrade gracefully."""

    @pytest.mark.parametrize("key", _PLAIN_STRING_KEYS)
    def test_english_locale_always_returns_non_empty_string(self, key: str):
        result = i18n.t(key, "en")
        assert isinstance(result, str)
        assert result != ""
        assert result != key

    def test_unknown_key_falls_back_to_the_key_itself(self):
        result = i18n.t("wind.this_key_does_not_exist", "en")
        assert result == "wind.this_key_does_not_exist"

    def test_t_inflected_unknown_gender_code_falls_back_to_masculine_singular(self):
        """An unrecognized gender code falls back to MS (masculine
        singular) per `i18n._gender_or_string()`'s documented fallback
        chain, rather than raising or returning an empty string.
        """
        ms_form = i18n.t_inflected("wx.coverage.scattered", "MS", "fr")
        unknown_form = i18n.t_inflected("wx.coverage.scattered", "ZZ", "fr")
        assert unknown_form == ms_form

    def test_t_inflected_unknown_locale_falls_back_to_english(self):
        result = i18n.t_inflected("wx.coverage.scattered", "MS", "xx-not-a-locale")
        assert result == i18n.t_inflected("wx.coverage.scattered", "MS", "en")

    def test_t_case_unknown_case_falls_back_to_nominative(self):
        nominative = i18n.t_case("wx.type.rain", "nominative", "ru")
        unknown_case = i18n.t_case("wx.type.rain", "not_a_real_case", "ru")
        assert unknown_case == nominative
