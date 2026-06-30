"""build_doc shape tests for the four per-app unified-search indexers.

Pure-Python: build_doc operates on plain objects with attributes, so these tests
need NO database and NO live OpenSearch. They assert the common-doc field shape
per type, the bilingual title extraction, and the case-only-published rule.
"""

from __future__ import annotations

from types import SimpleNamespace

from cases import search_index as case_index
from nes_service.entities import search_index as entity_index
from ngm_service.courts import search_index as courtcase_index
from ngm_service.materials import search_index as material_index

COMMON_FIELDS = {
    "iri",
    "type",
    "source_app",
    "title_ne",
    "title_en",
    "title_translit",
    "body",
    "keywords",
    "identifiers",
    "raw",
}


# ── entity ───────────────────────────────────────────────────────────────────


def test_entity_build_doc_shape():
    iri = "https://jawafdehi.org/entity/person/sher-bahadur-deuba"
    obj = SimpleNamespace(
        iri=iri,
        data={
            "@id": iri,
            "@type": "Person",
            "name": {"ne": "शेर बहादुर देउवा", "en": "Sher Bahadur Deuba"},
            "keywords": ["politician", "prime-minister"],
            "description": {"en": "Former PM"},
            "identifier": "NP-001",
        },
    )
    doc = entity_index.build_doc(obj)
    assert COMMON_FIELDS <= set(doc)
    assert doc["iri"] == iri
    assert doc["type"] == "Person"
    assert doc["source_app"] == "nes"
    assert doc["title_ne"] == "शेर बहादुर देउवा"
    assert doc["title_en"] == "Sher Bahadur Deuba"
    assert doc["keywords"] == ["politician", "prime-minister"]
    assert iri in doc["identifiers"]
    assert "NP-001" in doc["identifiers"]
    assert "Former PM" in doc["body"]
    # title_translit carries a romanization of the Devanagari title.
    assert doc["title_translit"]


def test_entity_list_type_joined():
    iri = "https://jawafdehi.org/entity/location/kathmandu"
    obj = SimpleNamespace(
        iri=iri,
        data={"@id": iri, "@type": ["Place", "AdministrativeArea"], "name": "Kathmandu"},
    )
    doc = entity_index.build_doc(obj)
    assert doc["type"] == "Place,AdministrativeArea"
    # Plain Latin string name → title_en, not title_ne.
    assert doc["title_en"] == "Kathmandu"
    assert doc["title_ne"] is None


# ── material ───────────────────────────────────────────────────────────────────


def test_material_build_doc_shape_with_dates():
    iri = "https://jawafdehi.org/material/court/supreme.081-cr-0081"
    obj = SimpleNamespace(
        iri=iri,
        ident="supreme.081-cr-0081",
        source="court",
        data={
            "@id": iri,
            "@type": ["Manuscript", "DigitalDocument"],
            "name": {"ne": "आदेश"},
            "text": {"ne": "पूरा पाठ यहाँ"},
            "keywords": ["court-order"],
            "datePublished": "2024-01-15",
            "jawafdehi:registrationDateBS": "2080-10-01",
            "identifier": "081-CR-0081",
        },
    )
    doc = material_index.build_doc(obj)
    assert COMMON_FIELDS <= set(doc)
    assert doc["iri"] == iri
    assert doc["type"] == "Manuscript,DigitalDocument"
    assert doc["source_app"] == "ngm"
    assert doc["title_ne"] == "आदेश"
    assert "पूरा पाठ यहाँ" in doc["body"]
    assert doc["date"] == "2024-01-15"
    # Bikram Sambat carried verbatim (never coerced to a date).
    assert doc["date_bs"] == "2080-10-01"
    assert "081-CR-0081" in doc["identifiers"]


# ── courtcase ──────────────────────────────────────────────────────────────────


def _courtcase_obj():
    # A bare CourtCase-like object: ``.iri`` uses build_courtcase_iri from the
    # composite key; party lookups degrade gracefully with no DB.
    from jawafdehi_shared.entities.ids import build_courtcase_iri

    obj = SimpleNamespace(
        court_id="supreme",
        case_number="081-CR-0081",
        case_type="corruption",
        case_status="decided",
        plaintiff="नेपाल सरकार",
        defendant="राम बहादुर",
        nes_id="https://jawafdehi.org/entity/person/ram-bahadur",
        registration_date_ad=None,
        registration_date_bs="2080-10-01",
        court=SimpleNamespace(full_name_english="Supreme Court"),
    )
    obj.iri = build_courtcase_iri(obj.court_id, obj.case_number)
    return obj


def test_courtcase_build_doc_shape_and_title_from_case_number():
    obj = _courtcase_obj()
    doc = courtcase_index.build_doc(obj)
    assert COMMON_FIELDS <= set(doc)
    assert doc["source_app"] == "ngm"
    assert doc["type"] == "jawafdehi:CourtCase"
    assert doc["iri"].endswith("/courtcase/supreme/081-cr-0081")
    # CONTRACT GAP: court cases have no language-map name. title_ne is the
    # case_number; title_en is the English court name + number.
    assert doc["title_ne"] == "081-CR-0081"
    assert doc["title_en"] == "Supreme Court 081-CR-0081"
    # Party names flow into body + keywords so a party-name query matches.
    assert "नेपाल सरकार" in doc["body"]
    assert "राम बहादुर" in doc["keywords"]
    # case_number, court, and the case-level nes_id ride in identifiers.
    assert "081-CR-0081" in doc["identifiers"]
    assert "supreme" in doc["identifiers"]
    assert "https://jawafdehi.org/entity/person/ram-bahadur" in doc["identifiers"]
    assert doc["date_bs"] == "2080-10-01"
    # case_type promoted to a top-level keyword for filtering/faceting.
    assert doc["case_type"] == "corruption"


def test_courtcase_title_en_none_without_english_court_name():
    obj = _courtcase_obj()
    obj.court = SimpleNamespace(full_name_english=None)
    doc = courtcase_index.build_doc(obj)
    assert doc["title_en"] is None
    assert doc["title_ne"] == "081-CR-0081"


# ── case ───────────────────────────────────────────────────────────────────────


def _published_case():
    return SimpleNamespace(
        state="PUBLISHED",
        public_iri="https://jawafdehi.org/case/budget-scam-2080",
        slug="budget-scam-2080",
        title="Budget allocation scam",
        description="A detailed markdown description.",
        short_description="Short summary.",
        key_allegations=["Misappropriation of funds", "Forgery"],
        tags=["corruption", "budget"],
        case_type="CORRUPTION",
        court_cases=["supreme:081-CR-0081"],
        case_start_date=None,
        created_at=None,
        updated_at=None,
    )


def test_case_build_doc_shape_published():
    case = _published_case()
    doc = case_index.build_doc(case)
    assert COMMON_FIELDS <= set(doc)
    assert doc["iri"] == "https://jawafdehi.org/case/budget-scam-2080"
    assert doc["type"] == "Case"
    assert doc["source_app"] == "jawafdehi"
    assert doc["title_en"] == "Budget allocation scam"
    # description + key_allegations fold into body.
    assert "detailed markdown" in doc["body"]
    assert "Misappropriation of funds" in doc["body"]
    # tags + case_type into keywords.
    assert "corruption" in doc["keywords"]
    assert "CORRUPTION" in doc["keywords"]
    # case_type also promoted to a top-level keyword for filtering/faceting.
    assert doc["case_type"] == "CORRUPTION"
    # slug + court_cases refs into identifiers.
    assert "budget-scam-2080" in doc["identifiers"]
    assert "supreme:081-CR-0081" in doc["identifiers"]


def test_case_should_index_only_published():
    assert case_index.should_index(_published_case()) is True
    for state in ("DRAFT", "IN_REVIEW", "CLOSED"):
        case = _published_case()
        case.state = state
        assert case_index.should_index(case) is False
