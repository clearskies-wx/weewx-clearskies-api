"""Unit tests for the terse-tier conditions text composer (ADR-044 §9,
NWS-style order/casing/wind-forms patch).

Covers weewx_clearskies_api/sse/conditions_text.py's `_template_compose()`
and `_wind_with_form()` — the locale-driven component order, NWS-style
sentence case (component values lowercased unless the locale opts out via
``composition.lowercase_components`` — German only, whose common nouns are
always capitalized), and the wind-specific "with"-position replacement
text (``composition.wind_with_forms``), which folds article, grammatical
gender, and case-inflection (German dative after "mit", Russian
instrumental after "с") into a single per-label lookup rather than one
flat article string per locale.

These exercise the composition helpers directly (as `_current_sky_label`
is exercised directly in test_gfe_composer_current.py) rather than driving
the full `build_weather_text()` pipeline, since composition order/casing/
wind-form logic is independent of how the individual component labels
were derived (sky classification, Beaufort lookup, etc.) — and driving
the full pipeline would make some assertions dependent on the current
time of day (day/night sky mapping).

Module under test: weewx_clearskies_api/sse/conditions_text.py
"""

from __future__ import annotations

from weewx_clearskies_api import i18n
from weewx_clearskies_api.sse import conditions_text as ct

# Production startup (__main__.py) calls i18n.load_locales() explicitly
# before serving any request (see i18n.py's module docstring). Load it here
# too so composition.order/wind_with_forms resolve correctly even when this
# is the very first i18n lookup in the test process — _template_compose
# reads i18n._locales directly for its order lookup, which (unlike i18n.t())
# does not itself trigger the lazy-load.
i18n.load_locales()

EN = "en"
DE = "de"
FR = "fr"
ES = "es"
RU = "ru"


def _components(
    *,
    temperature: str | None = None,
    sky: str | None = None,
    wind: str | None = None,
    precipitation: str | None = None,
    wind_key: str | None = None,
    wind_gust_suffix: str | None = None,
) -> dict[str, str | None]:
    components: dict[str, str | None] = {
        "temperature": temperature,
        "sky": sky,
        "wind": wind,
        "precipitation": precipitation,
    }
    if wind_key is not None:
        components["_wind_key"] = wind_key
    if wind_gust_suffix is not None:
        components["_wind_gust_suffix"] = wind_gust_suffix
    return components


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
    every component is lowercased before assembly for locales that don't
    opt out via composition.lowercase_components."""

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


class TestWindWithForms:
    """The locale's wind_with_forms entry (keyed by the HYBRID_WIND_SCALE
    label the wind speed mapped to) replaces the wind component only when
    wind is the final component AND the connector is connector_with (i.e.
    not Calm)."""

    def test_form_substituted_when_wind_is_last_with_connector(self):
        components = _components(
            sky="Partly Cloudy", wind="Gentle Breeze", wind_key="gentle_breeze"
        )
        result = ct._template_compose(components, EN)
        assert result == "Partly cloudy, with a gentle breeze"

    def test_form_substituted_with_three_components(self):
        components = _components(
            sky="Partly Cloudy",
            temperature="Warm and Humid",
            wind="Gentle Breeze",
            wind_key="gentle_breeze",
        )
        result = ct._template_compose(components, EN)
        assert result == "Partly cloudy, warm and humid, with a gentle breeze"

    def test_no_substitution_when_wind_is_not_last_component(self):
        # Precipitation follows wind in the order, so wind is not the final
        # component — no substitution, and precipitation itself never gets
        # a wind form.
        components = _components(
            sky="Partly Cloudy",
            wind="Gentle Breeze",
            precipitation="Light Rain",
            wind_key="gentle_breeze",
        )
        result = ct._template_compose(components, EN)
        assert "a gentle breeze" not in result
        assert "a light rain" not in result
        assert result == "Partly cloudy, gentle breeze, with light rain"

    def test_no_substitution_when_wind_is_calm(self):
        # Calm switches the connector from connector_with to connector_and,
        # so the wind-is-last-with-connector_with condition never fires.
        components = _components(sky="Partly Cloudy", wind="Calm", wind_key="calm")
        result = ct._template_compose(components, EN)
        assert "a calm" not in result
        assert result == "Partly cloudy, and calm"

    def test_no_substitution_for_single_wind_component(self):
        # No connector is used at all with a single component.
        components = _components(wind="Gentle Breeze", wind_key="gentle_breeze")
        result = ct._template_compose(components, EN)
        assert result == "Gentle breeze"

    def test_no_substitution_when_wind_key_absent(self):
        # No _wind_key in the components dict (e.g. hand-built test data,
        # or a caller that never threaded it through) -> _wind_with_form
        # returns "" -> falls back to the plain (lowercased) label.
        components = _components(sky="Partly Cloudy", wind="Gentle Breeze")
        result = ct._template_compose(components, EN)
        assert result == "Partly cloudy, with gentle breeze"

    def test_english_windy_conditions_form(self):
        # 35 mph maps to the "windy" HYBRID_WIND_SCALE key (30 <= mph < 40).
        # GFE adjective labels take "conditions", not the Beaufort "a"
        # article -- "with windy conditions", never "with a windy".
        components = _components(sky="Clear", wind="Windy", wind_key="windy")
        result = ct._template_compose(components, EN)
        assert result == "Clear, with windy conditions"
        assert "with a windy" not in result

    def test_english_strong_winds_plural_no_article(self):
        # 55 mph maps to the "strong_winds" HYBRID_WIND_SCALE key
        # (50 <= mph < 74). GFE plural labels take no article at all --
        # "with strong winds", never "with a strong winds".
        components = _components(sky="Clear", wind="Strong Winds", wind_key="strong_winds")
        result = ct._template_compose(components, EN)
        assert result == "Clear, with strong winds"
        assert "with a strong winds" not in result

    def test_gust_suffix_preserved_through_form_substitution(self):
        # wind_with_forms replaces the whole wind component text, but the
        # gust qualifier (tracked separately in _wind_gust_suffix) must
        # survive the substitution rather than being silently dropped.
        components = _components(
            sky="Clear",
            wind="Gentle Breeze with gusts to around 25 mph",
            wind_key="gentle_breeze",
            wind_gust_suffix="with gusts to around 25 mph",
        )
        result = ct._template_compose(components, EN)
        assert result == "Clear, with a gentle breeze with gusts to around 25 mph"

    def test_french_beaufort6_uses_masculine_article(self):
        # Beaufort 6 "Vent frais" (masculine noun "vent") must use "un",
        # not the feminine "une" used for breeze (fem. "brise").
        components = _components(
            sky="Ciel dégagé", wind="Vent frais", wind_key="strong_breeze"
        )
        result = ct._template_compose(components, FR)
        assert "un vent frais" in result.lower()
        assert "une vent frais" not in result.lower()

    def test_french_gentle_breeze_uses_feminine_article(self):
        components = _components(
            sky="Ciel dégagé", wind="Petite brise", wind_key="gentle_breeze"
        )
        result = ct._template_compose(components, FR)
        assert "une petite brise" in result.lower()

    def test_spanish_gentle_breeze_article(self):
        components = _components(
            sky="Despejado", wind="Brisa ligera", wind_key="gentle_breeze"
        )
        result = ct._template_compose(components, ES)
        assert "una brisa ligera" in result.lower()


class TestWindWithFormResolution:
    """_wind_with_form() reads composition.wind_with_forms.{key} directly,
    honoring deliberately empty entries and returning "" (never falling
    back to English) when the locale or key has no entry."""

    def test_english_form_for_light_breeze(self):
        assert ct._wind_with_form("light_breeze", EN) == "a light breeze"

    def test_english_windy_form_appends_conditions(self):
        assert ct._wind_with_form("windy", EN) == "windy conditions"

    def test_english_strong_winds_form_is_empty(self):
        # Deliberately empty: "Strong Winds" already reads correctly with
        # no article ("with strong winds") once lowered by the caller.
        assert ct._wind_with_form("strong_winds", EN) == ""

    def test_german_light_breeze_is_dative_feminine(self):
        # "Brise" is feminine; dative after "mit" is "einer leichten".
        assert ct._wind_with_form("light_breeze", DE) == "einer leichten Brise"

    def test_german_strong_breeze_is_dative_masculine(self):
        # "Wind" is masculine; dative after "mit" is "einem starken".
        assert ct._wind_with_form("strong_breeze", DE) == "einem starken Wind"

    def test_german_windy_form_is_empty(self):
        # GFE-tier German labels have no wind_with_forms entry -- the
        # regular (capitalized, per lowercase_components: false) label is
        # used as-is.
        assert ct._wind_with_form("windy", DE) == ""

    def test_russian_light_breeze_is_instrumental(self):
        # Russian "с" (with) governs the instrumental case.
        assert ct._wind_with_form("light_breeze", RU) == "лёгким ветром"

    def test_russian_windy_form_is_empty(self):
        assert ct._wind_with_form("windy", RU) == ""

    def test_none_key_returns_empty(self):
        assert ct._wind_with_form(None, EN) == ""

    def test_unknown_key_in_known_locale_returns_empty(self):
        assert ct._wind_with_form("not_a_real_key", EN) == ""

    def test_missing_locale_returns_empty_not_english_fallback(self):
        # A locale with no composition.wind_with_forms at all (genuinely
        # absent, not an empty dict) returns "" -- unlike the old
        # wind_article helper, this does NOT fall through to English.
        # The caller (_template_compose) falls back to the plain label.
        assert ct._wind_with_form("gentle_breeze", "xx-not-a-real-locale") == ""


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
            _components(
                sky="Clear", temperature="Cold", wind="Gentle Breeze",
                wind_key="gentle_breeze",
            ),
            EN,
        )
        assert result == "Clear, cold, with a gentle breeze"

    def test_four_components(self):
        result = ct._template_compose(
            _components(
                sky="Clear",
                temperature="Cold",
                wind="Gentle Breeze",
                precipitation="Light Rain",
                wind_key="gentle_breeze",
            ),
            EN,
        )
        assert result == "Clear, cold, gentle breeze, with light rain"


class TestGermanLocale:
    """Non-English locale: order (sky-first, matching German's existing
    composition.order), locale-specific dative wind forms, and preserved
    noun capitalization (composition.lowercase_components: false)."""

    def test_german_order_is_sky_first(self):
        components = _components(sky="Wolkenlos", temperature="Warm")
        result = ct._template_compose(components, DE)
        # "Wolkenlos" is capitalized (it's the sentence-initial word), so
        # compare case-insensitively rather than searching for the exact
        # lowercase substring.
        assert result.lower().index("wolkenlos") < result.lower().index("warm")
        # German does not lowercase components (composition.lowercase_
        # components: false) -- "Warm" stays capitalized, not "warm".
        assert result == "Wolkenlos, mit Warm"

    def test_german_wind_dative_form(self):
        components = _components(
            sky="Wolkenlos", wind="Leichte Brise", wind_key="light_breeze"
        )
        result = ct._template_compose(components, DE)
        assert result == "Wolkenlos, mit einer leichten Brise"

    def test_german_no_substitution_when_wind_not_last(self):
        components = _components(
            sky="Wolkenlos",
            wind="Leichte Brise",
            precipitation="Leichter Regen",
            wind_key="light_breeze",
        )
        result = ct._template_compose(components, DE)
        assert "einer leichten Brise" not in result
        # Nouns stay capitalized throughout -- no component is lowercased.
        assert result == "Wolkenlos, Leichte Brise, mit Leichter Regen"
