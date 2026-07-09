"""Tests for the IAST/machine-transliteration warning on entity name.en.

The guard is LOG-ONLY: validate_jsonld_entity never rejects on a bad en, it just
warns so a regression (a new bulk load reintroducing raw transliterations) is
visible. These tests pin the detector's accuracy (catches the signature, no
false positives on real English/mixed-case names) and that the warning fires
without failing validation.
"""

import logging

import pytest

from entities.validation import (
    JsonLdValidationError,
    looks_like_iast,
    validate_jsonld_entity,
)

# Real machine-transliterations pulled from the prod data set — must be flagged.
IAST_NAMES = [
    "nArAyaNI aspatAla",              # Harvard-Kyoto, नारायणी अस्पताल
    "kAThamADauM mahAnagarapAlikA",   # काठमाडौं महानगरपालिका
    "nepAla rASTra baiMka",           # नेपाल राष्ट्र बैंक
    "artha mantrAlaya",               # अर्थ मन्त्रालय
    "padmA marcenTa kampanI",         # पद्मा मर्चेन्ट कम्पनी
    "janakapuradhAma",                # जनकपुरधाम
    "vibhAga",                        # विभाग
    "Nārāyaṇī",                       # academic IAST diacritics
    "Sindhupālchok",                  # single diacritic
    "sA~da",                          # anusvara tilde
]

# Legitimate English / clean romanization / mixed-case — must NOT be flagged.
GOOD_NAMES = [
    "Narayani Hospital",
    "Kathmandu Metropolitan City",
    "Nepal Rastra Bank",
    "Ram Chandra Poudel",
    "Lakshmi B.K.",
    "Office of the Attorney General",
    "Ward No. 3 Office, Fungling Municipality",
    "Tribhuvan University",
    "AsiaInfo Yunghang Software (Beijing) Limited",  # CamelCase brand
    "UOB Singapore Bank",                            # ALLCAPS acronym
    "McMahon Associates",                            # intra-word capital
    "O'Brien Trust",
    "iPhone Store Nepal",
]


@pytest.mark.parametrize("name", IAST_NAMES)
def test_detects_transliterated_names(name):
    assert looks_like_iast(name) is True


@pytest.mark.parametrize("name", GOOD_NAMES)
def test_passes_legitimate_names(name):
    assert looks_like_iast(name) is False


def test_non_string_is_not_iast():
    assert looks_like_iast(None) is False
    assert looks_like_iast({"en": "x"}) is False
    assert looks_like_iast(123) is False


def _doc(en):
    return {
        "@context": "https://schema.org",
        "@id": "https://jawafdehi.org/entity/organization/narayani-hospital-x1",
        "@type": "Hospital",
        "name": {"ne": "नारायणी अस्पताल", "en": en},
    }


def test_iast_en_warns_but_does_not_reject(caplog):
    """A transliterated en logs a warning yet validation still succeeds."""
    doc = _doc("nArAyaNI aspatAla")
    with caplog.at_level(logging.WARNING, logger="entities.validation"):
        result = validate_jsonld_entity(doc)
    assert result is doc  # not rejected
    assert any("machine-transliterated" in r.message for r in caplog.records)


def test_clean_en_does_not_warn(caplog):
    doc = _doc("Narayani Hospital")
    with caplog.at_level(logging.WARNING, logger="entities.validation"):
        validate_jsonld_entity(doc)
    assert not any("machine-transliterated" in r.message for r in caplog.records)


def test_missing_name_still_raises():
    """The pre-existing name-required rule is unchanged by the guard."""
    doc = _doc("Narayani Hospital")
    del doc["name"]
    with pytest.raises(JsonLdValidationError):
        validate_jsonld_entity(doc)
