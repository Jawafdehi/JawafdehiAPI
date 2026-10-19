"""The phonetic search key: a Latin-typed query reaching a Devanagari caption."""

from __future__ import annotations

import pytest

from jawafdehi_shared.search.transliterate import (
    backend_available,
    matches_search,
    search_key,
)

pytestmark = pytest.mark.skipif(
    not backend_available(),
    reason="indic-transliteration backend unavailable; romanization is identity",
)

CAPTION = "बढी उजुरी परेका १० सङ्घीय मन्त्रालयको विवरण"


@pytest.mark.parametrize(
    "query",
    [
        # The spelling the scheme produces...
        "vivaran",
        # ...and the three a reader actually types. व is romanized "v", spoken
        # closer to "w", and written "b" as often as either.
        "viwaran",
        "wiwaran",
        "bibaran",
        # Aspirates, which almost nobody types.
        "badhi",
        "badi",
        # Vowel length, which a Latin keyboard does not carry.
        "ujuri",
        "ujuree",
        # Order-free: both of these must find the row.
        "ujuri badhi",
        "badhi ujuri",
    ],
)
def test_latin_queries_reach_a_devanagari_caption(query: str) -> None:
    assert matches_search(CAPTION, query), f"{query!r} should match {CAPTION!r}"


def test_ksha_is_read_as_chha_in_nepali() -> None:
    # क्ष is "ksha" by the scheme and "chha" by ear; both are typed.
    for query in ("kshetra", "chhetra"):
        assert matches_search("विषयगत क्षेत्र", query)


def test_a_devanagari_query_still_matches_literally() -> None:
    # The literal pass runs first, so this costs no transliteration at all.
    assert matches_search(CAPTION, "उजुरी")


def test_an_english_caption_still_matches_an_english_query() -> None:
    assert matches_search("Annual report summary", "annual")


def test_unrelated_queries_do_not_match() -> None:
    assert not matches_search(CAPTION, "kathmandu")
    assert not matches_search("Annual report", "zzz")


def test_every_query_token_must_appear() -> None:
    # Token-AND, not token-OR: a second word narrows the result rather than
    # widening it, which is what anyone typing two words expects.
    assert not matches_search(CAPTION, "ujuri kathmandu")


def test_an_empty_query_matches_everything() -> None:
    # The listing shows in full before anyone types.
    assert matches_search(CAPTION, "   ")


def test_a_query_that_folds_to_nothing_matches_nothing() -> None:
    # "..." folds away; without the guard every row would match it.
    assert not matches_search(CAPTION, "...")


def test_search_key_carries_both_scripts() -> None:
    key = search_key("विवरण")
    assert "bibaran" in key
