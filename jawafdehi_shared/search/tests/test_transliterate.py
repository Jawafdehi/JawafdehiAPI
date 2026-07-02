"""Tests for the shared Devanagari<->Latin transliteration.

Round-trips when the indic-transliteration backend is available; otherwise asserts
the documented identity fallback. Pure-python, no live OpenSearch.
"""

import pytest

from jawafdehi_shared.search import transliterate


def test_backend_name_consistent_with_availability():
    if transliterate.backend_available():
        assert transliterate.backend_name() == "indic-transliteration"
    else:
        assert transliterate.backend_name() == "fallback"


def test_empty_input_passthrough():
    assert transliterate.to_roman("") == ""
    assert transliterate.to_devanagari("") == ""


def test_to_roman_produces_latin_when_backend_available():
    deva = "नेपाल"  # "Nepal"
    out = transliterate.to_roman(deva)
    if transliterate.backend_available():
        # Output should be ASCII-ish Latin (no Devanagari code points remain).
        assert not any("ऀ" <= ch <= "ॿ" for ch in out)
        assert out  # non-empty
    else:
        # Documented fallback: identity.
        assert out == deva


def test_round_trip_or_fallback():
    deva = "काठमाडौं"  # "Kathmandu"
    roman = transliterate.to_roman(deva)
    back = transliterate.to_devanagari(roman)
    if transliterate.backend_available():
        # IAST round-trips losslessly for well-formed Devanagari.
        assert back == deva
    else:
        # Fallback: both are identity, so back == roman == deva.
        assert roman == deva
        assert back == deva


@pytest.mark.skipif(
    not transliterate.backend_available(), reason="indic-transliteration not installed"
)
def test_latin_to_devanagari_produces_devanagari():
    out = transliterate.to_devanagari("nepāla")
    assert any("ऀ" <= ch <= "ॿ" for ch in out)


# ── Colloquial romanization (the Latin-query recall fix) ─────────────────────────
# The fold/schwa helpers operate on IAST strings, so they are deterministic and run
# WITHOUT the transliteration backend; only the end-to-end Devanagari test is gated.


def test_fold_diacritics_strips_to_ascii():
    assert transliterate._fold_diacritics("tāla") == "tala"
    assert transliterate._fold_diacritics("nirmāṇamā") == "nirmanama"
    assert transliterate._fold_diacritics("bharata") == "bharata"


def test_colloquial_fold_applies_sound_map():
    # ś/ṣ → sh, ṛ → ri, then the generic fold handles the rest.
    assert transliterate._colloquial_fold("śarmā") == "sharma"
    assert transliterate._colloquial_fold("kṛṣṇa") == "krishna"


def test_delete_inherent_schwa_keeps_long_vowels():
    assert transliterate._delete_inherent_schwa("bharata") == "bharat"
    assert transliterate._delete_inherent_schwa("rāma") == "rām"
    assert transliterate._delete_inherent_schwa("sītā") == "sītā"  # long ā, not a schwa
    assert transliterate._delete_inherent_schwa("na") == "na"  # single syllable kept


@pytest.mark.skipif(
    not transliterate.backend_available(), reason="indic-transliteration not installed"
)
def test_to_roman_colloquial_emits_both_schwa_forms():
    # The reported case: "Bharat" must be reachable from the Devanagari title.
    out = transliterate.to_roman_colloquial("भरत ताल")  # "Bharat Tal"
    tokens = out.split()
    assert "bharat" in tokens and "tal" in tokens  # schwa-deleted spelling present
    assert "bharata" in tokens  # schwa-kept spelling also present
    assert all(ch.isascii() for ch in out)  # fully folded to ASCII


def test_to_roman_colloquial_empty_passthrough():
    assert transliterate.to_roman_colloquial("") == ""
