"""Tests for the CaseMaterialReference evidence join + resolve_materials seam
(ADR: cases own no documents).

Covers:
- material_iri validation (required, strict canonical host),
- unique(case, material_iri), ordinal ordering,
- resolve_materials(): resolves stored Material display data, stubs the rest,
  excludes soft-deleted, projects associatedMedia → roled links.
"""

from __future__ import annotations

import pytest
from django.core.exceptions import ValidationError

from cases.models import Case, CaseMaterialReference, CaseState, CaseType
from cases.services.material_resolver import resolve_materials
from courts.models import Court, CourtCase
from materials.jsonld import court_case_material_iri, documentsource_to_jsonld
from materials.models import Material

VALID_IRI = "https://jawafdehi.org/material/jawafdehi/20240115.ab12cd"


@pytest.mark.django_db
class TestCaseMaterialReferenceModel:
    def _case(self, **overrides):
        defaults = {
            "case_type": CaseType.CORRUPTION,
            "state": CaseState.DRAFT,
            "title": "Test Case",
            "slug": "cmr-case-001",
            "court_cases": ["https://jawafdehi.org/courtcase/special/test-001"],
        }
        defaults.update(overrides)
        return Case.objects.create(**defaults)

    def test_create_valid_reference(self):
        case = self._case()
        ref = CaseMaterialReference.objects.create(
            case=case, material_iri=VALID_IRI, additional_details="Key charge sheet."
        )
        assert ref.material_iri == VALID_IRI
        assert ref.additional_details == "Key charge sheet."
        assert case.material_references.count() == 1

    def test_additional_details_optional(self):
        case = self._case()
        ref = CaseMaterialReference.objects.create(case=case, material_iri=VALID_IRI)
        assert ref.additional_details == ""

    @pytest.mark.parametrize(
        "bad_iri",
        [
            "",
            "not-an-iri",
            "https://jawafdehi.org/entity/person/ram",  # entity, not material
            "http://evil.com/material/court/sc.123",  # non-canonical host
        ],
    )
    def test_invalid_material_iri_rejected(self, bad_iri):
        case = self._case()
        with pytest.raises(ValidationError):
            CaseMaterialReference.objects.create(case=case, material_iri=bad_iri)

    def test_unique_case_material(self):
        case = self._case()
        CaseMaterialReference.objects.create(case=case, material_iri=VALID_IRI)
        with pytest.raises((ValidationError, Exception)):
            CaseMaterialReference.objects.create(case=case, material_iri=VALID_IRI)

    def test_ordinal_ordering(self):
        case = self._case()
        second = "https://jawafdehi.org/material/jawafdehi/20240115.ffffff"
        CaseMaterialReference.objects.create(case=case, material_iri=second, ordinal=2)
        CaseMaterialReference.objects.create(case=case, material_iri=VALID_IRI, ordinal=1)
        iris = list(case.material_references.values_list("material_iri", flat=True))
        assert iris == [VALID_IRI, second]


@pytest.mark.django_db
class TestResolveMaterials:
    def _store_material(self, source_id, title, source_type, url):
        doc, mtype = documentsource_to_jsonld(
            source_id=source_id, title=title, source_type=source_type, url=url
        )
        mat = Material.from_jsonld(doc, material_type=mtype)
        mat.save()
        return mat

    def test_resolves_stored_material(self):
        mat = self._store_material(
            "source:20240115:ab12cd",
            "CIAA press release",
            "CIAA_PRESS_RELEASE",
            [{"link": "https://ciaa.gov.np/pr/1.pdf", "role": "RAW"}],
        )
        resolved = resolve_materials([mat.iri])
        rec = resolved[mat.iri]
        assert rec["display_name"] == "CIAA press release"
        assert rec["material_type"] == "charge_sheet"
        assert rec["urls"] == [
            {"link": "https://ciaa.gov.np/pr/1.pdf", "role": "RAW"}
        ]

    def test_stubs_unknown_iri(self):
        resolved = resolve_materials([VALID_IRI])
        rec = resolved[VALID_IRI]
        assert rec["material_iri"] == VALID_IRI
        assert rec["display_name"] is None
        assert rec["material_type"] is None
        assert rec["urls"] == []

    def test_excludes_soft_deleted(self):
        mat = self._store_material(
            "source:20240115:ab12cd", "News", "NEWS", None
        )
        mat.is_deleted = True
        mat.save()
        resolved = resolve_materials([mat.iri])
        assert resolved[mat.iri]["display_name"] is None  # stub, not resolved

    def test_empty_and_falsy_ids(self):
        assert resolve_materials([]) == {}
        assert resolve_materials([None, ""]) == {}

    def test_total_over_mixed(self):
        mat = self._store_material(
            "source:20240115:ab12cd", "Known", "MISC", None
        )
        unknown = "https://jawafdehi.org/material/jawafdehi/19990101.deadbe"
        resolved = resolve_materials([mat.iri, unknown, mat.iri])
        assert set(resolved) == {mat.iri, unknown}
        assert resolved[mat.iri]["display_name"] == "Known"
        assert resolved[unknown]["display_name"] is None

    def test_derives_court_case_material_without_stored_row(self):
        """BB-20: a court-case material usually has no stored Material row — it
        is derived on the fly from the court tables. resolve_materials must
        derive its display name too, instead of leaving a stub that renders as a
        raw IRI/slug on the public case card.
        """
        court = Court.objects.create(
            identifier="kathmandudc",
            court_type="district",
            full_name_nepali="जिल्ला अदालत काठमाडौं",
            full_name_english="District Court Kathmandu",
        )
        case = CourtCase.objects.create(
            case_number="082-OA-0503",
            court=court,
            case_type="भ्रष्टाचार",
            case_status="चालु",
            plaintiff="X",
            defendant="Y",
            document_sources=[],
        )
        iri = court_case_material_iri(court.identifier, case.case_number)
        # There is deliberately no stored Material row for this IRI.
        assert not Material.objects.filter(iri=iri).exists()

        rec = resolve_materials([iri])[iri]
        assert rec["display_name"] == "082-OA-0503"
        # Uses the stable NGM material-type token, not the schema.org @type.
        assert rec["material_type"] == "court_case"
