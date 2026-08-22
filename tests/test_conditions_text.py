"""Unit tests for the terse-tier conditions text composer (ADR-044 §9,
NWS-style order/casing/article patch).

Covers weewx_clearskies_api/sse/conditions_text.py's `_template_compose()`
and `_wind_article()` — the locale-driven component order, NWS-style
sentence case (lowercase components, capitalize only the assembled
string's first character), and the wind-specific indefinite article
("with a gentle breeze" rather than "with Gentle Breeze").

These exercise the composition helpers directly (as `_current_sky_label`
is exercised directly in test_gfe_composer_current.py) rather than driving
the full `build_weather_text()` pipeline, since composition order/casing/
article logic is independent of how the individual component labels were
derived (sky classification, Beaufort lookup, etc.).

Module under test: weewx_clearskies_api/sse/conditions_text.py
"""

from __future__ import annotations

from weewx_clearskies_api import i18n
from weewx_clearskies_api.sse import conditions_text as ct

# Production startup (__main__.py) calls i18n.load_locales() explicitly
# before serving any request (see i18n.py's module docstring). Load it here
# too so composition.order/wind_article resolve correctly even when this
# is the very first i18n lookup in the test process — _template_compose
# reads i18n._locales directly for its order lookup, which (unlike i18n.t())
# does not itself trigger the lazy-load.
i18n.load_locales()

EN = "en"
DE = "de"


def _components(
    *,
    temperature: str | None = None,
    sky: str | None = None,
    wind: str | None = None,
    precipitation: str | None = None,
) -> dict[str, str | None]:
    return {
        "temperature": temperature,
        "sky": sky,
        "wind": wind,
        "precipitation": precipitation,
    }


class TestComponentOrder:
    """English composition order is sky-first (NWS convention), not the old
    temperature-first order."""

    def test_english_order_is_sky_first(self):
        components = _components(
            temperature="Warm and Humid",
            sky="Partly Cloudy",
            wind="Gentle Breeze",
            precipitation="Light Rain",
        )
        result = ct._template_compose(components, EN)

        # Sky must appear before temperature, and temperature before wind.
        assert result.index("cloudy") < result.index("warm")
        assert result.index("warm") < result.index("breeze")
        assert result.index("breeze") < result.index("rain")

    def test_unknown_locale_falls_back_to_default_order(self):
        # An unloaded locale code has no composition.order — falls back to
        # _COMPONENT_ORDER_DEFAULT ([temperature, sky, wind, precipitation]),
        # i.e. temperature before sky (unlike English's locale-driven order).
        components = _components(temperature="Warm", sky="Clear")
        result = ct._template_compose(components, "xx-not-a-real-locale")
        assert result == "Warm, with clear"


class TestSentenceCase:
    """Only the first character of the assembled string is capitalized;
    every component is lowercased before assembly."""

    def test_only_first_character_capitalized(self):
        components = _components(
            temperature="Warm and Humid",
            sky="Partly Cloudy",
            wind="Gentle Breeze",
            precipitation="Light Rain",
        )
        result = ct._template_compose(components, EN)

        assert result[0] == "P"
        assert result[1:] == result[1:].lower()
        # No stray capitals mid-string (Title Case fragments eliminated).
        assert "Cloudy" not in result
        assert "Warm" not in result
        assert "Breeze" not in result
        assert "Rain" not in result

    def test_single_component_is_sentence_cased(self):
        components = _components(wind="Gentle Breeze")
        result = ct._template_compose(components, EN)
        assert result == "Gentle breeze"

    def test_empty_result_does_not_raise(self):
        # Capitalization must handle the empty-string edge case cleanly.
        result = ct._template_compose(_components(), EN)
        assert result == ""


class TestWindArticle:
    """The locale's wind_article is prepended only when wind is the final
    component AND the connector is connector_with (i.e. not Calm)."""

    def test_article_prepended_when_wind_is_last_with_connector(self):
        components = _components(sky="Partly Cloudy", wind="Gentle Breeze")
        result = ct._template_compose(components, EN)
        assert result == "Partly cloudy, with a gentle breeze"

    def test_article_prepended_with_three_components(self):
        components = _components(
            sky="Partly Cloudy", temperature="Warm and Humid", wind="Gentle Breeze"
        )
        result = ct._template_compose(components, EN)
        assert result == "Partly cloudy, warm and humid, with a gentle breeze"

    def test_no_article_when_wind_is_not_last_component(self):
        # Precipitation follows wind in the order, so wind is not the final
        # component — no article, and precipitation itself never gets one.
        components = _components(
            sky="Partly Cloudy", wind="Gentle Breeze", precipitation="Light Rain"
        )
        result = ct._template_compose(components, EN)
        assert "a gentle breeze" not in result
        assert "a light rain" not in result
        assert result == "Partly cloudy, gentle breeze, with light rain"

    def test_no_article_when_wind_is_calm(self):
        # Calm switches the connector from connector_with to connector_and,
        # so the wind-is-last-with-connector_with condition never fires.
        components = _components(sky="Partly Cloudy", wind="Calm")
        result = ct._template_compose(components, EN)
        assert "a calm" not in result
        assert result == "Partly cloudy, and calm"

    def test_no_article_for_single_wind_component(self):
        # No connector is used at all with a single component.
        components = _components(wind="Gentle Breeze")
        result = ct._template_compose(components, EN)
        assert result == "Gentle breeze"


class TestWindArticleResolution:
    """_wind_article() honors a deliberately empty locale value instead of
    falling through i18n.t()'s empty-means-untranslated resolution to the
    English "a"."""

    def test_english_article_is_a(self):
        assert ct._wind_article(EN) == "a"

    def test_german_article_is_einer(self):
        assert ct._wind_article(DE) == "einer"

    def test_russian_article_is_empty_not_english_fallback(self):
        # ru.json defines composition.wind_article as "" deliberately
        # (Russian doesn't use articles) — this must not resolve to "a".
        assert ct._wind_article("ru") == ""

    def test_missing_locale_falls_back_to_english(self):
        # A locale with no composition.wind_article key at all (genuinely
        # absent, not empty) falls through to the normal en/key chain.
        assert ct._wind_article("xx-not-a-real-locale") == "a"


class TestComponentCombinations:
    """Empty / single / two / three / four component combinations."""

    def test_zero_components(self):
        assert ct._template_compose(_components(), EN) == ""

    def test_one_component(self):
        result = ct._template_compose(_components(sky="Clear"), EN)
        assert result == "Clear"

    def test_two_components(self):
        result = ct._template_compose(
            _components(sky="Clear", temperature="Cold"), EN
        )
        assert result == "Clear, with cold"

    def test_three_components(self):
        result = ct._template_compose(
            _components(sky="Clear", temperature="Cold", wind="Gentle Breeze"), EN
        )
        assert result == "Clear, cold, with a gentle breeze"

    def test_four_components(self):
        result = ct._template_compose(
            _components(
                sky="Clear",
                temperature="Cold",
                wind="Gentle Breeze",
                precipitation="Light Rain",
            ),
            EN,
        )
        assert result == "Clear, cold, gentle breeze, with light rain"


class TestGermanLocale:
    """Non-English locale: order (sky-first, matching German's existing
    composition.order) and locale-specific article ("einer")."""

    def test_german_order_is_sky_first(self):
        components = _components(sky="Wolkenlos", temperature="Warm")
        result = ct._template_compose(components, DE)
        # "Wolkenlos" is capitalized (it's the sentence-initial word), so
        # compare case-insensitively rather than searching for the exact
        # lowercase substring.
        assert result.lower().index("wolkenlos") < result.lower().index("warm")
        assert result == "Wolkenlos, mit warm"

    def test_german_wind_article(self):
        components = _components(sky="Wolkenlos", wind="Leichte Brise")
        result = ct._template_compose(components, DE)
        assert result == "Wolkenlos, mit einer leichte brise"

    def test_german_no_article_when_wind_not_last(self):
        components = _components(
            sky="Wolkenlos", wind="Leichte Brise", precipitation="Leichter Regen"
        )
        result = ct._template_compose(components, DE)
        assert "einer leichte brise" not in result
        assert result == "Wolkenlos, leichte brise, mit leichter regen"
