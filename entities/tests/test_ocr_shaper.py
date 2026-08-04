"""Pure-shaper tests for the OCR company → entity projection.

No DB, no network: build an OCR ``company`` dict, call ``ocr_company_to_jsonld``,
and assert the authoring payload (its ``@id``/``@type``/slug/dates/identifiers/
status filter) — and, crucially, that a publishable payload passes the real
``validate_create_payload`` so a live POST cannot 422 on shape.
"""

from __future__ import annotations

from entities.sourcing.ocr.shaper import (
    COMPANY_PREFIX,
    OCR_AUTHORITY,
    company_slug,
    ocr_company_to_jsonld,
)
from entities.write_validation import validate_create_payload


def _company(**over):
    """A realistic APPROVED private-company record; override any field."""
    rec = {
        "companyId": 1,
        "companyNameEnglish": "Sipradi Yatri",
        "companyNameNepali": "सिप्रदी यात्री",
        "registrationNumber": "350003",
        "addressLine": "Chandragiri Municipality-14, Kathmandu",
        "addressLineNp": "चन्द्रागिरी नगरपालिका-14, काठमाडौं",
        "companyTypeCategory": {
            "key": "PRIVATE_MULTIPLEPROPRIETOR_ALLNEPALI",
            "valueEnglish": "Private >> Multiple Ownership >> All Nepali Ownership",
            "valueNepali": "प्राइभेट",
            "baseValue": "PRIVATE",
        },
        "panNumber": "621xxx032",
        "registrationDateAD": "2024-07-14",
        "registrationDateBS": "2081-03-30",
        "status": "APPROVED",
        "deRegisteredDateAD": None,
        "deRegisteredDateBs": None,
        "registeredOffice": "Office of Company Register, Tripureshwor",
        "provinceNameEnglish": "Bagmati Province",
        "provinceNameNepali": "बागमती प्रदेश",
        "districtNameEnglish": "Kathmandu",
        "districtNameNepali": "काठमाडौं",
        "annualReportUpdatedUpto": "81/82",
        "natureOfBusiness": [
            {
                "nsicCode": "4540",
                "nameEnglish": "Sale of motor vehicles",
                "nameNepali": "बिक्री",
            },
        ],
    }
    rec.update(over)
    return rec


# --- status filter ----------------------------------------------------------


def test_draft_is_not_published():
    assert ocr_company_to_jsonld(_company(status="DRAFT")) is None


def test_rejected_is_not_published():
    assert ocr_company_to_jsonld(_company(status="REJECTED")) is None


def test_blank_status_is_not_published():
    assert ocr_company_to_jsonld(_company(status=None)) is None


def test_approved_is_published():
    assert ocr_company_to_jsonld(_company()) is not None


def test_deregistered_is_published():
    assert ocr_company_to_jsonld(_company(status="DEREGISTERED")) is not None


# --- identity / @type -------------------------------------------------------


def test_id_and_type_for_private():
    payload = ocr_company_to_jsonld(_company())
    assert payload["prefix"] == COMPANY_PREFIX
    assert payload["slug"] == "sipradi-yatri-350003"
    assert payload["type"] == "Corporation"


def test_foreign_is_corporation():
    payload = ocr_company_to_jsonld(
        _company(companyTypeCategory={"baseValue": "FOREIGN"})
    )
    assert payload["type"] == "Corporation"


def test_nonprofit_is_organization_with_additional_type():
    payload = ocr_company_to_jsonld(
        _company(companyTypeCategory={"baseValue": "NONPROFIT"})
    )
    assert payload["type"] == "Organization"
    assert payload["additionalType"] == "jawafdehi:NonProfitCompany"


def test_unknown_base_falls_back_to_organization():
    payload = ocr_company_to_jsonld(_company(companyTypeCategory={"baseValue": "WHAT"}))
    assert payload["type"] == "Organization"


# --- names / slug -----------------------------------------------------------


def test_name_is_bilingual_language_map():
    payload = ocr_company_to_jsonld(_company())
    assert payload["name"] == {"en": "Sipradi Yatri", "ne": "सिप्रदी यात्री"}


def test_slug_falls_back_to_regno_when_name_unromanizable():
    # No English name + a Devanagari-only name; whether or not the transliteration
    # backend is present, the slug must be valid and carry the registration number.
    payload = ocr_company_to_jsonld(
        _company(companyNameEnglish="", companyNameNepali="काठमाडौं")
    )
    assert payload["slug"].endswith("-350003")
    assert payload is not None


def test_slug_is_stable_for_same_record():
    rec = _company()
    assert company_slug(rec) == company_slug(dict(rec))


# --- dates ------------------------------------------------------------------


def test_founding_date_pair():
    payload = ocr_company_to_jsonld(_company())
    # BS 2081-03-30 converts to the AD registration date; BS carried verbatim.
    assert payload["foundingDate"] == "2024-07-14"
    assert payload["jawafdehi:foundingDateBS"] == "2081-03-30"


def test_dissolution_date_for_deregistered():
    payload = ocr_company_to_jsonld(
        _company(
            status="DEREGISTERED",
            deRegisteredDateAD="2026-05-12",
            deRegisteredDateBs="2083-01-29",
        )
    )
    assert payload["dissolutionDate"] == "2026-05-12"
    assert payload["jawafdehi:deregisteredDateBS"] == "2083-01-29"


def test_no_dissolution_date_for_approved():
    payload = ocr_company_to_jsonld(_company())
    assert "dissolutionDate" not in payload


# --- identifiers / provenance / business ------------------------------------


def test_identifiers_carry_ocr_regno_pan_and_company_id():
    payload = ocr_company_to_jsonld(_company())
    by_prop = {i["propertyID"]: i["value"] for i in payload["identifier"]}
    assert by_prop["ocr"] == "350003"
    assert by_prop["ocr-company-id"] == "1"
    assert by_prop["pan"] == "621xxx032"  # masked verbatim; no PII un-masking.


def test_source_is_single_ocr_authority():
    payload = ocr_company_to_jsonld(_company())
    assert payload["sources"] == [
        {"url": "https://company.ocr.gov.np/company/1", "authority": OCR_AUTHORITY}
    ]


def test_address_is_postal_address_with_region_and_locality():
    payload = ocr_company_to_jsonld(_company())
    addr = payload["address"]
    assert addr["@type"] == "PostalAddress"
    assert addr["addressRegion"] == {"en": "Bagmati Province", "ne": "बागमती प्रदेश"}
    assert addr["addressLocality"] == {"en": "Kathmandu", "ne": "काठमाडौं"}
    assert addr["addressCountry"] == "NP"


def test_nature_of_business_carried_with_nsic():
    payload = ocr_company_to_jsonld(_company())
    nob = payload["jawafdehi:natureOfBusiness"]
    assert nob[0]["nsicCode"] == "4540"
    assert nob[0]["name"]["en"] == "Sale of motor vehicles"


# --- the contract that matters: output validates on the real create path -----


def test_payload_passes_validate_create_payload():
    payload = ocr_company_to_jsonld(_company())
    doc = validate_create_payload(dict(payload))  # copy — validation normalizes
    assert (
        doc["@id"]
        == "https://jawafdehi.org/entity/organization/company/sipradi-yatri-350003"
    )
    assert doc["@type"] == "Corporation"


def test_nonprofit_payload_validates():
    payload = ocr_company_to_jsonld(
        _company(companyTypeCategory={"baseValue": "NONPROFIT"})
    )
    doc = validate_create_payload(dict(payload))
    assert doc["@type"] == "Organization"
    assert doc["additionalType"] == "jawafdehi:NonProfitCompany"


def test_minimal_record_still_validates():
    # Only the fields needed for identity + a name; everything optional absent.
    minimal = {
        "companyId": 99,
        "companyNameEnglish": "Acme Pvt Ltd",
        "registrationNumber": "12345",
        "status": "APPROVED",
        "companyTypeCategory": {"baseValue": "PRIVATE"},
    }
    payload = ocr_company_to_jsonld(minimal)
    doc = validate_create_payload(dict(payload))
    assert doc["@id"].endswith("/company/acme-pvt-ltd-12345")
