"""The raw-value -> alias-key fold.

Every case here is a real value from the live corpus (82 published cases, 144
distinct tag strings), not an invented one — the fold exists to collapse the mess
that is actually there.
"""

from __future__ import annotations

import pytest

from case_tags.normalize import normalize


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Capitalisation — `Ncell` and `ncell` are both live.
        ("Ncell", "ncell"),
        ("ncell", "ncell"),
        ("Illegal Enrichment", "illegal enrichment"),
        ("Illegal enrichment", "illegal enrichment"),
        # Trailing punctuation — `Abuse of Power.` and `Conflict of Interest.`
        ("Abuse of Power.", "abuse of power"),
        ("Conflict of Interest.", "conflict of interest"),
        # Separators — `land-deal`, `Money-laundering`
        ("land-deal", "land deal"),
        ("Money-laundering", "money laundering"),
        # Collapsed whitespace — `Hulak Saving  Bank Case` has a double space
        ("Hulak Saving  Bank Case", "hulak saving bank case"),
        ("  Public   Office Abuse ", "public office abuse"),
        # Devanagari is unicameral, so casefold must leave it alone
        ("कर छली", "कर छली"),
        ("एनसेल प्रकरण", "एनसेल प्रकरण"),
    ],
)
def test_folds_live_corpus_values(raw: str, expected: str) -> None:
    assert normalize(raw) == expected


def test_repairs_the_preeti_vowel_artefact() -> None:
    """ो arriving decomposed as ा + े renders almost identically but is a different
    byte sequence, so it can never match a correctly typed query. Two live values
    carry it; without this repair they are permanently unreachable.
    """
    broken = "माछापाेखरी"  # मा-छा-प + ा + े + खरी
    assert "ो" not in broken  # no real ो in the input
    assert normalize(broken) == "माछापोखरी"

    broken_asset = "सार्वजनिक सम्पत्ति हानी नाेकसानी"
    assert normalize(broken_asset) == "सार्वजनिक सम्पत्ति हानी नोकसानी"


def test_repairs_the_au_vowel_too() -> None:
    """Same artefact class: ौ (U+094C) decomposed as ा + ै."""
    assert normalize("नाैकर") == "नौकर"


def test_nfc_normalizes_composed_and_decomposed_devanagari() -> None:
    """The same word typed composed vs decomposed must land in one bucket."""
    composed = "का"  # का
    decomposed = "का"
    assert normalize(composed) == normalize(decomposed)


def test_is_idempotent() -> None:
    """Alias keys are STORED normalized, so re-normalizing a key must be a no-op —
    otherwise a lookup could miss a row that is already in the table."""
    for raw in ("Money-laundering", "  Conflict of Interest. ", "माछापाेखरी"):
        once = normalize(raw)
        assert normalize(once) == once


def test_does_not_strip_interior_punctuation() -> None:
    """Only the ends are trimmed. `K.P. Sharma Oli` keeps its stops — collapsing
    them would merge genuinely different values."""
    assert normalize("K.P. Sharma Oli") == "k.p. sharma oli"


def test_empty_and_whitespace_only() -> None:
    assert normalize("") == ""
    assert normalize("   ") == ""
    assert normalize(" . ") == ""
