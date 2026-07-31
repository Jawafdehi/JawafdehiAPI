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
        with mock.patch("case_events.bus.publish", side_effect=RuntimeError("broker down")):
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
        with mock.patch("case_events.bus.publish") as publish:
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
        with mock.patch("case_events.bus.publish") as publish:
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
        with mock.patch("case_events.bus.publish") as publish:
            with django_capture_on_commit_callbacks(execute=True):
                r = caseworker_client().post(f"{LIST_URL}{p.id}/approve/", {}, format="json")

        assert r.status_code == 409
        publish.assert_not_called()

    @override_settings(NATS_URL="nats://localhost:4222")
    def test_the_publish_is_deferred_until_after_commit(
        self, django_capture_on_commit_callbacks
    ):
        """Nothing may be announced while the transaction can still roll back.

        Without this, replacing ``transaction.on_commit(_run)`` with a plain
        ``_run()`` passes the whole suite — the deferral is the documented point
        of the design and was previously pinned by nothing. ``execute=False``
        captures the callbacks without running them, so the request completes
        with the publish still pending.
        """
        make_case()
        p = make_proposal()
        with mock.patch("case_events.bus.publish") as publish:
            with django_capture_on_commit_callbacks(execute=False) as callbacks:
                r = caseworker_client().post(f"{LIST_URL}{p.id}/approve/", {}, format="json")

            assert r.status_code == 200
            publish.assert_not_called()  # still inside the transaction's lifetime

            for callback in callbacks:
                callback()
            publish.assert_called_once()


@pytest.mark.django_db
class TestMalformedSubjectRefsCannotCostUsTheWrite:
    """A producer-supplied field must never be able to roll back a case write.

    ``subject_refs`` is a writable, historically unvalidated ``JSONField``. A
    non-list value used to raise ``TypeError`` inside ``build_decision_envelope``
    — which runs *inside* the caller's ``transaction.atomic()`` — so the
    approval 500'd and the timeline write was rolled back, with no broker
    configured at all. These rows can still exist from before the serializer
    validated the field, so the publisher stays defensive too.
    """

    @override_settings(NATS_URL="")
    @pytest.mark.parametrize(
        "refs", [5, "a-string", {"a": 1}, [["nested"]], [None], [""], ["ok", 7]]
    )
    def test_approval_survives_any_shape(self, django_capture_on_commit_callbacks, refs):
        make_case()
        p = make_proposal()
        # Bypass the serializer the way a pre-validation row would have.
        CaseUpdateProposal.objects.filter(pk=p.pk).update(subject_refs=refs)
        p.refresh_from_db()

        with django_capture_on_commit_callbacks(execute=True):
            r = caseworker_client().post(f"{LIST_URL}{p.id}/approve/", {}, format="json")

        assert r.status_code == 200, f"{refs!r} broke the approval"
        p.refresh_from_db()
        assert p.status == ProposalStatus.APPROVED
        # The write that actually matters really did land.
        assert len(Case.objects.get(slug="lalita-niwas-land-scam").timeline) == 1

    def test_unusable_refs_are_dropped_not_published(self):
        p = make_proposal(status=ProposalStatus.APPROVED, subject_refs=["ok-ref", 7, None, ""])
        refs = build_decision_envelope(p)["subject_refs"]
        assert refs == ["https://jawafdehi.org/case/lalita-niwas-land-scam", "ok-ref"]

    @override_settings(NATS_URL="")
    def test_an_envelope_that_blows_up_entirely_still_leaves_the_write(
        self, django_capture_on_commit_callbacks
    ):
        """Pins the try/except independently of the ref sanitising.

        The sanitiser and the guard are two layers, so a shape-based test passes
        with either one alone. This removes the sanitiser from the picture by
        making envelope construction fail outright, which is the only thing that
        fails if the guard is dropped.
        """
        make_case()
        p = make_proposal()
        with mock.patch(
            "case_proposals.publish.build_decision_envelope",
            side_effect=RuntimeError("envelope exploded"),
        ):
            with django_capture_on_commit_callbacks(execute=True):
                r = caseworker_client().post(f"{LIST_URL}{p.id}/approve/", {}, format="json")

        assert r.status_code == 200
        p.refresh_from_db()
        assert p.status == ProposalStatus.APPROVED
        assert len(Case.objects.get(slug="lalita-niwas-land-scam").timeline) == 1

    def test_a_non_decision_status_raises_a_named_error(self):
        # build_decision_envelope is public and called directly by other code;
        # a bare KeyError('pending') told the caller nothing.
        p = make_proposal(status=ProposalStatus.PENDING)
        with pytest.raises(ValueError, match="not a.*decision"):
            build_decision_envelope(p)


@pytest.mark.django_db
class TestTheSerializerRejectsUnusableJoinKeys:
    """Stop the bad rows at the door, since a proposal cannot be repaired later.

    The viewset exposes no update and ``dedup_key`` is unique, so a proposal
    created with an unusable ``case_slug`` or ``subject_refs`` can neither be
    fixed nor re-filed.
    """

    def _post(self, **over):
        payload = dict(
            case_slug="lalita-niwas-land-scam",
            case_title="Lalita Niwas land scam",
            source_kind="ngm_docket",
            intent={
                "type": "append_timeline_entry",
                "entry": {"date": "2026-08-12", "title": "Hearing"},
            },
            confidence=0.9,
            detected_by="consumer:proposal-builder",
            dedup_key="d-1",
        )
        payload.update(over)
        return caseworker_client().post(LIST_URL, payload, format="json")

    def test_a_valid_proposal_is_still_accepted(self):
        assert self._post().status_code == 201

    @pytest.mark.parametrize("refs", [5, "a-string", {"a": 1}, [["nested"]], [""], [None]])
    def test_malformed_subject_refs_is_a_400_not_a_201(self, refs):
        r = self._post(subject_refs=refs, dedup_key=f"d-{refs}")
        assert r.status_code == 400
        assert "subject_refs" in r.data

    def test_subject_refs_may_be_omitted_or_empty(self):
        assert self._post(subject_refs=[], dedup_key="d-empty").status_code == 201

    @pytest.mark.parametrize("slug", ["lalita_niwas_scam", "2072-lalita-niwas"])
    def test_a_slug_the_iri_grammar_rejects_is_a_400(self, slug):
        # SlugField allows underscores and a leading digit; build_case_iri does
        # not. Such a proposal would publish its decision with NO join key, and
        # the reject path never resolves the Case so nothing would notice.
        r = self._post(case_slug=slug, dedup_key=f"d-{slug}")
        assert r.status_code == 400
        assert "case_slug" in r.data


@pytest.mark.django_db
class TestTheProposedAnnouncement:
    """``jaw.case.update.proposed`` — published where the row is created.

    DESIGN.md §6.3 originally had the bus's proposal-builder consumer emit this.
    It cannot: the builder acks as soon as it has enqueued an intent job, and no
    proposal exists at that moment — the row appears a minute later when the
    model answers. The consumer's message would name a proposal_id nothing could
    resolve.
    """

    def test_the_envelope_names_the_proposed_subject(self):
        from case_proposals.publish import build_proposed_envelope

        envelope = build_proposed_envelope(make_proposal())
        assert envelope["subject"] == "jaw.case.update.proposed"
        assert envelope["payload"]["status"] == ProposalStatus.PENDING

    def test_it_carries_no_reviewer_because_there_is_not_one_yet(self):
        from case_proposals.publish import build_proposed_envelope

        payload = build_proposed_envelope(make_proposal())["payload"]
        assert "reviewer" not in payload
        assert "review_notes" not in payload

    def test_its_dedup_key_is_distinct_from_the_decision_on_the_same_proposal(self):
        """Otherwise JetStream would collapse the approval into the proposal."""
        from case_proposals.publish import build_proposed_envelope

        proposal = make_proposal(status=ProposalStatus.APPROVED)
        assert (
            build_proposed_envelope(proposal)["dedup_key"]
            != build_decision_envelope(proposal)["dedup_key"]
        )

    @override_settings(NATS_URL="nats://localhost:4222")
    def test_creating_a_proposal_over_http_announces_it(self, django_capture_on_commit_callbacks):
        make_case()
        with mock.patch("case_events.bus.publish", return_value=True) as pub:
            with django_capture_on_commit_callbacks(execute=True):
                r = caseworker_client().post(
                    LIST_URL,
                    {
                        "case_slug": "lalita-niwas-land-scam",
                        "source_kind": "ngm_docket",
                        "intent": {
                            "type": "append_timeline_entry",
                            "entry": {"date": "2026-08-12", "title": "Hearing scheduled"},
                        },
                        "confidence": 0.9,
                        "detected_by": "caseworker:someone",
                        "dedup_key": "docket:x:hearing:announce",
                    },
                    format="json",
                )
        assert r.status_code == 201, r.data
        assert pub.call_args.args[0] == "jaw.case.update.proposed"

    def test_a_broker_outage_cannot_cost_us_the_proposal(self, django_capture_on_commit_callbacks):
        """Same guarantee the decision path has, asserted separately.

        The publisher for `proposed` runs inside the create transaction, so an
        unguarded failure here would 500 the create and roll the row back.
        """
        make_case()
        with mock.patch("case_events.bus.publish", side_effect=RuntimeError("broker gone")):
            with django_capture_on_commit_callbacks(execute=True):
                r = caseworker_client().post(
                    LIST_URL,
                    {
                        "case_slug": "lalita-niwas-land-scam",
                        "source_kind": "ngm_docket",
                        "intent": {
                            "type": "append_timeline_entry",
                            "entry": {"date": "2026-08-12", "title": "Hearing scheduled"},
                        },
                        "confidence": 0.9,
                        "detected_by": "caseworker:someone",
                        "dedup_key": "docket:x:hearing:outage",
                    },
                    format="json",
                )
        assert r.status_code == 201, r.data
        assert CaseUpdateProposal.objects.filter(dedup_key="docket:x:hearing:outage").exists()

    def test_an_unbuildable_envelope_still_leaves_the_proposal(self):
        from case_proposals.publish import schedule_proposed_event

        proposal = make_proposal()
        with mock.patch(
            "case_proposals.publish.build_proposed_envelope",
            side_effect=RuntimeError("boom"),
        ):
            schedule_proposed_event(proposal)  # must not raise
        assert CaseUpdateProposal.objects.filter(pk=proposal.pk).exists()
