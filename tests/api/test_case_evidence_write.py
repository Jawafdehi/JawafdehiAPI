"""Tests for the case-evidence WRITE paths (ADR: cases own no documents).

Evidence is the CaseMaterialReference join (material_iri + additional_details).
Covers create (POST), PATCH (add/replace/remove /evidence), soft-delete, and the
material-visibility recompute that fires on evidence/state/delete transitions.
"""

import pytest
from rest_framework.test import APIClient

from cases.models import (
    Case,
    CaseEntityRelationship,
    CaseMaterialReference,
    CaseState,
    CaseType,
    RelationshipType,
)
from materials.jsonld import documentsource_to_jsonld
from materials.models import Material, Visibility
from tests.conftest import create_user_with_role

URL = "/api/cases/{}/"
LIST_URL = "/api/cases/"

IRI_A = "https://jawafdehi.org/material/jawafdehi/20240101.aaaa01"
IRI_B = "https://jawafdehi.org/material/jawafdehi/20240101.bbbb02"


def _contributor(name="rishi"):
    return create_user_with_role(name, f"{name}@example.com", "Caseworker")


def _authed(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def _make_case(**kwargs):
    defaults = dict(
        title="Test case",
        case_type=CaseType.CORRUPTION,
        state=CaseState.DRAFT,
        description="Some description",
        short_description="Short",
    )
    defaults.update(kwargs)
    return Case.objects.create(**defaults)


def _store_material(source_id, visibility=Visibility.LISTED):
    doc, mtype = documentsource_to_jsonld(
        source_id=source_id, title="Doc", source_type="MISC", url=None
    )
    mat = Material.from_jsonld(doc, material_type=mtype)
    mat.visibility = visibility
    mat.save()
    return mat


@pytest.mark.django_db
class TestCreateWritesEvidence:
    def test_create_case_with_evidence(self):
        user = _contributor("creator")
        client = _authed(user)
        resp = client.post(
            LIST_URL,
            data={
                "case_type": CaseType.CORRUPTION,
                "title": "Bribery at the ministry",
                "evidence": [
                    {"material_iri": IRI_A, "additional_details": "the charge sheet"},
                    {"material_iri": IRI_B},
                ],
            },
            format="json",
        )
        assert resp.status_code == 201, resp.data
        case = Case.objects.get(slug=resp.data["slug"])
        refs = list(case.material_references.all())
        assert [r.material_iri for r in refs] == [IRI_A, IRI_B]
        assert refs[0].additional_details == "the charge sheet"
        assert refs[1].additional_details == ""
        assert [r.ordinal for r in refs] == [0, 1]

    def test_create_rejects_bad_material_iri(self):
        user = _contributor("creator2")
        client = _authed(user)
        resp = client.post(
            LIST_URL,
            data={
                "case_type": CaseType.CORRUPTION,
                "title": "Bad evidence",
                "evidence": [{"material_iri": "not-an-iri"}],
            },
            format="json",
        )
        assert resp.status_code == 422


@pytest.mark.django_db
class TestPatchWritesEvidence:
    def test_patch_add_evidence(self):
        user = _contributor("patcher")
        case = _make_case()
        client = _authed(user)
        resp = client.patch(
            URL.format(case.slug),
            data=[
                {
                    "op": "add",
                    "path": "/evidence/-",
                    "value": {"material_iri": IRI_A, "additional_details": "x"},
                }
            ],
            format="json",
        )
        assert resp.status_code == 200, resp.data
        assert case.material_references.filter(material_iri=IRI_A).exists()

    def test_patch_replace_evidence_set(self):
        user = _contributor("patcher2")
        case = _make_case()
        CaseMaterialReference.objects.create(case=case, material_iri=IRI_A, ordinal=0)
        client = _authed(user)
        # Replace the whole evidence list with a single different material.
        resp = client.patch(
            URL.format(case.slug),
            data=[
                {
                    "op": "replace",
                    "path": "/evidence",
                    "value": [{"material_iri": IRI_B}],
                }
            ],
            format="json",
        )
        assert resp.status_code == 200, resp.data
        iris = set(case.material_references.values_list("material_iri", flat=True))
        assert iris == {IRI_B}

    def test_patch_remove_all_evidence(self):
        user = _contributor("patcher3")
        case = _make_case()
        CaseMaterialReference.objects.create(case=case, material_iri=IRI_A, ordinal=0)
        client = _authed(user)
        resp = client.patch(
            URL.format(case.slug),
            data=[{"op": "replace", "path": "/evidence", "value": []}],
            format="json",
        )
        assert resp.status_code == 200
        assert case.material_references.count() == 0

    def test_scalar_patch_does_not_wipe_evidence(self):
        user = _contributor("patcher4")
        case = _make_case()
        CaseMaterialReference.objects.create(case=case, material_iri=IRI_A, ordinal=0)
        client = _authed(user)
        resp = client.patch(
            URL.format(case.slug),
            data=[{"op": "replace", "path": "/title", "value": "New title"}],
            format="json",
        )
        assert resp.status_code == 200
        # Evidence untouched because no /evidence op was present.
        assert case.material_references.filter(material_iri=IRI_A).exists()


@pytest.mark.django_db
class TestEvidenceVisibilityTriggers:
    # The recompute is scheduled on transaction.on_commit, so these tests wrap the
    # request in django_capture_on_commit_callbacks(execute=True) to run it.

    def test_publish_promotes_referenced_material(
        self, django_capture_on_commit_callbacks
    ):
        # A material referenced only by a case being submitted to review should
        # flip PRIVATE -> UNLISTED via the recompute trigger.
        user = create_user_with_role("mod", "mod@example.com", "Moderator")
        mat = _store_material("source:20240101:aaaa01", visibility=Visibility.PRIVATE)
        # IN_REVIEW requires an accused entity + a key allegation.
        case = _make_case(key_allegations=["Took a bribe of Rs 10 lakh"])
        CaseEntityRelationship.objects.create(
            case=case,
            nes_id="https://jawafdehi.org/entity/person/accused-one",
            relationship_type=RelationshipType.ACCUSED,
        )
        CaseMaterialReference.objects.create(case=case, material_iri=mat.iri, ordinal=0)
        client = _authed(user)
        with django_capture_on_commit_callbacks(execute=True):
            resp = client.patch(
                URL.format(case.slug),
                data=[
                    {"op": "replace", "path": "/state", "value": CaseState.IN_REVIEW}
                ],
                format="json",
            )
        assert resp.status_code == 200, resp.data
        mat.refresh_from_db()
        assert mat.visibility == Visibility.UNLISTED

    def test_delete_case_demotes_referenced_material(
        self, django_capture_on_commit_callbacks
    ):
        user = create_user_with_role("mod2", "mod2@example.com", "Moderator")
        mat = _store_material("source:20240101:aaaa01", visibility=Visibility.LISTED)
        case = _make_case(state=CaseState.DRAFT)
        CaseMaterialReference.objects.create(case=case, material_iri=mat.iri, ordinal=0)
        client = _authed(user)
        with django_capture_on_commit_callbacks(execute=True):
            resp = client.delete(URL.format(case.slug))
        assert resp.status_code == 204
        mat.refresh_from_db()
        # CLOSED (soft-deleted) case confers PRIVATE.
        assert mat.visibility == Visibility.PRIVATE

    def test_removing_evidence_demotes_dropped_material(
        self, django_capture_on_commit_callbacks
    ):
        user = create_user_with_role("mod3", "mod3@example.com", "Moderator")
        mat = _store_material("source:20240101:aaaa01", visibility=Visibility.LISTED)
        case = _make_case(state=CaseState.DRAFT)
        CaseMaterialReference.objects.create(case=case, material_iri=mat.iri, ordinal=0)
        client = _authed(user)
        # Drop the material from evidence — it now has no referrer → PRIVATE.
        with django_capture_on_commit_callbacks(execute=True):
            resp = client.patch(
                URL.format(case.slug),
                data=[{"op": "replace", "path": "/evidence", "value": []}],
                format="json",
            )
        assert resp.status_code == 200
        mat.refresh_from_db()
        assert mat.visibility == Visibility.PRIVATE
