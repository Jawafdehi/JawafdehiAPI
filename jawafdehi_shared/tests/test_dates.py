"""Tests for the shared Bikram Sambat → Gregorian date contract."""

from datetime import date

from jawafdehi_shared.dates import bs_to_ad, bs_to_ad_iso


def test_bs_to_ad_basic():
    # BS 2080-03-24 → AD 2023-07-09 (the anchor used across the sourcing shapers).
    assert bs_to_ad("2080-03-24") == date(2023, 7, 9)


def test_bs_to_ad_iso_string():
    assert bs_to_ad_iso("2080-03-24") == "2023-07-09"


def test_devanagari_digits_accepted():
    # Only the CIAA copy handled Devanagari numerals; the shared helper does too.
    assert bs_to_ad_iso("२०८०-०३-२४") == "2023-07-09"


def test_slash_separator_accepted():
    assert bs_to_ad_iso("2080/03/24") == "2023-07-09"


def test_empty_is_none():
    assert bs_to_ad("") is None
    assert bs_to_ad(None) is None
    assert bs_to_ad_iso("") is None


def test_unparseable_is_none_never_raises():
    assert bs_to_ad("not-a-date") is None
    assert bs_to_ad("2080-03") is None  # wrong arity
    assert bs_to_ad_iso("garbage") is None
