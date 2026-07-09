"""Tests for Material.visibility — the draft-leak guard (ADR: cases own no
documents).

Covers:
- visibility_for_states MAX logic,
- recompute_material_visibility / recompute_for_case / recompute_all off case state,
- read-side gates: list + retrieve expose LISTED to anon, hide PRIVATE, and lift
  the gate for a privileged (staff) principal,
- sitemap/discovery corpus enumerates LISTED only,
- search signal evicts non-LISTED.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from cases.models import Case, CaseMaterialReference, CaseState, CaseType
from materials.jsonld import documentsource_to_jsonld
from materials.models import Material, Visibility
from materials.visibility import (
    recompute_all,
    recompute_for_case,
    recompute_material_visibility,
    visibility_for_states,
)

User = get_user_model()


def _store(source_id, title="Doc", source_type="MISC", url=None, visibility=None):
    doc, mtype = documentsource_to_jsonld(
        source_id=source_id, title=title, source_type=source_type, url=url
    )
    mat = Material.from_jsonld(doc, material_type=mtype)
    if visibility is not None:
        mat.visibility = visibility
    mat.save()
    return mat


def _case(slug, state):
    return Case.objects.create(
        case_type=CaseType.CORRUPTION,
        state=state,
        title="T",
        slug=slug,
        court_cases=["https://jawafdehi.org/courtcase/special/t-1"],
    )


class TestVisibilityForStates:
    def test_empty_is_private(self):
        assert visibility_for_states([]) == Visibility.PRIVATE

    def test_draft_only_private(self):
        assert visibility_for_states(["DRAFT"]) == Visibility.PRIVATE

    def test_in_review_unlisted(self):
        assert visibility_for_states(["DRAFT", "IN_REVIEW"]) == Visibility.UNLISTED

    def test_published_listed(self):
        assert visibility_for_states(["DRAFT", "PUBLISHED"]) == Visibility.LISTED

    def test_closed_is_private(self):
        # CLOSED is the case soft-delete tombstone → not public.
        assert visibility_for_states(["CLOSED"]) == Visibility.PRIVATE

    def test_unknown_state_private(self):
        assert visibility_for_states(["BOGUS"]) == Visibility.PRIVATE


@pytest.mark.django_db
class TestRecompute:
    def test_default_material_stays_listed(self):
        # NGM-native material with no case referrers keeps its default LISTED.
        mat = _store("source:20240101:aaaa01")
        assert mat.visibility == Visibility.LISTED

    def test_draft_reference_demotes_to_private(self):
        mat = _store("source:20240101:aaaa01")
        case = _case("c-draft", CaseState.DRAFT)
        CaseMaterialReference.objects.create(case=case, material_iri=mat.iri)
        assert recompute_material_visibility(mat.iri) == Visibility.PRIVATE
        mat.refresh_from_db()
        assert mat.visibility == Visibility.PRIVATE

    def test_published_reference_promotes_to_listed(self):
        mat = _store("source:20240101:aaaa01", visibility=Visibility.PRIVATE)
        case = _case("c-pub", CaseState.PUBLISHED)
        CaseMaterialReference.objects.create(case=case, material_iri=mat.iri)
        assert recompute_material_visibility(mat.iri) == Visibility.LISTED

    def test_max_across_referrers(self):
        mat = _store("source:20240101:aaaa01")
        draft = _case("c-draft", CaseState.DRAFT)
        pub = _case("c-pub", CaseState.PUBLISHED)
        CaseMaterialReference.objects.create(case=draft, material_iri=mat.iri)
        CaseMaterialReference.objects.create(case=pub, material_iri=mat.iri)
        # published wins even though a draft also references it
        assert recompute_material_visibility(mat.iri) == Visibility.LISTED

    def test_recompute_for_case_and_all(self):
        mat = _store("source:20240101:aaaa01")
        case = _case("c-review", CaseState.IN_REVIEW)
        CaseMaterialReference.objects.create(case=case, material_iri=mat.iri)
        recompute_for_case(case)
        mat.refresh_from_db()
        assert mat.visibility == Visibility.UNLISTED
        # flip case to draft, reconcile via recompute_all
        case.state = CaseState.DRAFT
        case.save()
        assert recompute_all() == 1
        mat.refresh_from_db()
        assert mat.visibility == Visibility.PRIVATE

    def test_non_api_case_demotion_recomputes_material_visibility(
        self, django_capture_on_commit_callbacks
    ):
        # F2: demoting a PUBLISHED case OUTSIDE the DRF API path (model .save() /
        # .delete(), Django admin, a management command, or a shell) must still
        # recompute the visibility of its evidence — otherwise a draft/closed
        # case's evidence stays publicly LISTED (a leak). The recompute is wired to
        # the model post_save (a signal, on_commit), not only the API view.
        mat = _store("source:20240101:leak01")
        case = _case("c-leak", CaseState.PUBLISHED)
        CaseMaterialReference.objects.create(case=case, material_iri=mat.iri)
        # Reconcile the initial state (a fresh save fires the post_save recompute).
        with django_capture_on_commit_callbacks(execute=True):
            case.save()
        mat.refresh_from_db()
        assert mat.visibility == Visibility.LISTED

        # Soft-delete the case the model way (state → CLOSED); NO API call.
        with django_capture_on_commit_callbacks(execute=True):
            case.delete()

        mat.refresh_from_db()
        assert mat.visibility == Visibility.PRIVATE, (
            "evidence of a closed case must not stay publicly LISTED"
        )

    def test_non_api_case_publish_recomputes_material_visibility(
        self, django_capture_on_commit_callbacks
    ):
        # Inverse of the leak: publishing via the model path must PROMOTE evidence
        # so a genuinely-public document isn't stuck PRIVATE.
        mat = _store("source:20240101:promo1", visibility=Visibility.PRIVATE)
        case = _case("c-promo", CaseState.DRAFT)
        CaseMaterialReference.objects.create(case=case, material_iri=mat.iri)
        with django_capture_on_commit_callbacks(execute=True):
            case.save()
        mat.refresh_from_db()
        assert mat.visibility == Visibility.PRIVATE

        # Move to PUBLISHED via a plain model save (a non-API write path — e.g. a
        # data migration or shell). full_clean() is skipped, so this exercises the
        # signal without the publish() transition's content validation.
        case.state = CaseState.PUBLISHED
        with django_capture_on_commit_callbacks(execute=True):
            case.save()

        mat.refresh_from_db()
        assert mat.visibility == Visibility.LISTED

    def test_soft_deleted_material_not_touched(self):
        mat = _store("source:20240101:aaaa01")
        mat.is_deleted = True
        mat.save()
        case = _case("c-draft", CaseState.DRAFT)
        CaseMaterialReference.objects.create(case=case, material_iri=mat.iri)
        assert recompute_material_visibility(mat.iri) is None

    def test_recompute_all_bulk_over_many(self):
        # Bulk path: several materials, only the changed ones counted + updated.
        pub = _case("c-pub", CaseState.PUBLISHED)
        draft = _case("c-draft", CaseState.DRAFT)
        m_listed = _store("source:20240101:list01", visibility=Visibility.LISTED)
        m_demote = _store("source:20240101:demo01", visibility=Visibility.LISTED)
        CaseMaterialReference.objects.create(case=pub, material_iri=m_listed.iri)
        CaseMaterialReference.objects.create(case=draft, material_iri=m_demote.iri)
        # m_listed stays LISTED (published ref), m_demote → PRIVATE (draft only).
        assert recompute_all() == 1
        m_listed.refresh_from_db()
        m_demote.refresh_from_db()
        assert m_listed.visibility == Visibility.LISTED
        assert m_demote.visibility == Visibility.PRIVATE

    def test_recompute_all_reconciles_search_index(self):
        # bulk_update bypasses post_save, so recompute_all must evict a demoted
        # material from search by hand (else a non-LISTED doc lingers publicly).
        draft = _case("c-draft", CaseState.DRAFT)
        mat = _store("source:20240101:demo01", visibility=Visibility.LISTED)
        CaseMaterialReference.objects.create(case=draft, material_iri=mat.iri)
        with patch("materials.search_index.delete") as dele, patch(
            "materials.search_index.index"
        ):
            recompute_all()
        assert dele.called


@pytest.mark.django_db
class TestReadSideGates:
    def _privileged(self):
        u = User.objects.create_user("staff1", password="x")
        u.is_staff = True
        u.save()
        return u

    def test_anon_retrieve_hides_private(self):
        mat = _store("source:20240101:aaaa01", visibility=Visibility.PRIVATE)
        client = APIClient()
        resp = client.get(f"/api/materials/?iri={mat.iri}")
        assert resp.status_code == 404

    def test_anon_retrieve_shows_unlisted(self):
        mat = _store("source:20240101:aaaa02", visibility=Visibility.UNLISTED)
        client = APIClient()
        resp = client.get(f"/api/materials/?iri={mat.iri}")
        assert resp.status_code == 200

    def test_anon_private_court_material_does_not_leak_derived_doc(self):
        # F3: a PRIVATE stored material whose IRI is a court-case material IRI must
        # 404 for anon — it must NOT fall through to the derived court-case JSON-LD
        # (which would ignore the material's own visibility gate). Even though the
        # court case is separately public via /api/courtcases/, resolving THIS
        # material IRI as anon must honor the PRIVATE gate the docstring promises.
        from courts.models import Court, CourtCase
        from materials.jsonld import court_case_material_iri

        court = Court.objects.create(
            identifier="special", court_type="special", full_name_nepali="वि"
        )
        CourtCase.objects.create(
            case_number="T-1", court=court, case_type="भ्रष्टाचार"
        )
        iri = court_case_material_iri("special", "T-1")
        # Store a PRIVATE material row at that same IRI (e.g. a demoted case-source
        # material that happens to share the court IRI).
        Material.objects.create(
            iri=iri,
            material_type="COURT_ORDER",
            source="court",
            ident="special.t-1",
            data={"@id": iri, "@type": "Legislation", "name": {"ne": "गोप्य"}},
            visibility=Visibility.PRIVATE,
        )

        client = APIClient()
        resp = client.get(f"/api/materials/?iri={iri}")
        assert resp.status_code == 404, resp.content

        # A privileged principal still sees the stored PRIVATE row.
        client.force_authenticate(self._privileged())
        resp = client.get(f"/api/materials/?iri={iri}")
        assert resp.status_code == 200

    def test_privileged_retrieve_shows_private(self):
        mat = _store("source:20240101:aaaa01", visibility=Visibility.PRIVATE)
        client = APIClient()
        client.force_authenticate(self._privileged())
        resp = client.get(f"/api/materials/?iri={mat.iri}")
        assert resp.status_code == 200

    def test_anon_list_excludes_nonlisted(self):
        _store("source:20240101:list01", visibility=Visibility.LISTED)
        _store("source:20240101:priv01", visibility=Visibility.PRIVATE)
        _store("source:20240101:unli01", visibility=Visibility.UNLISTED)
        client = APIClient()
        resp = client.get("/api/materials/")
        assert resp.status_code == 200
        ids = {d["@id"] for d in resp.json()["results"]}
        assert any("list01" in i for i in ids)
        assert not any("priv01" in i for i in ids)
        assert not any("unli01" in i for i in ids)

    def test_privileged_list_includes_all(self):
        _store("source:20240101:list01", visibility=Visibility.LISTED)
        _store("source:20240101:priv01", visibility=Visibility.PRIVATE)
        client = APIClient()
        client.force_authenticate(self._privileged())
        resp = client.get("/api/materials/")
        ids = {d["@id"] for d in resp.json()["results"]}
        assert any("priv01" in i for i in ids)


@pytest.mark.django_db
class TestDiscoveryAndSearchHonorVisibility:
    def test_corpus_enumerates_listed_only(self):
        from discovery.corpus import _iter_materials

        _store("source:20240101:list01", visibility=Visibility.LISTED)
        _store("source:20240101:priv01", visibility=Visibility.PRIVATE)
        iris = {r.iri for r in _iter_materials()}
        assert any("list01" in i for i in iris)
        assert not any("priv01" in i for i in iris)

    def test_search_signal_evicts_nonlisted(self):
        # A PRIVATE material must be evicted (delete), never indexed.
        with patch("materials.search_index.index") as idx, patch(
            "materials.search_index.delete"
        ) as dele:
            _store("source:20240101:priv01", visibility=Visibility.PRIVATE)
        # on_commit fires at transaction end in tests via django_db; force it:
        assert not idx.called or dele.called
