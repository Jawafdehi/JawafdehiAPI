"""Tests for the source→Material ingest helper (ADR: cases own no documents).

Covers cases.services.material_ingest (upsert_source_material / bind /
ingest_source_as_evidence) and the rewired CIAA draft-case ingest.
"""

import pytest

from cases.models import Case, CaseMaterialReference, CaseState, CaseType
from cases.services.material_ingest import (
    bind_material_to_case,
    ingest_source_as_evidence,
    upsert_source_material,
)
from materials.models import Material, Visibility


def _case(slug="ingest-case"):
    return Case.objects.create(
        case_type=CaseType.CORRUPTION,
        state=CaseState.DRAFT,
        title="Ingest case",
        slug=slug,
    )


@pytest.mark.django_db
class TestUpsertSourceMaterial:
    def test_creates_material_from_source(self):
        iri = upsert_source_material(
            title="CIAA press release",
            url="https://ciaa.gov.np/pr/1.pdf",
            source_type="CIAA_PRESS_RELEASE",
            source_id="source:20240115:ab12cd",
        )
        assert iri == "https://jawafdehi.org/material/jawafdehi/20240115.ab12cd"
        mat = Material.objects.get(iri=iri)
        assert mat.material_type == "charge_sheet"
        assert mat.data["associatedMedia"][0]["contentUrl"] == (
            "https://ciaa.gov.np/pr/1.pdf"
        )

    def test_titleless_source_returns_none(self):
        assert upsert_source_material(title="   ", url="x") is None

    def test_idempotent_by_id(self):
        kwargs = dict(title="Doc", source_id="source:20240115:ff00ff")
        a = upsert_source_material(**kwargs)
        b = upsert_source_material(**kwargs)
        assert a == b
        assert Material.objects.filter(iri=a).count() == 1

    def test_derives_stable_id_without_source_id(self):
        # No source_id → deterministic id from the link, so re-ingest is idempotent.
        a = upsert_source_material(title="News", url="https://example.com/a")
        b = upsert_source_material(title="News", url="https://example.com/a")
        assert a == b
        assert Material.objects.filter(iri=a).count() == 1


@pytest.mark.django_db
class TestBindAndIngest:
    def test_bind_is_idempotent(self):
        case = _case()
        iri = "https://jawafdehi.org/material/jawafdehi/20240115.ab12cd"
        bind_material_to_case(case, iri, additional_details="first")
        bind_material_to_case(case, iri, additional_details="second")
        refs = case.material_references.all()
        assert refs.count() == 1
        assert refs[0].additional_details == "second"

    def test_ingest_source_as_evidence_end_to_end(self):
        case = _case()
        iri = ingest_source_as_evidence(
            case,
            title="AG charge sheet",
            url="https://ag.gov.np/cs/9.pdf",
            source_type="AG_ABHIYOG_PATRA",
            source_id="source:20240201:aabbcc",
            additional_details="the abhiyog patra",
        )
        assert Material.objects.filter(iri=iri).exists()
        ref = CaseMaterialReference.objects.get(case=case, material_iri=iri)
        assert ref.additional_details == "the abhiyog patra"

    def test_ingest_titleless_binds_nothing(self):
        case = _case()
        assert ingest_source_as_evidence(case, title="") is None
        assert case.material_references.count() == 0

    def test_ingest_into_draft_case_does_not_leak_public(self):
        # Regression: a Material is born LISTED; ingest must recompute from the
        # DRAFT case's state so the evidence is not publicly searchable/crawlable.
        case = _case("draft-leak", )
        assert case.state == CaseState.DRAFT
        iri = ingest_source_as_evidence(
            case, title="Secret charge sheet", source_id="source:20240301:secret1"
        )
        mat = Material.objects.get(iri=iri)
        assert mat.visibility == Visibility.PRIVATE

    def test_ingest_into_published_case_is_listed(self):
        case = _case("pub-ok")
        case.state = CaseState.PUBLISHED
        case.save()
        iri = ingest_source_as_evidence(
            case, title="Public charge sheet", source_id="source:20240301:public1"
        )
        mat = Material.objects.get(iri=iri)
        assert mat.visibility == Visibility.LISTED


@pytest.mark.django_db
class TestCIAADraftServiceIngest:
    def test_create_material_evidence_binds_all_kinds(self):
        from cases.services.ciaa_draft_case_service import CIAADraftCaseService

        case = _case("ciaa-ingest")
        ciaa_json = {
            "case_no": "078-CR-0123",
            "ciaa": {
                "press_releases": [
                    {"title": "PR one", "url": "https://ciaa.gov.np/pr/1", "release_id": "R1"}
                ],
                "abhiyogPatras": [
                    {"title": "Charge sheet", "pdf_url": "https://ag.gov.np/cs 1.pdf", "case_number": "CN1"}
                ],
            },
            "court_case": {"faisala_link": ["https://supremecourt.gov.np/order/1"]},
        }
        iris = CIAADraftCaseService().create_material_evidence(ciaa_json, case)
        assert len(iris) == 3
        assert case.material_references.count() == 3
        # material types cover charge_sheet (PR + AG) and court_order
        types = set(
            Material.objects.filter(iri__in=iris).values_list("material_type", flat=True)
        )
        assert "charge_sheet" in types
        assert "court_order" in types
