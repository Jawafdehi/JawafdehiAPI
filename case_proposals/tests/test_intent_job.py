# SPDX-License-Identifier: Hippocratic-3.0
"""The ``case_proposal_intent`` job kind.

Two properties carry the weight here.

**Nothing a model says reaches a Case.** It reaches a PENDING proposal, through
the same serializer the HTTP create path uses, and only if it passes a closed
vocabulary and a confidence floor. Most of these tests are that boundary.

**A rejected answer is never silent.** ``jobs.queue.finalize`` swallows whatever
``on_result`` raises and leaves the job DONE, so a hook that raised on bad input
would produce a green job, no proposal, and nothing to explain the gap. Every
rejection path is asserted to record itself on ``job.result`` instead.
"""

from unittest import mock

import pytest
from django.test import override_settings

from case_proposals import job_kind
from case_proposals.job_kind import BadIntentPayload, build_payload, on_result
from case_proposals.models import CaseUpdateProposal, ProposalStatus
from cases.models import Case, CaseType
from jawafdehi_shared.entities.ids import build_courtcase_iri
from jobs.models import Job

pytestmark = pytest.mark.django_db


def make_case(**kwargs):
    defaults = {
        "title": "Lalita Niwas land scam",
        "case_type": CaseType.CORRUPTION,
        "slug": "lalita-niwas-land-scam",
        "description": "A land transfer case.",
        "timeline": [{"date": "2026-01-01", "title": "Charge sheet filed"}],
    }
    return Case.objects.create(**{**defaults, **kwargs})


def make_job(case, **payload_overrides):
    payload = {
        "case_id": case.pk,
        "observation": {"kind": "hearing", "date": "2026-03-14"},
        "dedup_key": "docket:abc:hearing:2082-12-01",
        "source_kind": "ngm_docket",
        "source": "https://example.test/docket",
        "origin_subject": "jaw.case.matched",
        "origin_msg_id": "m-1",
        "subject_refs": [],
        **payload_overrides,
    }
    job = Job.objects.create(kind=job_kind.KIND, payload=payload, status=Job.RUNNING)
    # build_payload's output is merged into the payload before the worker runs;
    # do the same here so on_result sees what it sees in production.
    job.payload = {**payload, **build_payload(job)}
    job.save(update_fields=["payload"])
    return job


def good_result(**overrides):
    return {
        "intent": {
            "type": "append_timeline_entry",
            "entry": {"date": "2026-03-14", "title": "Special Court records hearing"},
        },
        "confidence": 0.8,
        "rationale": "Not present in the timeline.",
        **overrides,
    }


class TestBuildPayload:
    def test_resolves_the_case_snapshot_so_the_worker_needs_no_database(self):
        case = make_case()
        job = Job.objects.create(kind=job_kind.KIND, payload={"case_id": case.pk, "observation": {"a": 1}})
        out = build_payload(job)
        assert out["case"]["slug"] == case.slug
        assert out["case"]["title"] == case.title
        assert out["case"]["timeline"] == case.timeline

    @pytest.mark.parametrize(
        "payload,missing",
        [
            ({"observation": {"a": 1}}, "case_id"),
            ({"case_id": 1}, "observation"),
        ],
    )
    def test_an_unusable_payload_fails_before_any_model_call(self, payload, missing):
        job = Job.objects.create(kind=job_kind.KIND, payload=payload)
        with pytest.raises(BadIntentPayload, match=missing):
            build_payload(job)

    def test_a_missing_case_is_named_rather_than_raising_does_not_exist(self):
        job = Job.objects.create(kind=job_kind.KIND, payload={"case_id": 999999, "observation": {"a": 1}})
        with pytest.raises(BadIntentPayload, match="No case with id 999999"):
            build_payload(job)

    def test_a_devanagari_case_asks_for_a_nepali_entry(self):
        case = make_case(title="विशेष अदालत मुद्दा", slug="bishesh-adalat")
        job = Job.objects.create(kind=job_kind.KIND, payload={"case_id": case.pk, "observation": {"a": 1}})
        assert build_payload(job)["language"] == "np"

    def test_an_english_case_asks_for_an_english_entry(self):
        case = make_case()
        job = Job.objects.create(kind=job_kind.KIND, payload={"case_id": case.pk, "observation": {"a": 1}})
        assert build_payload(job)["language"] == "en"

    def test_a_long_timeline_keeps_the_newest_entries_and_says_how_many_it_dropped(self):
        """The tail is what a fresh observation could duplicate.

        Trimming the head would leave the model unable to see the entry it is
        about to propose again — the one failure mode the prompt exists to avoid.
        """
        entries = [{"date": f"2026-01-{i:02d}", "title": f"Entry {i}"} for i in range(1, 100)]
        case = make_case(timeline=entries)
        job = Job.objects.create(kind=job_kind.KIND, payload={"case_id": case.pk, "observation": {"a": 1}})
        snapshot = build_payload(job)["case"]

        assert len(snapshot["timeline"]) == job_kind.MAX_TIMELINE_ENTRIES
        assert snapshot["timeline"][-1] == entries[-1]
        assert snapshot["timeline_entries_omitted"] == 99 - job_kind.MAX_TIMELINE_ENTRIES

    def test_a_truncated_description_is_flagged_not_silently_shortened(self):
        case = make_case(description="x" * (job_kind.MAX_DESCRIPTION_CHARS + 50))
        job = Job.objects.create(kind=job_kind.KIND, payload={"case_id": case.pk, "observation": {"a": 1}})
        snapshot = build_payload(job)["case"]
        assert snapshot["description_truncated"] is True
        assert len(snapshot["description"]) == job_kind.MAX_DESCRIPTION_CHARS


class TestStagingAProposal:
    def test_a_good_answer_becomes_a_pending_proposal(self):
        case = make_case()
        job = make_job(case)
        on_result(job, good_result())

        proposal = CaseUpdateProposal.objects.get()
        assert proposal.status == ProposalStatus.PENDING
        assert proposal.case_slug == case.slug
        assert proposal.confidence == 0.8
        assert proposal.intent["type"] == "append_timeline_entry"
        assert proposal.detected_by == job_kind.DETECTED_BY
        assert proposal.dedup_key == "docket:abc:hearing:2082-12-01"
        assert proposal.origin_subject == "jaw.case.matched"

    def test_a_REALISTIC_docket_key_stages_rather_than_failing_validation(self):
        """The keys in the other tests here are short, and that hid a real bug.

        Production keys are not ``docket:abc:...``. A docket key embeds a full
        court-case IRI and the matched key appends the case slug, which comes to
        108 characters for an ordinary case — over the 100 that ``origin_msg_id``
        used to allow. Every docket-derived proposal therefore failed serializer
        validation, was filed as "the model produced something unusable", and
        left no row behind, so the duplicate check found nothing and the next
        scrape bought another premium call to fail identically.

        Built from the real IRI helper rather than a literal, so the day the IRI
        grammar or the base host gets longer, this fails here instead of in a
        cron nobody is watching.
        """
        case = make_case()
        iri = build_courtcase_iri("special", "082-CR-0154")
        matched_key = f"matched:docket:{iri}:hearing:2082-11-20:{case.slug}"
        assert len(matched_key) > 100, "the fixture stopped exercising the overflow"

        job = make_job(case, dedup_key=matched_key, origin_msg_id=matched_key)
        on_result(job, good_result())

        proposal = CaseUpdateProposal.objects.get()
        assert proposal.origin_msg_id == matched_key
        # Not truncated: the value's whole job is to lead back to the message
        # that caused the proposal, and a clipped key leads nowhere.
        assert job.result["staged"]["proposal_id"] == proposal.pk

    def test_the_staged_proposal_is_recorded_on_the_job(self):
        case = make_case()
        job = make_job(case)
        on_result(job, good_result())
        job.refresh_from_db()
        assert job.result["staged"]["proposal_id"] == CaseUpdateProposal.objects.get().pk

    def test_the_slug_comes_from_the_snapshot_not_the_enqueuer(self):
        """A case re-slugged between the observation and the claim must still land.

        build_payload resolves by pk, so the snapshot carries the CURRENT slug;
        the enqueuer's stale one would fail to resolve at approve time.
        """
        case = make_case()
        job = make_job(case, case_slug="a-stale-slug")
        on_result(job, good_result())
        assert CaseUpdateProposal.objects.get().case_slug == case.slug

    def test_a_staged_proposal_is_announced_so_the_notifier_can_see_it(self):
        """The bus's proposal-builder cannot announce this — the row does not

        exist when it acks. So the announcement happens here, where it does.
        """
        with mock.patch("case_proposals.publish.schedule_proposed_event") as announce:
            on_result(make_job(make_case()), good_result())
        announce.assert_called_once()
        assert announce.call_args.args[0].pk == CaseUpdateProposal.objects.get().pk

    def test_a_refused_answer_announces_nothing(self):
        with mock.patch("case_proposals.publish.schedule_proposed_event") as announce:
            on_result(make_job(make_case()), good_result(confidence=0.1))
        announce.assert_not_called()

    def test_a_failure_to_announce_loses_neither_the_proposal_nor_its_record(self):
        """An escape here would skip the bookkeeping below it.

        The proposal would exist while its job showed no sign of having staged
        one — the exact silent gap the recording is for.
        """
        job = make_job(make_case())
        with mock.patch(
            "case_proposals.publish.schedule_proposed_event",
            side_effect=RuntimeError("bus module broken"),
        ):
            on_result(job, good_result())

        proposal = CaseUpdateProposal.objects.get()
        job.refresh_from_db()
        assert job.result["staged"]["proposal_id"] == proposal.pk

    def test_nothing_is_written_to_the_case_itself(self):
        case = make_case()
        before = list(case.timeline)
        on_result(make_job(case), good_result())
        case.refresh_from_db()
        assert case.timeline == before


class TestWhatAModelIsNotAllowedToDraft:
    """The closed vocabulary, the confidence floor, and the shape gate."""

    def test_a_decline_stages_nothing_and_says_it_declined(self):
        job = make_job(make_case())
        on_result(job, {"intent": None, "rationale": "Already in the timeline."})

        assert not CaseUpdateProposal.objects.exists()
        job.refresh_from_db()
        assert job.result["staged"]["declined"] is True
        assert job.result["staged"]["rationale"] == "Already in the timeline."

    def test_a_raw_patch_is_refused_even_though_the_system_accepts_one(self):
        """``raw_patch`` is a valid intent type — just not one a model may write."""
        job = make_job(make_case())
        on_result(
            job,
            good_result(intent={"type": "raw_patch", "patch": [{"op": "replace", "path": "/notes", "value": "x"}]}),
        )

        assert not CaseUpdateProposal.objects.exists()
        job.refresh_from_db()
        assert "not draftable by a model" in job.result["staged"]["rejected"]

    def test_raw_patch_is_still_a_supported_intent_type(self):
        """Guards the test above from passing for the wrong reason.

        If ``raw_patch`` were dropped from the system vocabulary entirely, the
        refusal test would still pass while proving something much weaker.
        """
        from case_proposals.models import SUPPORTED_INTENT_TYPES

        assert "raw_patch" in SUPPORTED_INTENT_TYPES
        assert "raw_patch" not in job_kind.MODEL_INTENT_TYPES

    @pytest.mark.parametrize(
        "intent",
        [
            {"type": "delete_case"},
            {"type": None},
            {"no_type": True},
            "append_timeline_entry",
            [],
        ],
    )
    def test_an_unknown_or_malformed_intent_stages_nothing(self, intent):
        job = make_job(make_case())
        on_result(job, good_result(intent=intent))
        assert not CaseUpdateProposal.objects.exists()
        job.refresh_from_db()
        assert job.result["staged"]["rejected"]

    def test_a_low_confidence_draft_is_dropped(self):
        job = make_job(make_case())
        on_result(job, good_result(confidence=0.2))
        assert not CaseUpdateProposal.objects.exists()
        job.refresh_from_db()
        assert job.result["staged"]["below_threshold"] is True

    def test_a_draft_exactly_at_the_threshold_is_kept(self):
        job = make_job(make_case())
        on_result(job, good_result(confidence=job_kind.MIN_CONFIDENCE))
        assert CaseUpdateProposal.objects.count() == 1

    @override_settings(CASE_PROPOSAL_MIN_CONFIDENCE=0.95)
    def test_the_threshold_can_be_tightened_without_a_deploy(self):
        job = make_job(make_case())
        on_result(job, good_result(confidence=0.8))
        assert not CaseUpdateProposal.objects.exists()

    @pytest.mark.parametrize(
        "confidence",
        [
            None,
            "high",
            float("nan"),
            1.5,
            -0.1,
            # A JSON number has no size limit, so `json.loads` hands us a Python
            # int this large and `float()` on it raises OverflowError — which is
            # an ArithmeticError, not a TypeError or ValueError. Given an explicit
            # id because the default one would be 401 digits wide.
            pytest.param(10**400, id="too-big-for-a-float"),
            # The float spelling is NOT the same case: JSON `1e400` parses to
            # float("inf"), converts without complaint, and is stopped by the
            # range check instead. Here so a fix aimed at one cannot regress the
            # other.
            pytest.param(float("inf"), id="inf"),
        ],
    )
    def test_a_confidence_that_is_not_a_number_in_range_stages_nothing(self, confidence):
        job = make_job(make_case())
        on_result(job, good_result(confidence=confidence))
        assert not CaseUpdateProposal.objects.exists()

    def test_an_intent_the_proposal_serializer_rejects_stages_nothing(self):
        """The model may not stage what a caseworker could not have posted."""
        job = make_job(make_case())
        # append_timeline_entry without entry.title — rejected by validate_intent_shape.
        on_result(job, good_result(intent={"type": "append_timeline_entry", "entry": {"date": "2026-03-14"}}))

        assert not CaseUpdateProposal.objects.exists()
        job.refresh_from_db()
        assert "failed proposal validation" in job.result["staged"]["rejected"]

    def test_a_result_that_is_not_an_object_stages_nothing(self):
        job = make_job(make_case())
        on_result(job, ["not", "a", "dict"])
        assert not CaseUpdateProposal.objects.exists()

    def test_a_payload_with_no_dedup_key_stages_nothing(self):
        """Without one the idempotency spine is gone and the fact re-proposes forever."""
        job = make_job(make_case(), dedup_key="")
        on_result(job, good_result())
        assert not CaseUpdateProposal.objects.exists()
        job.refresh_from_db()
        assert "dedup_key" in job.result["staged"]["rejected"]


class TestIdempotency:
    def test_the_same_fact_twice_stages_one_proposal(self):
        case = make_case()
        on_result(make_job(case), good_result())
        second = make_job(case)
        on_result(second, good_result())

        assert CaseUpdateProposal.objects.count() == 1
        second.refresh_from_db()
        assert second.result["staged"]["duplicate"] is True

    def test_a_rejected_proposal_stays_rejected_when_the_fact_recurs(self):
        """The rejection is sticky — that is what dedup_key is for."""
        case = make_case()
        on_result(make_job(case), good_result())
        proposal = CaseUpdateProposal.objects.get()
        proposal.status = ProposalStatus.REJECTED
        proposal.save(update_fields=["status"])

        on_result(make_job(case), good_result())

        assert CaseUpdateProposal.objects.count() == 1
        assert CaseUpdateProposal.objects.get().status == ProposalStatus.REJECTED


class TestNoRejectionIsSilent:
    """``finalize`` swallows hook exceptions, so a raise here would vanish."""

    @pytest.mark.parametrize(
        "result",
        [
            {"intent": None},
            {"intent": {"type": "raw_patch", "patch": [{"op": "add", "path": "/notes", "value": "x"}]}, "confidence": 0.9},
            {"intent": {"type": "append_timeline_entry", "entry": {}}, "confidence": 0.9},
            {"intent": {"type": "append_timeline_entry", "entry": {"date": "2026-03-14", "title": "t"}}},
            ["nope"],
            # A confidence too large to be a float. This is the case this class
            # exists for: `float()` raises OverflowError, which is neither a
            # TypeError nor a ValueError, so before the fix it escaped on_result
            # entirely — and `finalize` would then have swallowed it, leaving a
            # DONE job with no proposal and nothing recorded.
            pytest.param(
                {
                    "intent": {"type": "append_timeline_entry", "entry": {"date": "2026-03-14", "title": "t"}},
                    "confidence": 10**400,
                },
                id="confidence-too-big-for-a-float",
            ),
        ],
    )
    def test_every_path_that_stages_nothing_leaves_a_reason_on_the_job(self, result):
        job = make_job(make_case())
        on_result(job, result)
        job.refresh_from_db()

        staged = job.result.get("staged")
        assert staged is not None, "a rejected answer left no trace on the job"
        assert staged.get("proposal_id") is None
        assert any(
            staged.get(k) for k in ("rejected", "declined", "below_threshold", "duplicate")
        ), f"no reason recorded: {staged}"

    def test_on_result_never_raises_even_on_nonsense(self):
        job = make_job(make_case())
        for nonsense in (None, 42, "", {"intent": {"type": "append_timeline_entry"}}):
            on_result(job, nonsense)  # must not raise

    def test_a_failure_to_record_does_not_mask_the_outcome(self):
        """Bookkeeping is best-effort; the proposal is not."""
        job = make_job(make_case())
        with mock.patch.object(Job, "save", side_effect=RuntimeError("db gone")):
            on_result(job, good_result())
        assert CaseUpdateProposal.objects.count() == 1


class TestRegistration:
    def test_the_kind_is_registered_with_its_hooks(self):
        from jobs import registry

        spec = registry.get(job_kind.KIND)
        assert spec.build_payload is job_kind.build_payload
        assert spec.on_result is job_kind.on_result
        assert spec.on_failure is job_kind.on_failure

    def test_the_poller_has_a_handler_for_it(self):
        """A registered kind with no worker-side handler is a job nothing runs."""
        from review.management.commands.review_poller import HANDLERS

        assert job_kind.KIND in HANDLERS

    def test_on_failure_does_not_raise(self):
        job = make_job(make_case())
        job.status = Job.DEAD
        job.error = "provider timeout"
        job_kind.on_failure(job)
