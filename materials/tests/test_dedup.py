"""Unit tests for the pure duplicate-audit matcher (materials/dedup.py).

DB-free: the matcher is a pure function over a Material JSON-LD ``data`` dict, so
these run without a database (mirroring the person-sector classifier tests). The
matcher decides, by natural key, whether a ``/material/jawafdehi/*`` document
duplicates a canonical corpus material.
"""

from __future__ import annotations

from materials.dedup import Outcome, extract_canonical_key, normalize_digits


def _jawaf(source_type, name):
    """A minimal jawafdehi Material ``data`` dict (name is Nepali free text)."""
    return {
        "@id": "https://jawafdehi.org/material/jawafdehi/20260507.deadbeef",
        "jawafdehi:sourceType": source_type,
        "name": {"ne": name},
    }


def test_normalize_devanagari_digits():
    assert normalize_digits("३१५५") == "3155"
    assert normalize_digits("०८१-CR-०१३८") == "081-CR-0138"
    assert normalize_digits("no digits") == "no digits"


def test_ciaa_press_release_matches_by_number():
    data = _jawaf("CIAA_PRESS_RELEASE", "CIAA प्रेस विज्ञप्ति नं. ३१५५ — आरोपपत्र दायर")
    outcome, ref = extract_canonical_key(data)
    assert outcome == Outcome.HAS_KEY
    assert ref.source == "ciaa_press_release"
    assert ref.ident == "3155"
    assert ref.case_number is None
    assert "3155" in ref.signal


def test_court_order_matches_by_case_number():
    data = _jawaf("COURT_ORDER", "विशेष अदालत मुद्दा नं. ०८१-CR-०१३८ आदेश")
    outcome, ref = extract_canonical_key(data)
    assert outcome == Outcome.HAS_KEY
    assert ref.source == "court_order"
    assert ref.ident is None
    assert ref.case_number == "081-cr-0138"


def test_court_filing_other_also_uses_case_number():
    data = _jawaf("COURT_FILING_OTHER", "सर्वोच्च अदालत ०७५-WF-०००५ रिट")
    outcome, ref = extract_canonical_key(data)
    assert outcome == Outcome.HAS_KEY
    assert ref.source == "court_order"
    assert ref.case_number == "075-wf-0005"


def test_charge_sheet_has_no_canonical_key():
    # The AG corpus is keyed by an internal AG id, not the court-case number, so a
    # jawafdehi charge sheet has no shared natural key to match on.
    data = _jawaf("AG_ABHIYOG_PATRA", "आरोपपत्र — विशेष अदालत मुद्दा नं. ०८१-CR-०१३८")
    outcome, ref = extract_canonical_key(data)
    assert outcome == Outcome.NO_CANONICAL_KEY
    assert ref is None


def test_law_and_report_have_no_canonical_key():
    assert extract_canonical_key(_jawaf("LAW_OR_BILL", "सम्पत्ति शुद्धीकरण ऐन"))[0] == Outcome.NO_CANONICAL_KEY
    assert extract_canonical_key(_jawaf("OAG_AUDIT_REPORT", "महालेखा प्रतिवेदन"))[0] == Outcome.NO_CANONICAL_KEY


def test_news_and_social_and_misc_have_no_twin():
    for st in ("NEWS", "SOCIAL_MEDIA", "MISC"):
        outcome, ref = extract_canonical_key(_jawaf(st, "कुनै समाचार"))
        assert outcome == Outcome.NO_CANONICAL_TWIN
        assert ref is None


def test_missing_or_unknown_source_type_has_no_twin():
    assert extract_canonical_key({"name": {"ne": "x"}})[0] == Outcome.NO_CANONICAL_TWIN
    assert extract_canonical_key(_jawaf("SOMETHING_NEW", "x"))[0] == Outcome.NO_CANONICAL_TWIN


def test_press_release_without_a_number_has_no_key():
    outcome, ref = extract_canonical_key(_jawaf("CIAA_PRESS_RELEASE", "CIAA प्रेस विज्ञप्ति — विवरण"))
    assert outcome == Outcome.NO_CANONICAL_KEY
    assert ref is None


def test_name_as_plain_string_parses():
    data = {
        "@id": "https://jawafdehi.org/material/jawafdehi/x.y",
        "jawafdehi:sourceType": "CIAA_PRESS_RELEASE",
        "name": "प्रेस विज्ञप्ति नं. ३१५५",
    }
    outcome, ref = extract_canonical_key(data)
    assert outcome == Outcome.HAS_KEY
    assert ref.ident == "3155"
