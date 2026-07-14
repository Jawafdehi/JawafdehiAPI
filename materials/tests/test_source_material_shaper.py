"""Tests for the Jawafdehi case-source → Material projection (ADR: cases own no
documents). Pure functions — no DB — mirroring the court_case_to_jsonld tests.

Covers:
- build_source_material_iri: source_id (with colons) → canonical /material/jawafdehi/<ident>,
- material_type_for_source_type mapping (governance/news/social/misc),
- documentsource_to_jsonld shape: @id/@type/name/associatedMedia/about/datePublished,
- the produced doc validates under validate_material_jsonld + Material.from_jsonld.
"""

from __future__ import annotations

from datetime import date

import pytest

from jawafdehi_shared.entities.ids import (
    build_source_material_iri,
    is_valid_material_iri,
)
from materials.jsonld import (
    MaterialType,
    documentsource_to_jsonld,
    material_type_for_source_type,
    validate_material_jsonld,
)


class TestBuildSourceMaterialIri:
    def test_normalizes_legacy_source_id_colons(self):
        iri = build_source_material_iri("source:20240115:ab12cd")
        assert iri == "https://jawafdehi.org/material/jawafdehi/20240115.ab12cd"
        assert is_valid_material_iri(iri)

    def test_idempotent_on_already_normalized_ident(self):
        # A bare id without the source: prefix still yields a valid IRI.
        iri = build_source_material_iri("20240115.ab12cd")
        assert iri == "https://jawafdehi.org/material/jawafdehi/20240115.ab12cd"
        assert is_valid_material_iri(iri)

    def test_lowercases_ident(self):
        iri = build_source_material_iri("source:20240115:AB12CD")
        assert iri.endswith("/20240115.ab12cd")


class TestMaterialTypeMapping:
    @pytest.mark.parametrize(
        "source_type,expected",
        [
            ("CIAA_PRESS_RELEASE", MaterialType.PRESS_RELEASE),
            ("AG_ABHIYOG_PATRA", MaterialType.CHARGE_SHEET),
            ("OAG_AUDIT_REPORT", MaterialType.OFFICIAL_REPORT),
            ("COURT_ORDER", MaterialType.COURT_ORDER),
            ("LAW_OR_BILL", MaterialType.LEGAL_CORPUS),
            ("NEWS", MaterialType.NEWS),
            ("SOCIAL_MEDIA", MaterialType.SOCIAL_MEDIA),
            ("COURT_FILING_OTHER", MaterialType.DOCUMENT),
            ("MISC", MaterialType.DOCUMENT),
            (None, MaterialType.DOCUMENT),
            ("SOMETHING_UNKNOWN", MaterialType.DOCUMENT),
        ],
    )
    def test_mapping(self, source_type, expected):
        assert material_type_for_source_type(source_type) == expected


class TestDocumentSourceToJsonld:
    def test_full_shape(self):
        doc, material_type = documentsource_to_jsonld(
            source_id="source:20240115:ab12cd",
            title="CIAA press release on X",
            source_type="CIAA_PRESS_RELEASE",
            url=[
                {"link": "https://ciaa.gov.np/pr/1.pdf", "role": "RAW"},
                {"link": "https://ciaa.gov.np/pr/1", "role": "SOURCE_PAGE"},
            ],
            description="A press release summary.",
            related_entities=["https://jawafdehi.org/entity/person/ram-bahadur"],
            publication_date=date(2024, 1, 15),
        )
        # A CIAA press release is the announcement, NOT the indictment: it must
        # shape to press_release, distinct from a charge sheet (अभियोगपत्र).
        assert material_type == MaterialType.PRESS_RELEASE
        assert doc["@id"] == "https://jawafdehi.org/material/jawafdehi/20240115.ab12cd"
        assert doc["@type"] == "CreativeWork"
        assert doc["additionalType"] == "jawafdehi:PressRelease"
        assert doc["name"] == {"ne": "CIAA press release on X"}
        assert doc["description"] == {"ne": "A press release summary."}
        assert doc["jawafdehi:sourceType"] == "CIAA_PRESS_RELEASE"
        assert doc["datePublished"] == "2024-01-15"
        # roled links → associatedMedia MediaObjects, order + roles preserved
        media = doc["associatedMedia"]
        assert [m["jawafdehi:linkRole"] for m in media] == ["RAW", "SOURCE_PAGE"]
        assert media[0]["contentUrl"] == "https://ciaa.gov.np/pr/1.pdf"
        # SOURCE_PAGE carries an html encoding hint
        assert media[1]["encodingFormat"] == "text/html"
        # related entity → about
        assert doc["about"] == [
            {"@id": "https://jawafdehi.org/entity/person/ram-bahadur"}
        ]

    def test_charge_sheet_shape_is_distinct_from_press_release(self):
        # An AG अभियोगपत्र (the indictment) stays charge_sheet: DigitalDocument +
        # jawafdehi:ChargeSheet — NOT the press_release type/additionalType.
        doc, material_type = documentsource_to_jsonld(
            source_id="source:20240115:cs0001",
            title="AG Charge Sheet - 081-CR-0095",
            source_type="AG_ABHIYOG_PATRA",
            url=None,
        )
        assert material_type == MaterialType.CHARGE_SHEET
        assert doc["@type"] == "DigitalDocument"
        assert doc["additionalType"] == "jawafdehi:ChargeSheet"

    def test_minimal_shape_no_optional_fields(self):
        doc, material_type = documentsource_to_jsonld(
            source_id="source:20240201:ff00ff",
            title="A news article",
            source_type="NEWS",
            url=None,
        )
        assert material_type == MaterialType.NEWS
        assert doc["@type"] == "NewsArticle"
        assert "additionalType" not in doc
        assert "associatedMedia" not in doc
        assert "about" not in doc
        assert "datePublished" not in doc
        assert "description" not in doc

    def test_name_falls_back_to_source_id_when_title_blank(self):
        doc, _ = documentsource_to_jsonld(
            source_id="source:20240201:ff00ff",
            title="   ",
            source_type="MISC",
            url=None,
        )
        assert doc["name"] == {"ne": "source:20240201:ff00ff"}

    def test_produced_doc_validates(self):
        doc, _ = documentsource_to_jsonld(
            source_id="source:20240115:ab12cd",
            title="CIAA press release",
            source_type="CIAA_PRESS_RELEASE",
            url=[{"link": "https://ciaa.gov.np/pr/1.pdf", "role": "RAW"}],
        )
        # Must not raise (known @type, valid @id, name present).
        validate_material_jsonld(doc, iri=doc["@id"])

    def test_produced_doc_builds_a_material(self):
        doc, material_type = documentsource_to_jsonld(
            source_id="source:20240115:ab12cd",
            title="CIAA press release",
            source_type="NEWS",
            url=None,
        )
        from materials.models import Material

        mat = Material.from_jsonld(doc, material_type=material_type)
        assert mat.iri == doc["@id"]
        assert mat.source == "jawafdehi"
        assert mat.ident == "20240115.ab12cd"
        assert mat.material_type == MaterialType.NEWS
        mat.clean()  # promoted-column agreement + jsonld validation
