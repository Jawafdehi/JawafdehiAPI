# SPDX-License-Identifier: Hippocratic-3.0
"""The decision publisher, and the guarantee that it can never break an approval.

Uses ``django_capture_on_commit_callbacks`` so the on_commit hook actually runs
inside the test transaction — without it these would all pass vacuously, which
is the usual way an on_commit path ships broken.
"""

from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import override_settings
from rest_framework.test import APIClient

from case_proposals.models import CaseUpdateProposal, ProposalStatus
from case_proposals.publish import build_decision_envelope, schedule_decision_event
from cases.models import Case, CaseType

LIST_URL = "/api/case-update-proposals/"


def make_caseworker():
    User = get_user_model()
    user = User.objects.create_user(username="u-caseworker", password="x")
    group, _ = Group.objects.get_or_create(name="Caseworker")
    user.groups.add(group)
    return user


def caseworker_client():
    client = APIClient()
    client.force_authenticate(user=make_caseworker())
    return client


def make_case(slug="lalita-niwas-land-scam"):
    return Case.objects.create(
        title="Lalita Niwas land scam", case_type=CaseType.CORRUPTION, slug=slug
    )


def make_proposal(**over):
    data = dict(
        case_slug="lalita-niwas-land-scam",
        case_title="Lalita Niwas land scam",
        source_kind="ngm_docket",
        intent={
            "type": "append_timeline_entry",
            "entry": {"date": "2026-08-12", "title": "Hearing scheduled"},
        },
        confidence=0.97,
        detected_by="consumer:proposal-builder",
        dedup_key="docket:x:hearing:1",
    )
    data.update(over)
    return CaseUpdateProposal.objects.create(**data)


@pytest.mark.django_db
class TestEnvelope:
    def test_approved_and_rejected_map_to_distinct_subjects(self):
        approved = make_proposal(status=ProposalStatus.APPROVED)
        rejected = make_proposal(status=ProposalStatus.REJECTED, dedup_key="d2")
        assert build_decision_envelope(approved)["subject"] == "jaw.case.update.approved"
        assert build_decision_envelope(rejected)["subject"] == "jaw.case.update.rejected"

    def test_case_iri_leads_the_subject_refs(self):
        p = make_proposal(status=ProposalStatus.APPROVED)
        refs = build_decision_envelope(p)["subject_refs"]
        assert refs[0] == "https://jawafdehi.org/case/lalita-niwas-land-scam"

    def test_producer_subject_refs_are_preserved_and_deduplicated(self):
        docket = "https://jawafdehi.org/courtcase/special/082-cr-0154"
        case_iri = "https://jawafdehi.org/case/lalita-niwas-land-scam"
        p = make_proposal(
            status=ProposalStatus.APPROVED,
            subject_refs=[docket, case_iri],  # case_iri duplicated on purpose
        )
        refs = build_decision_envelope(p)["subject_refs"]
        assert refs == [case_iri, docket]

    def test_decision_dedup_key_differs_from_the_fact_dedup_key(self):
        # The fact key identifies WHAT happened; the decision key identifies the
        # decision, so re-publishing a decision collapses without suppressing a
        # genuinely different event about the same fact.
        p = make_proposal(status=ProposalStatus.APPROVED, dedup_key="docket:x:hearing:1")
        env = build_decision_envelope(p)
        assert env["dedup_key"] == f"proposal:{p.pk}:approved"
        assert env["payload"]["fact_dedup_key"] == "docket:x:hearing:1"

    def test_payload_carries_the_intent_and_confidence(self):
        p = make_proposal(status=ProposalStatus.APPROVED)
        payload = build_decision_envelope(p)["payload"]
        assert payload["intent"]["type"] == "append_timeline_entry"
        assert payload["confidence"] == 0.97
        assert payload["case_slug"] == "lalita-niwas-land-scam"

    def test_a_broken_case_iri_degrades_rather_than_raising(self):
        p = make_proposal(status=ProposalStatus.APPROVED)
        with mock.patch(
            "jawafdehi_shared.entities.ids.build_case_iri", side_effect=ValueError("bad")
        ):
            env = build_decision_envelope(p)
        assert env["subject_refs"] == []  # degraded, but still a message

    def test_pending_proposals_publish_nothing(self):
        p = make_proposal(status=ProposalStatus.PENDING)
        with mock.patch("case_proposals.publish.transaction.on_commit") as on_commit:
            schedule_decision_event(p)
        on_commit.assert_not_called()


@pytest.mark.django_db
class TestApprovalIsIndependentOfTheBroker:
    """The property worth protecting: the archive does not depend on the bus."""

    @override_settings(NATS_URL="")
    def test_approve_succeeds_with_no_broker_configured(self, django_capture_on_commit_callbacks):
        make_case()
        p = make_proposal()
        with django_capture_on_commit_callbacks(execute=True):
            r = caseworker_client().post(f"{LIST_URL}{p.id}/approve/", {}, format="json")

        assert r.status_code == 200
        p.refresh_from_db()
        assert p.status == ProposalStatus.APPROVED
        # And the intent really was applied.
        assert len(Case.objects.get(slug="lalita-niwas-land-scam").timeline) == 1

    @override_settings(NATS_URL="nats://unreachable:4222")
    def test_approve_succeeds_when_publishing_blows_up(
        self, django_capture_on_commit_callbacks
    ):
        make_case()
        p = make_proposal()
        with mock.patch("events.bus.publish", side_effect=RuntimeError("broker down")):
            with django_capture_on_commit_callbacks(execute=True):
                r = caseworker_client().post(f"{LIST_URL}{p.id}/approve/", {}, format="json")

        assert r.status_code == 200
        p.refresh_from_db()
        assert p.status == ProposalStatus.APPROVED
        assert len(Case.objects.get(slug="lalita-niwas-land-scam").timeline) == 1

    @override_settings(NATS_URL="nats://localhost:4222")
    def test_approve_publishes_the_approved_event(self, django_capture_on_commit_callbacks):
        make_case()
        p = make_proposal()
        with mock.patch("events.bus.publish") as publish:
            with django_capture_on_commit_callbacks(execute=True):
                caseworker_client().post(f"{LIST_URL}{p.id}/approve/", {}, format="json")

        subject, envelope = publish.call_args.args
        assert subject == "jaw.case.update.approved"
        assert envelope["payload"]["proposal_id"] == p.pk
        assert envelope["payload"]["status"] == "approved"

    @override_settings(NATS_URL="nats://localhost:4222")
    def test_reject_publishes_the_rejected_event(self, django_capture_on_commit_callbacks):
        make_case()
        p = make_proposal()
        with mock.patch("events.bus.publish") as publish:
            with django_capture_on_commit_callbacks(execute=True):
                caseworker_client().post(
                    f"{LIST_URL}{p.id}/reject/", {"notes": "wrong person"}, format="json"
                )

        subject, envelope = publish.call_args.args
        assert subject == "jaw.case.update.rejected"
        assert envelope["payload"]["review_notes"] == "wrong person"

    @override_settings(NATS_URL="nats://localhost:4222")
    def test_nothing_is_published_when_the_decision_is_rejected_by_a_409(
        self, django_capture_on_commit_callbacks
    ):
        # An already-decided proposal 409s without touching the case, so it must
        # not announce a second decision.
        make_case()
        p = make_proposal(status=ProposalStatus.APPROVED)
        with mock.patch("events.bus.publish") as publish:
            with django_capture_on_commit_callbacks(execute=True):
                r = caseworker_client().post(f"{LIST_URL}{p.id}/approve/", {}, format="json")

        assert r.status_code == 409
        publish.assert_not_called()
