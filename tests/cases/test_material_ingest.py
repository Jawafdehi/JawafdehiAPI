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
from materials.models import Material, Policy, Visibility


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
        # Sourced by material_type (a CIAA press release → press_release), NOT the
        # legacy monolithic jawafdehi bucket.
        assert iri == "https://jawafdehi.org/material/press_release/20240115.ab12cd"
        mat = Material.objects.get(iri=iri)
        assert mat.material_type == "press_release"
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

    def test_rebind_preserves_ordinal_unless_explicit(self):
        # Re-binding an existing material without an explicit ordinal must NOT
        # reshuffle it to the current count (Gemini finding). Two refs at 0,1;
        # re-binding the first (no ordinal) keeps its ordinal 0, not 2.
        case = _case()
        first = "https://jawafdehi.org/material/jawafdehi/20240115.aaaa01"
        second = "https://jawafdehi.org/material/jawafdehi/20240115.bbbb02"
        bind_material_to_case(case, first)   # ordinal 0
        bind_material_to_case(case, second)  # ordinal 1
        bind_material_to_case(case, first, additional_details="updated")
        ref = case.material_references.get(material_iri=first)
        assert ref.ordinal == 0
        assert ref.additional_details == "updated"
        # explicit ordinal still applies
        bind_material_to_case(case, first, ordinal=5)
        ref.refresh_from_db()
        assert ref.ordinal == 5

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

    def test_ingest_into_draft_case_is_public_by_default(self):
        # Case uploads are sourced by material_type (→ non-jawafdehi) and born
        # PUBLIC, so an upload is publicly LISTED on ingest regardless of the
        # binding case's state — a caseworker embargoes a sensitive one explicitly
        # (see test_ingest_honors_prior_embargo).
        case = _case("draft-pub")
        assert case.state == CaseState.DRAFT
        iri = ingest_source_as_evidence(
            case, title="Press release", source_id="source:20240301:public2"
        )
        mat = Material.objects.get(iri=iri)
        assert mat.visibility == Visibility.LISTED

    def test_ingest_honors_prior_embargo(self):
        # The ingest-path recompute HONORS a caseworker embargo: once an @id is set
        # CASE_GATED, a later ingest pass maps that policy against the DRAFT case's
        # state → PRIVATE, rather than the born-PUBLIC default.
        case = _case("draft-embargo")
        iri = ingest_source_as_evidence(
            case, title="Sensitive doc", source_id="source:20240301:embargo1"
        )
        Material.objects.filter(pk=iri).update(visibility_policy=Policy.CASE_GATED)
        # Re-ingest the same @id; the upsert preserves the manual policy (INSERT-only
        # birth default) and the ingest then recomputes from the DRAFT case.
        ingest_source_as_evidence(
            case, title="Sensitive doc", source_id="source:20240301:embargo1"
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
        # material types: press_release (PR), charge_sheet (AG abhiyogpatra), court_order
        types = set(
            Material.objects.filter(iri__in=iris).values_list("material_type", flat=True)
        )
        assert "press_release" in types
        assert "charge_sheet" in types
        assert "court_order" in types
