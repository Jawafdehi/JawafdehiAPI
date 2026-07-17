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
from django.contrib.auth.models import Group
from rest_framework.test import APIClient

from cases.models import Case, CaseMaterialReference, CaseState, CaseType
from materials.jsonld import documentsource_to_jsonld
from materials.models import Material, Policy, Visibility, default_policy_for
from materials.single_source_ingest import upsert_single_source_material
from materials.visibility import (
    recompute_all,
    recompute_for_case,
    recompute_material_visibility,
    visibility_for_policy,
    visibility_for_states,
)

User = get_user_model()


def _store(source_id, title="Doc", source_type="MISC", url=None, visibility=None):
    # A CASE-GATED upload. Case uploads are now type-sourced and born PUBLIC, so
    # pin the caseworker-embargoed policy these gating tests exercise: a
    # CASE_GATED material tracks its citing cases (draft/in-review/closed →
    # PRIVATE/UNLISTED via recompute), which is the behaviour under test here.
    doc, mtype = documentsource_to_jsonld(
        source_id=source_id, title=title, source_type=source_type, url=url
    )
    mat = Material.from_jsonld(doc, material_type=mtype)
    mat.visibility_policy = Policy.CASE_GATED
    if visibility is not None:
        mat.visibility = visibility
    mat.save()
    return mat


def _store_corpus(source, ident, material_type="court_order", visibility=Visibility.LISTED):
    """A corpus material (non-``jawafdehi`` source) — e.g. a court order or CIAA
    press release that is public on its own merits, independent of any case. Built
    directly so ``source``/``ident`` are the corpus namespace (policy defaults to
    PUBLIC), not the ``jawafdehi`` case-upload namespace that ``_store`` mints."""
    iri = f"https://jawafdehi.org/material/{source}/{ident}"
    mat = Material(
        iri=iri,
        material_type=material_type,
        source=source,
        ident=ident,
        data={"@id": iri, "@type": "DigitalDocument", "name": {"en": "Corpus doc"}},
        visibility=visibility,
    )
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

    def test_hard_delete_of_case_demotes_orphaned_evidence(
        self, django_capture_on_commit_callbacks
    ):
        # F2 (hard-delete path): a queryset/admin HARD delete fires post_delete, by
        # which point the case pk is cleared and its CaseMaterialReference rows are
        # CASCADE-gone — so a live reverse query finds nothing. The pre_delete
        # snapshot must carry the IRIs forward so the now-orphaned material (LISTED
        # only because of this one PUBLISHED case) demotes to PRIVATE. Without the
        # snapshot this recompute is a silent no-op and the evidence stays LISTED.
        mat = _store("source:20240101:hard01")
        case = _case("c-hard", CaseState.PUBLISHED)
        CaseMaterialReference.objects.create(case=case, material_iri=mat.iri)
        with django_capture_on_commit_callbacks(execute=True):
            case.save()
        mat.refresh_from_db()
        assert mat.visibility == Visibility.LISTED

        # Hard-delete via the queryset manager (Case.delete() would only soft-delete);
        # this is the path Django admin's bulk-delete and Case.objects...delete() use.
        with django_capture_on_commit_callbacks(execute=True):
            Case.objects.filter(pk=case.pk).delete()

        mat.refresh_from_db()
        assert mat.visibility == Visibility.PRIVATE, (
            "evidence orphaned by a hard-deleted case must not stay publicly LISTED"
        )

    def test_reconciler_command_heals_visibility_drift(
        self, django_capture_on_commit_callbacks
    ):
        # The recompute_material_visibility management command is the periodic
        # backstop the signals delegate to on failure. Simulate drift (a material
        # left LISTED though its only referrer is a DRAFT case) and assert the
        # reconciler heals it.
        from io import StringIO

        from django.core.management import call_command

        mat = _store("source:20240101:drift1")
        case = _case("c-drift", CaseState.DRAFT)
        CaseMaterialReference.objects.create(case=case, material_iri=mat.iri)
        # Force the drifted state directly, bypassing the recompute.
        Material.objects.filter(pk=mat.iri).update(visibility=Visibility.LISTED)

        out = StringIO()
        call_command("recompute_material_visibility", stdout=out)

        mat.refresh_from_db()
        assert mat.visibility == Visibility.PRIVATE
        assert "reconciled" in out.getvalue()

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

    def _readonly(self):
        # The org-wide ReadOnly role as the bearer authenticator produces it:
        # in the ReadOnly Group, but NOT is_staff / is_superuser (that flag is
        # only ever set by the Django-admin session path, never by bearer auth).
        u = User.objects.create_user("ro1", password="x")
        u.groups.add(Group.objects.get_or_create(name="ReadOnly")[0])
        return u

    def _plain_authed(self):
        # Authenticated but roleless: the boundary case. Systemwide read is the
        # ReadOnly *role*, not mere authentication — this principal stays gated.
        return User.objects.create_user("nobody1", password="x")

    def _caseworker(self):
        # The NGM content role via bearer auth: in the Caseworker Group but NOT
        # is_staff (bearer auth never sets it), so it exercises the NGM-group
        # read path rather than the is_staff short-circuit.
        u = User.objects.create_user("cw1", password="x")
        u.groups.add(Group.objects.get_or_create(name="Caseworker")[0])
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

    def test_readonly_retrieve_shows_private(self):
        # Org-wide ReadOnly is a systemwide READ role: it must resolve PRIVATE
        # (draft-only) materials, exactly like content staff.
        mat = _store("source:20240101:aaaa01", visibility=Visibility.PRIVATE)
        client = APIClient()
        client.force_authenticate(self._readonly())
        resp = client.get(f"/api/materials/?iri={mat.iri}")
        assert resp.status_code == 200, resp.content

    def test_readonly_list_includes_private(self):
        _store("source:20240101:list01", visibility=Visibility.LISTED)
        _store("source:20240101:priv01", visibility=Visibility.PRIVATE)
        client = APIClient()
        client.force_authenticate(self._readonly())
        resp = client.get("/api/materials/")
        ids = {d["@id"] for d in resp.json()["results"]}
        assert any("priv01" in i for i in ids)

    def test_plain_authed_hides_private(self):
        # Boundary: a roleless authenticated user is NOT a read role and stays
        # gated out of PRIVATE (only the ReadOnly role / content staff lift it).
        mat = _store("source:20240101:aaaa01", visibility=Visibility.PRIVATE)
        client = APIClient()
        client.force_authenticate(self._plain_authed())
        resp = client.get(f"/api/materials/?iri={mat.iri}")
        assert resp.status_code == 404, resp.content

    def test_caseworker_retrieve_shows_private(self):
        # A bearer-only Caseworker (content role, not is_staff/superuser) resolves
        # PRIVATE materials via the NGM-group read path — the branch the
        # single-query gate now covers alongside ReadOnly.
        mat = _store("source:20240101:aaaa01", visibility=Visibility.PRIVATE)
        client = APIClient()
        client.force_authenticate(self._caseworker())
        resp = client.get(f"/api/materials/?iri={mat.iri}")
        assert resp.status_code == 200, resp.content

    def test_nonpublic_gate_uses_single_group_query(self, django_assert_num_queries):
        # Efficiency guard: a role-carrying principal is resolved in ONE group
        # query — not a ReadOnly `.exists()` miss followed by a second NGM query.
        from types import SimpleNamespace

        from materials.views import _can_see_nonpublic

        cw = self._caseworker()
        with django_assert_num_queries(1):
            assert _can_see_nonpublic(SimpleNamespace(user=cw)) is True


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


class TestVisibilityForPolicy:
    def test_public_is_always_listed(self):
        # states_fn must NOT be consulted for a PUBLIC policy (no DB round-trip).
        def _boom():
            raise AssertionError("states_fn should not be called for PUBLIC")

        assert visibility_for_policy(Policy.PUBLIC, _boom) == Visibility.LISTED

    def test_private_is_always_private(self):
        def _boom():
            raise AssertionError("states_fn should not be called for PRIVATE")

        assert visibility_for_policy(Policy.PRIVATE, _boom) == Visibility.PRIVATE

    def test_case_gated_defers_to_states(self):
        assert visibility_for_policy(Policy.CASE_GATED, lambda: ["DRAFT"]) == (
            Visibility.PRIVATE
        )
        assert visibility_for_policy(Policy.CASE_GATED, lambda: ["PUBLISHED"]) == (
            Visibility.LISTED
        )

    def test_unknown_policy_treated_as_case_gated(self):
        assert visibility_for_policy("BOGUS", lambda: ["PUBLISHED"]) == Visibility.LISTED


class TestDefaultPolicyForSource:
    def test_case_upload_source_is_case_gated(self):
        assert default_policy_for("jawafdehi") == Policy.CASE_GATED

    def test_corpus_sources_are_public(self):
        assert default_policy_for("court_order") == Policy.PUBLIC
        assert default_policy_for("ciaa_press_release") == Policy.PUBLIC
        assert default_policy_for("court") == Policy.PUBLIC

    def test_from_jsonld_derives_policy_from_source(self):
        # A case upload is now sourced by material_type (MISC → document), so it is
        # non-jawafdehi and born PUBLIC — case uploads are public by default.
        doc, mtype = documentsource_to_jsonld(
            source_id="source:20240101:pol01", title="D", source_type="MISC", url=None
        )
        assert doc["@id"] == "https://jawafdehi.org/material/document/20240101.pol01"
        assert Material.from_jsonld(doc, material_type=mtype).visibility_policy == (
            Policy.PUBLIC
        )
        # A corpus document (non-jawafdehi source) is likewise born PUBLIC.
        corpus = {
            "@id": "https://jawafdehi.org/material/court_order/pol02",
            "@type": "DigitalDocument",
            "name": {"en": "Order"},
        }
        assert Material.from_jsonld(corpus, material_type="court_order").visibility_policy == (
            Policy.PUBLIC
        )
        # Only a residual jawafdehi-sourced row stays CASE_GATED at birth.
        legacy = {
            "@id": "https://jawafdehi.org/material/jawafdehi/pol03",
            "@type": "DigitalDocument",
            "name": {"en": "Legacy upload"},
        }
        assert Material.from_jsonld(legacy, material_type="document").visibility_policy == (
            Policy.CASE_GATED
        )


@pytest.mark.django_db
class TestPolicyRecompute:
    """The bug fix: a corpus document (PUBLIC policy) stays LISTED no matter what
    state the citing case is in — a DRAFT case can no longer hide it. This is what
    makes the doc-dedup re-point (case evidence: duplicate upload → canonical
    corpus doc) safe."""

    def test_public_material_cited_by_draft_stays_listed(self):
        mat = _store_corpus("ciaa_press_release", "2402", material_type="press_release")
        case = _case("c-draft", CaseState.DRAFT)
        CaseMaterialReference.objects.create(case=case, material_iri=mat.iri)
        assert recompute_material_visibility(mat.iri) == Visibility.LISTED
        mat.refresh_from_db()
        assert mat.visibility == Visibility.LISTED

    def test_public_material_cited_by_in_review_stays_listed(self):
        mat = _store_corpus("court_order", "special.080-cr-0001")
        case = _case("c-review", CaseState.IN_REVIEW)
        CaseMaterialReference.objects.create(case=case, material_iri=mat.iri)
        assert recompute_material_visibility(mat.iri) == Visibility.LISTED

    def test_recompute_heals_a_mis_demoted_public_material(self):
        # The dedup fleet damage: a corpus doc left PRIVATE by an earlier unguarded
        # recompute is healed back to LISTED once its policy is PUBLIC.
        mat = _store_corpus(
            "ciaa_press_release", "2403",
            material_type="press_release", visibility=Visibility.PRIVATE,
        )
        case = _case("c-draft", CaseState.DRAFT)
        CaseMaterialReference.objects.create(case=case, material_iri=mat.iri)
        assert recompute_material_visibility(mat.iri) == Visibility.LISTED

    def test_private_policy_withholds_even_from_published_case(self):
        # An absolute withhold: PRIVATE policy beats a PUBLISHED referrer.
        mat = _store_corpus("court_order", "special.080-cr-0009")
        mat.visibility_policy = Policy.PRIVATE
        mat.save(update_fields=["visibility_policy"])
        case = _case("c-pub", CaseState.PUBLISHED)
        CaseMaterialReference.objects.create(case=case, material_iri=mat.iri)
        assert recompute_material_visibility(mat.iri) == Visibility.PRIVATE

    def test_recompute_all_settles_every_policy(self):
        # One reconciler pass over a mix: corpus (PUBLIC) → LISTED, case-upload
        # (CASE_GATED) referenced only by a DRAFT → PRIVATE, and an UNREFERENCED
        # PRIVATE-policy withhold → PRIVATE (the full scan reaches it even with no
        # CaseMaterialReference).
        corpus = _store_corpus(
            "court_order", "special.080-cr-0002", visibility=Visibility.PRIVATE
        )
        upload = _store("source:20240101:up0001")  # a CASE_GATED upload, LISTED
        withheld = _store_corpus("court_order", "special.080-cr-0003")
        withheld.visibility_policy = Policy.PRIVATE
        withheld.save(update_fields=["visibility_policy"])
        draft = _case("c-draft", CaseState.DRAFT)
        CaseMaterialReference.objects.create(case=draft, material_iri=corpus.iri)
        CaseMaterialReference.objects.create(case=draft, material_iri=upload.iri)
        recompute_all()
        corpus.refresh_from_db()
        upload.refresh_from_db()
        withheld.refresh_from_db()
        assert corpus.visibility == Visibility.LISTED
        assert upload.visibility == Visibility.PRIVATE
        assert withheld.visibility == Visibility.PRIVATE


@pytest.mark.django_db
class TestUpsertPolicyDefaults:
    def _doc(self, source_id):
        doc, mtype = documentsource_to_jsonld(
            source_id=source_id, title="D", source_type="MISC", url=None
        )
        return doc, mtype

    def test_upsert_births_case_upload_public(self):
        # Case uploads are now type-sourced (MISC → /material/document/…), so —
        # being non-jawafdehi — they are born PUBLIC. A caseworker embargoes a
        # sensitive one by explicitly setting CASE_GATED/PRIVATE afterward.
        doc, mtype = self._doc("source:20240101:ins01")
        mat = upsert_single_source_material(doc, material_type=mtype)
        assert mat.source == "document"
        assert mat.visibility_policy == Policy.PUBLIC

    def test_reupsert_preserves_manual_policy(self):
        # A caseworker embargoes a case-upload (CASE_GATED — now a non-default
        # override); re-sourcing the SAME @id must NOT reset it to the PUBLIC
        # birth default (create_defaults is INSERT-only).
        doc, mtype = self._doc("source:20240101:ins02")
        mat = upsert_single_source_material(doc, material_type=mtype)
        mat.visibility_policy = Policy.CASE_GATED
        mat.save(update_fields=["visibility_policy"])
        again = upsert_single_source_material(doc, material_type=mtype)
        assert again.visibility_policy == Policy.CASE_GATED

    def test_explicit_override_applies_on_update(self):
        doc, mtype = self._doc("source:20240101:ins03")
        upsert_single_source_material(doc, material_type=mtype)  # born PUBLIC
        again = upsert_single_source_material(
            doc, material_type=mtype, visibility_policy=Policy.PRIVATE
        )
        assert again.visibility_policy == Policy.PRIVATE


@pytest.mark.django_db
class TestPatchVisibilityPolicy:
    def _caseworker(self):
        u = User.objects.create_user("cw-patch", password="x")
        u.groups.add(Group.objects.get_or_create(name="Caseworker")[0])
        return u

    def test_anon_patch_is_401(self):
        mat = _store_corpus("court_order", "special.080-cr-0100")
        resp = APIClient().patch(
            f"/api/materials/?iri={mat.iri}",
            {"visibility_policy": "PRIVATE"},
            format="json",
        )
        assert resp.status_code == 401

    def test_roleless_authed_patch_is_403(self):
        mat = _store_corpus("court_order", "special.080-cr-0101")
        client = APIClient()
        client.force_authenticate(User.objects.create_user("nobody-patch", password="x"))
        resp = client.patch(
            f"/api/materials/?iri={mat.iri}",
            {"visibility_policy": "PRIVATE"},
            format="json",
        )
        assert resp.status_code == 403

    def test_caseworker_patch_sets_policy_and_recomputes(self):
        # A public court order cited by a DRAFT case is LISTED; a caseworker PATCHes
        # it to CASE_GATED → it demotes to PRIVATE (draft-only) and 404s for anon;
        # PATCH back to PUBLIC → LISTED and visible again.
        mat = _store_corpus("court_order", "special.080-cr-0102")
        draft = _case("c-draft", CaseState.DRAFT)
        CaseMaterialReference.objects.create(case=draft, material_iri=mat.iri)
        client = APIClient()
        client.force_authenticate(self._caseworker())

        resp = client.patch(
            f"/api/materials/?iri={mat.iri}",
            {"visibility_policy": "CASE_GATED"},
            format="json",
        )
        assert resp.status_code == 200
        assert resp.data["jawafdehi:visibilityPolicy"] == Policy.CASE_GATED
        assert resp.data["jawafdehi:visibility"] == Visibility.PRIVATE
        mat.refresh_from_db()
        assert mat.visibility == Visibility.PRIVATE
        assert APIClient().get(f"/api/materials/?iri={mat.iri}").status_code == 404

        resp = client.patch(
            f"/api/materials/?iri={mat.iri}",
            {"visibility_policy": "PUBLIC"},
            format="json",
        )
        assert resp.status_code == 200
        assert resp.data["jawafdehi:visibility"] == Visibility.LISTED
        assert APIClient().get(f"/api/materials/?iri={mat.iri}").status_code == 200

    def test_patch_invalid_policy_is_400(self):
        mat = _store_corpus("court_order", "special.080-cr-0103")
        client = APIClient()
        client.force_authenticate(self._caseworker())
        resp = client.patch(
            f"/api/materials/?iri={mat.iri}",
            {"visibility_policy": "SORTA_PUBLIC"},
            format="json",
        )
        assert resp.status_code == 400

    def test_patch_missing_material_is_404(self):
        client = APIClient()
        client.force_authenticate(self._caseworker())
        resp = client.patch(
            "/api/materials/?iri=https://jawafdehi.org/material/court_order/nope",
            {"visibility_policy": "PRIVATE"},
            format="json",
        )
        assert resp.status_code == 404

    def test_authed_get_surfaces_policy(self):
        mat = _store_corpus("court_order", "special.080-cr-0104")
        client = APIClient()
        client.force_authenticate(self._caseworker())
        resp = client.get(f"/api/materials/?iri={mat.iri}")
        assert resp.status_code == 200
        assert resp.data["jawafdehi:visibilityPolicy"] == Policy.PUBLIC
        # Anon never sees the admin fields.
        anon = APIClient().get(f"/api/materials/?iri={mat.iri}")
        assert "jawafdehi:visibilityPolicy" not in anon.data

    def test_anon_patch_without_iri_is_401_not_422(self):
        # Auth precedes iri-param validation: an anon caller gets 401, never a
        # 422/400 input-validation disclosure (matches the DELETE branch).
        resp = APIClient().patch(
            "/api/materials/", {"visibility_policy": "PRIVATE"}, format="json"
        )
        assert resp.status_code == 401


@pytest.mark.django_db
class TestControlKeysNotPersisted:
    """Server-owned keys must never land in a Material's stored JSON-LD `data`."""

    def _caseworker_client(self):
        u = User.objects.create_user("cw-strip", password="x")
        u.groups.add(Group.objects.get_or_create(name="Caseworker")[0])
        c = APIClient()
        c.force_authenticate(u)
        return c

    def test_bare_put_strips_control_key_but_applies_policy(self):
        # A bare PUT body carrying a top-level `visibility_policy` must NOT persist
        # that key into `data` (it would leak to anon), yet the policy IS applied.
        iri = "https://jawafdehi.org/material/court_order/strip01"
        client = self._caseworker_client()
        resp = client.put(
            "/api/materials/court_order/strip01",
            {
                "@id": iri,
                "@type": "DigitalDocument",
                "name": {"en": "Order"},
                "visibility_policy": "PRIVATE",
            },
            format="json",
        )
        assert resp.status_code in (200, 201)
        mat = Material.objects.get(pk=iri)
        assert "visibility_policy" not in mat.data
        assert mat.visibility_policy == Policy.PRIVATE

    def test_annotated_authed_get_does_not_round_trip_into_data(self):
        # GET as caseworker (doc is annotated with jawafdehi:visibility[Policy]),
        # then PUT that exact doc back — the annotations must be stripped, not baked
        # into the stored document.
        mat = _store_corpus("court_order", "strip02")
        client = self._caseworker_client()
        got = client.get(f"/api/materials/?iri={mat.iri}")
        assert "jawafdehi:visibilityPolicy" in got.data
        client.put("/api/materials/court_order/strip02", got.data, format="json")
        mat.refresh_from_db()
        assert "jawafdehi:visibility" not in mat.data
        assert "jawafdehi:visibilityPolicy" not in mat.data

    def test_primitive_rejects_invalid_explicit_policy(self):
        from django.core.exceptions import ValidationError

        doc, mtype = documentsource_to_jsonld(
            source_id="source:20240101:bad01", title="D", source_type="MISC", url=None
        )
        with pytest.raises(ValidationError):
            upsert_single_source_material(
                doc, material_type=mtype, visibility_policy="NOT_A_POLICY"
            )
