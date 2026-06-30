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
