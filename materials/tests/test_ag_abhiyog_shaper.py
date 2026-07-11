"""Pure-shaping tests for the AG अभियोगपत्र → Material JSON-LD projection.

No DB / no settings beyond the IRI base — the shaper is a pure function, so these
run fast and pin the IRI grammar, the digit-transliteration (the collision bug),
the charge_sheet type, the RAW/MARKDOWN media, the embedded full text, and the
deliberate omission of entity links.
"""

from materials.jsonld import validate_material_jsonld
from materials.sourcing.ag.shaper import (
    AG_SOURCE,
    SOURCE_TYPE,
    _slug_ident,
    ag_abhiyog_to_jsonld,
)


def test_devanagari_case_number_keeps_digits():
    # ०८२-FT-०५२४ must NOT collapse to "ft" (would collide across all FT cases)
    assert _slug_ident("०८२-FT-०५२४") == "082-ft-0524"


def test_slug_falls_back_when_no_alphanumeric():
    # a purely-Devanagari-consonant candidate has no ASCII alnum -> skip to fallback
    assert _slug_ident("अभियोग", "118689") == "118689"


def test_iri_keyed_on_record_id_not_case_number():
    # court_case_no repeats across offices; the @id must key on the unique
    # record_id so distinct indictments never collide/overwrite on upsert.
    d1, _ = ag_abhiyog_to_jsonld({"court_case_no": "082-C1-0069", "record_id": 111, "name": "a"})
    d2, _ = ag_abhiyog_to_jsonld({"court_case_no": "082-C1-0069", "record_id": 222, "name": "b"})
    assert d1["@id"] != d2["@id"]
    assert d1["@id"].endswith("/111")
    assert d2["@id"].endswith("/222")


def test_missing_record_id_raises():
    import pytest

    # no record_id -> cannot mint a unique ident -> must fail loudly, not collide
    with pytest.raises(ValueError):
        ag_abhiyog_to_jsonld({"court_case_no": "082-C1-0069", "name": "a"})


def test_shape_is_valid_charge_sheet_material():
    rec = {
        "court_case_no": "०८२-FT-०५२४",
        "record_id": 118689,
        "name": "वादी नेपाल सरकार प्रतिवादी केशर बहादुर गुरुङ्ग समेत",
        "office": "विशेष सरकारी वकील कार्यालय",
        "date_ad": "2026-07-07",
    }
    doc, mt = ag_abhiyog_to_jsonld(
        rec,
        markdown="श्री विशेष अदालत, काठमाडौं समक्ष पेश गरेको आरोप-पत्र",
        pdf_url="https://ngm-store.jawafdehi.org/ag/abc.pdf",
        markdown_url="https://ngm-store.jawafdehi.org/ag/abc.md",
    )
    validate_material_jsonld(doc, iri=doc["@id"])  # raises on any contract breach
    assert mt == "charge_sheet"
    assert doc["@id"].startswith(f"https://jawafdehi.org/material/{AG_SOURCE}/")
    assert doc["@type"] == "DigitalDocument"
    assert doc["additionalType"] == "jawafdehi:ChargeSheet"
    assert doc["jawafdehi:sourceType"] == SOURCE_TYPE
    assert doc["jawafdehi:caseNumber"] == "०८२-FT-०५२४"
    assert doc["datePublished"] == "2026-07-07"
    roles = [m["jawafdehi:linkRole"] for m in doc["associatedMedia"]]
    assert roles == ["RAW", "MARKDOWN"]
    assert doc["text"]["ne"].startswith("श्री विशेष अदालत")
    # entity links deliberately omitted (ingest now, link later)
    assert "about" not in doc


def test_bs_date_converted_to_ad_when_no_explicit_ad():
    # created_date_np present, no date_ad -> the shaper converts BS→AD itself (via
    # the shared jawafdehi_shared.dates helper). The BS original is preserved on
    # filingDateBS; datePublished carries the Gregorian conversion.
    doc, _ = ag_abhiyog_to_jsonld({"record_id": 86132, "name": "x", "created_date_np": "2082-3-29"})
    assert doc["jawafdehi:filingDateBS"] == "2082-3-29"
    assert doc["datePublished"] == "2025-07-13"


def test_unconvertible_bs_date_falls_back_to_bs_string():
    # a BS date the converter can't parse -> datePublished falls back to the BS
    # string so the date is never dropped; filingDateBS still preserved.
    doc, _ = ag_abhiyog_to_jsonld({"record_id": 5, "name": "x", "created_date_np": "not-a-date"})
    assert doc["jawafdehi:filingDateBS"] == "not-a-date"
    assert doc["datePublished"] == "not-a-date"


def test_ad_date_preferred_when_present():
    doc, _ = ag_abhiyog_to_jsonld(
        {"record_id": 1, "name": "x", "created_date_np": "2082-3-29", "date_ad": "2025-07-13"})
    assert doc["datePublished"] == "2025-07-13"
    assert doc["jawafdehi:filingDateBS"] == "2082-3-29"


def test_publisher_carries_office():
    doc, _ = ag_abhiyog_to_jsonld({"record_id": 7, "name": "x", "office": "उच्च सरकारी वकील कार्यालय, इलाम"})
    assert doc["publisher"]["name"]["ne"] == "उच्च सरकारी वकील कार्यालय, इलाम"


def test_no_ident_raises():
    import pytest

    with pytest.raises(ValueError):
        ag_abhiyog_to_jsonld({"name": "x"})  # no court_case_no, no record_id


def test_markdown_optional():
    # a doc with no markdown yet (pre-conversion) still shapes + validates
    doc, _ = ag_abhiyog_to_jsonld({"record_id": 42, "name": "x"}, pdf_url="https://r2/x.pdf")
    validate_material_jsonld(doc, iri=doc["@id"])
    assert "text" not in doc
    assert [m["jawafdehi:linkRole"] for m in doc["associatedMedia"]] == ["RAW"]
