"""Smoke + edge-case tests for the CaseUpdateProposal review API.

Self-contained: a local user/role helper (Caseworker / ReadOnly groups + a
superuser) so the suite doesn't depend on repo-root conftest internals. Runs on
the sqlite unit DB via ``@pytest.mark.django_db``.
"""

from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db.utils import IntegrityError
from rest_framework.test import APIClient

from case_proposals.models import CaseUpdateProposal, ProposalStatus
from case_proposals.views import CaseUpdateProposalViewSet
from cases.models import Case, CaseMaterialReference, CaseType

LIST_URL = "/api/case-update-proposals/"
MATERIAL_IRI = "https://jawafdehi.org/material/court_order/specialcourt.082-cr-0179"


# ── helpers ──────────────────────────────────────────────────────────────────

def make_user(role):
    """role: 'Caseworker' | 'ReadOnly' | 'superuser'."""
    User = get_user_model()
    user = User.objects.create_user(username=f"u-{role}", password="x")
    if role == "superuser":
        user.is_superuser = True
        user.is_staff = True
        user.save()
    else:
        group, _ = Group.objects.get_or_create(name=role)
        user.groups.add(group)
    return user


def client_for(role):
    client = APIClient()
    client.force_authenticate(user=make_user(role))
    return client


def make_case(slug="lalita-niwas-land-scam", title="Lalita Niwas land scam"):
    return Case.objects.create(title=title, case_type=CaseType.CORRUPTION, slug=slug)


def timeline_payload(slug="lalita-niwas-land-scam", dedup="docket:x:hearing:1", **over):
    payload = {
        "case_slug": slug,
        "case_title": "Lalita Niwas land scam",
        "source_kind": "ngm_docket",
        "intent": {
            "type": "append_timeline_entry",
            "entry": {"date": "2026-08-12", "date_bs": "2083-04-28", "title": "Hearing scheduled"},
        },
        "confidence": 0.97,
        "detected_by": "consumer:proposal-builder",
        "dedup_key": dedup,
    }
    payload.update(over)
    return payload


def make_proposal(**over):
    """Create a pending proposal directly (bypassing the API) for approve tests."""
    data = dict(
        case_slug="lalita-niwas-land-scam",
        case_title="Lalita Niwas land scam",
        source_kind="ngm_docket",
        intent={"type": "append_timeline_entry", "entry": {"date": "2026-08-12", "title": "Hearing scheduled"}},
        confidence=0.97,
        detected_by="consumer:proposal-builder",
        dedup_key="docket:x:hearing:1",
    )
    data.update(over)
    return CaseUpdateProposal.objects.create(**data)


# ── smoke ──────────────────────────────────────────────────────────────────

@pytest.mark.django_db
class TestSmoke:
    def test_list_requires_auth(self):
        r = APIClient().get(LIST_URL)
        assert r.status_code in (401, 403)

    def test_readonly_and_caseworker_can_list(self):
        make_proposal()
        for role in ("ReadOnly", "Caseworker", "superuser"):
            r = client_for(role).get(LIST_URL)
            assert r.status_code == 200, role
            assert r.data["count"] == 1

    def test_caseworker_creates_pending_proposal(self):
        r = client_for("Caseworker").post(LIST_URL, timeline_payload(), format="json")
        assert r.status_code == 201, r.data
        assert r.data["status"] == "pending"
        assert CaseUpdateProposal.objects.count() == 1

    def test_filter_by_status_and_case(self):
        make_proposal(dedup_key="a", status=ProposalStatus.PENDING)
        make_proposal(dedup_key="b", status=ProposalStatus.APPROVED, case_slug="other")
        c = client_for("Caseworker")
        assert c.get(LIST_URL, {"status": "pending"}).data["count"] == 1
        assert c.get(LIST_URL, {"case_slug": "other"}).data["count"] == 1

    def test_approve_appends_timeline_entry_to_case(self):
        case = make_case()
        p = make_proposal()
        r = client_for("Caseworker").post(f"{LIST_URL}{p.id}/approve/", {"notes": "ok"}, format="json")
        assert r.status_code == 200, r.data
        case.refresh_from_db()
        assert len(case.timeline) == 1
        assert case.timeline[0]["title"] == "Hearing scheduled"
        p.refresh_from_db()
        assert p.status == ProposalStatus.APPROVED
        assert p.reviewer.startswith("caseworker:")
        assert p.reviewed_at is not None
        assert p.review_notes == "ok"

    def test_approve_link_material_creates_reference(self):
        case = make_case()
        p = make_proposal(
            source_kind="court_order",
            intent={"type": "link_material", "material": MATERIAL_IRI, "relation": "court_order"},
            dedup_key="link:1",
        )
        r = client_for("Caseworker").post(f"{LIST_URL}{p.id}/approve/", {}, format="json")
        assert r.status_code == 200, r.data
        assert CaseMaterialReference.objects.filter(case=case, material_iri=MATERIAL_IRI).exists()

    def test_approve_raw_patch_sets_scalar(self):
        case = make_case()
        p = make_proposal(
            source_kind="court_order",
            intent={"type": "raw_patch", "patch": [{"op": "replace", "path": "/public_notes", "value": "Updated note."}]},
            dedup_key="patch:1",
        )
        r = client_for("Caseworker").post(f"{LIST_URL}{p.id}/approve/", {}, format="json")
        assert r.status_code == 200, r.data
        case.refresh_from_db()
        assert case.public_notes == "Updated note."

    def test_reject_leaves_case_untouched(self):
        case = make_case()
        p = make_proposal()
        r = client_for("Caseworker").post(f"{LIST_URL}{p.id}/reject/", {"notes": "wrong person"}, format="json")
        assert r.status_code == 200, r.data
        case.refresh_from_db()
        assert case.timeline == []
        p.refresh_from_db()
        assert p.status == ProposalStatus.REJECTED
        assert p.review_notes == "wrong person"


# ── edge cases ───────────────────────────────────────────────────────────────

@pytest.mark.django_db
class TestEdge:
    def test_confidence_required(self):
        payload = timeline_payload()
        payload.pop("confidence")
        r = client_for("Caseworker").post(LIST_URL, payload, format="json")
        assert r.status_code == 400
        assert "confidence" in r.data

    @pytest.mark.parametrize("bad", [1.5, -0.1])
    def test_confidence_out_of_range(self, bad):
        r = client_for("Caseworker").post(LIST_URL, timeline_payload(confidence=bad), format="json")
        assert r.status_code == 400
        assert "confidence" in r.data

    @pytest.mark.parametrize("bad", [1.5, -0.1])
    def test_confidence_out_of_range_rejected_by_the_db_too(self, bad):
        """The [0, 1] bound holds for writers that never touch the serializer."""
        with pytest.raises(IntegrityError):
            make_proposal(confidence=bad)

    def test_unknown_intent_type_rejected_on_create(self):
        r = client_for("Caseworker").post(
            LIST_URL, timeline_payload(intent={"type": "frobnicate", "foo": 1}), format="json"
        )
        assert r.status_code == 400
        assert "intent" in r.data

    def test_timeline_intent_missing_title_rejected_on_create(self):
        bad = {"type": "append_timeline_entry", "entry": {"date": "2026-08-12"}}
        r = client_for("Caseworker").post(LIST_URL, timeline_payload(intent=bad), format="json")
        assert r.status_code == 400

    def test_duplicate_dedup_key_rejected(self):
        c = client_for("Caseworker")
        assert c.post(LIST_URL, timeline_payload(dedup="dup"), format="json").status_code == 201
        r = c.post(LIST_URL, timeline_payload(dedup="dup"), format="json")
        assert r.status_code == 400
        assert "dedup_key" in r.data

    def test_approve_twice_is_conflict(self):
        make_case()
        p = make_proposal()
        c = client_for("Caseworker")
        assert c.post(f"{LIST_URL}{p.id}/approve/", {}, format="json").status_code == 200
        r = c.post(f"{LIST_URL}{p.id}/approve/", {}, format="json")
        assert r.status_code == 409

    def test_stale_pending_instance_cannot_re_apply(self):
        """A decided proposal is not re-applied even if the view holds a stale instance.

        This is the time-of-check/time-of-use hazard behind the ``select_for_update``
        in ``_decide``: two concurrent approvals both read the row while it is still
        PENDING, so a status check against the *already loaded* instance passes twice
        and the intent gets applied twice. sqlite (the unit DB) does not implement
        row locking, so a genuine race isn't reproducible here — instead we hand the
        view exactly what the losing request of that race would hold, a PENDING
        in-memory instance whose committed row is already APPROVED, and assert the
        re-read inside the transaction catches it.
        """
        case = make_case()
        p = make_proposal()
        c = client_for("Caseworker")
        assert c.post(f"{LIST_URL}{p.id}/approve/", {}, format="json").status_code == 200
        case.refresh_from_db()
        assert len(case.timeline) == 1  # applied exactly once so far

        stale = CaseUpdateProposal.objects.get(pk=p.pk)
        stale.status = ProposalStatus.PENDING  # in memory only; DB row says APPROVED
        with mock.patch.object(CaseUpdateProposalViewSet, "get_object", return_value=stale):
            r = c.post(f"{LIST_URL}{p.id}/approve/", {}, format="json")

        assert r.status_code == 409
        case.refresh_from_db()
        assert len(case.timeline) == 1  # NOT appended a second time

    def test_approve_missing_case_is_400(self):
        p = make_proposal(case_slug="does-not-exist")  # no Case row
        r = client_for("Caseworker").post(f"{LIST_URL}{p.id}/approve/", {}, format="json")
        assert r.status_code == 400
        assert "case_slug" in r.data

    def test_raw_patch_disallowed_path_rejected_on_approve(self):
        make_case()
        p = make_proposal(
            intent={"type": "raw_patch", "patch": [{"op": "replace", "path": "/state", "value": "PUBLISHED"}]},
            dedup_key="patch:state",
        )
        r = client_for("Caseworker").post(f"{LIST_URL}{p.id}/approve/", {}, format="json")
        assert r.status_code == 400
        assert "intent" in r.data
        p.refresh_from_db()
        assert p.status == ProposalStatus.PENDING  # rolled back, still pending

    def test_set_status_cannot_be_applied_yet(self):
        make_case()
        p = make_proposal(
            intent={"type": "set_status", "field": "status", "to": "verdict_delivered"},
            dedup_key="status:1",
        )
        r = client_for("Caseworker").post(f"{LIST_URL}{p.id}/approve/", {}, format="json")
        assert r.status_code == 400

    def test_readonly_cannot_approve(self):
        make_case()
        p = make_proposal()
        r = client_for("ReadOnly").post(f"{LIST_URL}{p.id}/approve/", {}, format="json")
        assert r.status_code == 403
        p.refresh_from_db()
        assert p.status == ProposalStatus.PENDING

    def test_readonly_cannot_create(self):
        r = client_for("ReadOnly").post(LIST_URL, timeline_payload(), format="json")
        assert r.status_code == 403

    def test_jobpoller_can_create_but_cannot_approve(self):
        # JobPoller is the automation identity: it may PRODUCE proposals but must
        # NOT approve/reject them (fully human-in-loop for now).
        make_case()
        client = APIClient()
        client.force_authenticate(user=make_user("JobPoller"))
        created = client.post(LIST_URL, timeline_payload(dedup="jobpoller"), format="json")
        assert created.status_code == 201, created.data
        pid = created.data["id"]
        denied = client.post(f"{LIST_URL}{pid}/approve/", {}, format="json")
        assert denied.status_code == 403
        CaseUpdateProposal.objects.get(pk=pid)  # still pending, unapplied
        assert CaseUpdateProposal.objects.get(pk=pid).status == ProposalStatus.PENDING

    def test_jobpoller_cannot_reject(self):
        p = make_proposal(dedup_key="jobpoller-reject")
        client = APIClient()
        client.force_authenticate(user=make_user("JobPoller"))
        r = client.post(f"{LIST_URL}{p.id}/reject/", {}, format="json")
        assert r.status_code == 403


@pytest.mark.django_db
class TestSideEffects:
    """On approve, the apply path must trigger the same downstream refreshes the
    sanctioned case-PATCH path does (search reindex, material visibility) — those
    run as transaction on_commit callbacks, so capture them explicitly."""

    def test_approve_schedules_search_reindex(self, django_capture_on_commit_callbacks, monkeypatch):
        indexed = []
        monkeypatch.setattr("cases.search_index.index", lambda case, **kw: indexed.append(case.pk))
        case = make_case()
        p = make_proposal()
        with django_capture_on_commit_callbacks(execute=True):
            r = client_for("Caseworker").post(f"{LIST_URL}{p.id}/approve/", {}, format="json")
        assert r.status_code == 200, r.data
        assert case.pk in indexed, "approve should re-index the case in unified search"

    def test_link_material_recomputes_visibility_and_bumps_updated_at(
        self, django_capture_on_commit_callbacks, monkeypatch
    ):
        recomputed = []
        monkeypatch.setattr("cases.search_index.index", lambda case, **kw: None)
        monkeypatch.setattr(
            "materials.visibility.recompute_material_visibility",
            lambda iri: recomputed.append(iri),
        )
        case = make_case()
        before = case.updated_at
        p = make_proposal(
            source_kind="court_order",
            intent={"type": "link_material", "material": MATERIAL_IRI, "relation": "court_order"},
            dedup_key="link:vis",
        )
        with django_capture_on_commit_callbacks(execute=True):
            r = client_for("Caseworker").post(f"{LIST_URL}{p.id}/approve/", {}, format="json")
        assert r.status_code == 200, r.data
        assert MATERIAL_IRI in recomputed, "linking a material must recompute its visibility"
        case.refresh_from_db()
        assert case.updated_at > before, "a join-only write must still bump updated_at"


@pytest.mark.django_db
class TestAudit:
    def test_approve_audits_the_acceptor(self):
        from auditlog.models import LogEntry

        make_case()
        p = make_proposal()
        user = make_user("Caseworker")
        client = APIClient()
        client.force_authenticate(user=user)
        r = client.post(f"{LIST_URL}{p.id}/approve/", {"notes": "ok"}, format="json")
        assert r.status_code == 200, r.data
        # The status transition is audited AND attributed to the acceptor (user).
        updates = LogEntry.objects.filter(object_pk=str(p.pk), action=LogEntry.Action.UPDATE)
        assert updates.exists(), "approve should write an audit LogEntry"
        assert updates.filter(actor=user).exists(), "the acceptor must be recorded as actor"
        # And the proposal row records the acceptor handle.
        p.refresh_from_db()
        assert p.reviewer.endswith(user.username)

    def test_create_audits_the_proposed_change_intent(self):
        from auditlog.models import LogEntry

        r = client_for("Caseworker").post(LIST_URL, timeline_payload(dedup="audit-create"), format="json")
        assert r.status_code == 201, r.data
        pk = r.data["id"]
        creates = LogEntry.objects.filter(object_pk=str(pk), action=LogEntry.Action.CREATE)
        assert creates.exists()
        # The proposed-change intent is captured in the create audit entry.
        assert "append_timeline_entry" in str(creates.first().changes)
