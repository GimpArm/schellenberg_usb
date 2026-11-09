"""Direct function-level tests for runtime_translation_text.py.

Every other test file in this project calls these functions only
incidentally, as a side effect of whichever UI text happens to need
translating - this file is the one place that tests the module's own logic
directly: the English/key fallback chain in runtime_translation_text(),
translate_phrase()/yes_no()'s flat literal lookup, translate_block_reason()'s
own branching, and - most importantly for catching a future translation gap
before it ships - that every key present for one language is present for
all four, in both translation tables.

These are plain synchronous functions with no Home Assistant dependency
beyond reading hass.config.language, so a real `hass` fixture is not
needed - a minimal stand-in covers every call here, including hass=None.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from custom_components.schellenberg_usb.runtime_translation_text import (
    _PHRASE_TRANSLATIONS,
    _RUNTIME_TRANSLATION_TEXT,
    runtime_translation_text,
    translate_block_reason,
    translate_phrase,
    yes_no,
)


def _hass_with_language(language: str) -> Any:
    """A minimal stand-in for HomeAssistant - only .config.language is read."""
    return SimpleNamespace(config=SimpleNamespace(language=language))


# ---------------------------------------------------------------------------
# Translation-table integrity: every key present for one language must be
# present for all the others, in both tables. This is what actually
# enforces "every UI word gets a real translation in all 4 languages" going
# forward - a missing key would otherwise only surface as silent English
# fallback text in a language that is supposed to be fully translated.
# ---------------------------------------------------------------------------


def test_runtime_translation_text_keys_match_across_all_languages() -> None:
    english_keys = set(_RUNTIME_TRANSLATION_TEXT["en"])
    assert english_keys, "expected the English table not to be empty"
    for language in ("de", "es", "fr"):
        assert set(_RUNTIME_TRANSLATION_TEXT[language]) == english_keys, language


def test_phrase_translation_keys_match_across_all_languages() -> None:
    languages = list(_PHRASE_TRANSLATIONS)
    assert languages, "expected at least one translated language"
    reference_keys = set(_PHRASE_TRANSLATIONS[languages[0]])
    for language in languages[1:]:
        assert set(_PHRASE_TRANSLATIONS[language]) == reference_keys, language


# ---------------------------------------------------------------------------
# runtime_translation_text(): template lookup, parameter filling, and the
# English/key fallback chain for a missing language or key.
# ---------------------------------------------------------------------------


def test_runtime_translation_text_fills_in_a_german_template() -> None:
    hass = _hass_with_language("de")

    result = runtime_translation_text(hass, "device_fallback_name", device_id="ABCDEF")

    assert result == "Gerät ABCDEF"


def test_runtime_translation_text_fills_in_a_french_template() -> None:
    hass = _hass_with_language("fr")

    result = runtime_translation_text(
        hass, "cover_stop_failed", name="Salon", reason="x"
    )

    assert result == "Impossible d'arrêter Salon : x"


def test_runtime_translation_text_falls_back_to_english_for_an_unknown_language() -> (
    None
):
    hass = _hass_with_language("it")

    assert runtime_translation_text(hass, "none_word") == "None"


def test_runtime_translation_text_falls_back_to_the_key_itself_when_unknown() -> None:
    hass = _hass_with_language("de")

    assert runtime_translation_text(hass, "no_such_translation_key") == (
        "no_such_translation_key"
    )


def test_runtime_translation_text_defaults_to_english_with_no_hass() -> None:
    assert runtime_translation_text(None, "none_word") == "None"


# ---------------------------------------------------------------------------
# translate_phrase()/yes_no(): flat literal lookup for short, reusable
# English words, with English itself always passed straight through.
# ---------------------------------------------------------------------------


def test_translate_phrase_translates_a_known_literal_to_german() -> None:
    hass = _hass_with_language("de")

    assert translate_phrase(hass, "opening") == "öffnend"


def test_translate_phrase_leaves_english_unchanged() -> None:
    hass = _hass_with_language("en")

    assert translate_phrase(hass, "opening") == "opening"


def test_translate_phrase_falls_back_to_the_original_for_an_unknown_phrase() -> None:
    hass = _hass_with_language("de")

    assert translate_phrase(hass, "some future phrase") == "some future phrase"


def test_translate_phrase_translates_the_two_previously_missing_phrases() -> None:
    """Regression: "fresh calibration result" and "measured during pairing /
    not confirmed since restart" - cover.py's async_added_to_hass() sets
    both whenever a blind's startup position comes from a calibration that
    just completed - were present in English but missing from
    _PHRASE_TRANSLATIONS for de/es/fr entirely. Invisible to
    test_phrase_translation_keys_match_across_all_languages above, which
    only checks the three non-English languages against each other: all
    three were missing both keys identically, so they stayed mutually
    consistent with each other despite the gap. A real installation in any
    of these three languages saw the plain English phrase mixed into
    otherwise-translated text instead of a crash, via translate_phrase()'s
    own silent fallback to the key itself - confirmed unchanged by this
    fix for a phrase that is still genuinely missing, see the
    falls-back-to-the-original test above.
    """
    for language in ("de", "es", "fr"):
        hass = _hass_with_language(language)
        for phrase in (
            "fresh calibration result",
            "measured during pairing / not confirmed since restart",
        ):
            translated = translate_phrase(hass, phrase)
            assert translated != phrase, (language, phrase)


def test_yes_no_translates_both_values_to_spanish() -> None:
    hass = _hass_with_language("es")

    assert yes_no(hass, True) == "Sí"
    assert yes_no(hass, False) == "No"


# ---------------------------------------------------------------------------
# translate_block_reason(): api.py's stable transmit_block_reason codes,
# including the two codes that carry their own embedded value
# ("wrong_mode:<mode>", "pending_retry:<payload>").
# ---------------------------------------------------------------------------


def test_translate_block_reason_passes_none_through_unchanged() -> None:
    hass = _hass_with_language("de")

    assert translate_block_reason(hass, None) is None


def test_translate_block_reason_translates_a_plain_code() -> None:
    hass = _hass_with_language("de")

    assert (
        translate_block_reason(hass, "disconnected")
        == "der USB-Stick ist nicht verbunden"
    )


def test_translate_block_reason_translates_the_embedded_mode_for_wrong_mode() -> None:
    hass = _hass_with_language("de")

    result = translate_block_reason(hass, "wrong_mode:listening")

    assert result == (
        "der Stick ist im Modus Empfangsbereit, benötigt wird aber der "
        "Empfangsbereit-Modus"
    )


def test_translate_block_reason_keeps_the_raw_payload_for_pending_retry() -> None:
    """Unlike wrong_mode's embedded value, pending_retry's payload is a raw
    wire-format hex string, not a translatable phrase - it must come
    through unchanged inside the translated sentence.
    """
    hass = _hass_with_language("de")

    result = translate_block_reason(hass, "pending_retry:tr10910000")

    assert "tr10910000" in result  # type: ignore[operator]


def test_translate_block_reason_returns_an_unknown_code_unchanged() -> None:
    hass = _hass_with_language("de")

    assert translate_block_reason(hass, "some_future_code") == "some_future_code"
