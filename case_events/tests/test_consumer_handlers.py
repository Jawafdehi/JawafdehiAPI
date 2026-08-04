# SPDX-License-Identifier: Hippocratic-3.0
"""What each consumer does with a message.

The bus is stubbed throughout — these assert on the messages a handler WOULD
publish and the rows it writes, not on delivery. Delivery is
``test_consumer_runner``'s subject.

The invariant running through all of it: no handler writes a Case. The only
thing that does is a caseworker approving a proposal.
"""

from __future__ import annotations

from unittest import mock

import pytest

from case_events import subjects
from case_events.consumers import PoisonMessage, handlers
from cases.models import (
    Case,
    CaseCourtCaseReference,
    CaseMaterialReference,
    CaseState,
    CaseType,
)
from jawafdehi_shared.entities.ids import (
    build_case_iri,
    build_courtcase_iri,
    build_entity_iri,
    build_material_iri,
)

pytestmark = pytest.mark.django_db


def make_case(slug="lalita-niwas-land-scam", title="Lalita Niwas land scam", state=CaseState.IN_REVIEW):
    """A case the matcher is allowed to propose against.

    ``state`` is explicit and does NOT default to the model's own default. The
    model defaults to DRAFT, and DRAFT is deliberately not enrichable — see
    ``handlers.ENRICHABLE_STATES``. A fixture that inherited the model default
    would make every matcher test here assert against a case the matcher is
    supposed to ignore, and they would all fail for the same uninformative
    reason.
    """
    return Case.objects.create(title=title, case_type=CaseType.CORRUPTION, slug=slug, state=state)


def signal_envelope(**overrides):
    return {
        "subject": subjects.SIGNAL_DOCKET_HEARING_ADDED,
        "producer": "producer:court-scraper",
        "subject_refs": [],
        "dedup_key": "docket:abc:hearing:2082-12-01",
        "source": "https://example.test/docket",
        "raw_ref": "",
        "occurred_at": "2026-03-14T00:00:00Z",
        "payload": {"hearing_date": "2082-12-01"},
        **overrides,
    }


def published(mock_publish):
    """[(subject, envelope), ...] from a patched bus.publish."""
    return [(call.args[0], call.args[1]) for call in mock_publish.call_args_list]


class TestMatcher:
    def test_a_court_case_reference_resolves_to_its_case(self):
        case = make_case()
        iri = build_courtcase_iri("special", "082-CR-0154")
        CaseCourtCaseReference.objects.create(case=case, courtcase_iri=iri, ordinal=1)

        with mock.patch("case_events.bus.publish") as pub:
            handlers.handle_matcher(signal_envelope(subject_refs=[iri]), None)

        (subject, envelope), = published(pub)
        assert subject == subjects.CASE_MATCHED
        assert envelope["payload"]["case_slug"] == case.slug
        assert envelope["payload"]["matched_on"] == "reference_iri"
        assert envelope["payload"]["match_confidence"] == 1.0

    def test_a_linked_material_resolves_to_its_case(self):
        case = make_case()
        iri = build_material_iri("supremecourt", "order-123")
        CaseMaterialReference.objects.create(case=case, material_iri=iri, ordinal=1)

        with mock.patch("case_events.bus.publish") as pub:
            handlers.handle_matcher(signal_envelope(subject_refs=[iri]), None)

        assert published(pub)[0][1]["payload"]["case_slug"] == case.slug

    def test_a_signal_naming_the_case_outright_is_an_assertion_not_an_inference(self):
        case = make_case()
        with mock.patch("case_events.bus.publish") as pub:
            handlers.handle_matcher(signal_envelope(subject_refs=[build_case_iri(case.slug)]), None)

        payload = published(pub)[0][1]["payload"]
        assert payload["matched_on"] == "case_iri"
        assert payload["match_confidence"] == 1.0

    def test_one_message_per_matched_case_not_one_listing_several(self):
        """Keeps every downstream unit of work a single case."""
        iri = build_courtcase_iri("special", "082-CR-0154")
        for slug in ("case-one", "case-two"):
            CaseCourtCaseReference.objects.create(case=make_case(slug=slug), courtcase_iri=iri, ordinal=1)

        with mock.patch("case_events.bus.publish") as pub:
            handlers.handle_matcher(signal_envelope(subject_refs=[iri]), None)

        assert len(published(pub)) == 2
        assert {e["payload"]["case_slug"] for _, e in published(pub)} == {"case-one", "case-two"}

    def test_an_ambiguous_match_is_scored_down(self):
        iri = build_courtcase_iri("special", "082-CR-0154")
        for slug in ("case-one", "case-two", "case-three", "case-four"):
            CaseCourtCaseReference.objects.create(case=make_case(slug=slug), courtcase_iri=iri, ordinal=1)

        with mock.patch("case_events.bus.publish") as pub:
            handlers.handle_matcher(signal_envelope(subject_refs=[iri]), None)

        assert all(e["payload"]["match_confidence"] == 0.25 for _, e in published(pub))

    def test_a_ref_matching_too_many_cases_is_treated_as_meaningless(self):
        iri = build_courtcase_iri("special", "082-CR-0154")
        for i in range(handlers.MAX_MATCHES + 1):
            CaseCourtCaseReference.objects.create(case=make_case(slug=f"case-{i}"), courtcase_iri=iri, ordinal=1)

        with mock.patch("case_events.bus.publish") as pub:
            handlers.handle_matcher(signal_envelope(subject_refs=[iri]), None)

        assert published(pub) == []

    def test_no_match_publishes_nothing_and_does_not_fail(self):
        make_case()
        with mock.patch("case_events.bus.publish") as pub:
            handlers.handle_matcher(signal_envelope(subject_refs=["urn:nothing:here"]), None)
        assert published(pub) == []

    def test_a_signal_with_no_refs_is_acked_rather_than_buried(self):
        """A ref-less signal is a producer bug; a DLQ nobody reads would hide it."""
        with mock.patch("case_events.bus.publish") as pub:
            handlers.handle_matcher(signal_envelope(subject_refs=[]), None)
        assert published(pub) == []

    def test_an_entity_reference_alone_does_not_match_even_when_it_links_a_case(self):
        """One politician matches most of the archive; that is not evidence.

        The relationship row is REAL here. An earlier version of this test used
        an entity IRI no case referenced, so it passed with an entity join added
        and proved nothing.
        """
        from cases.models import CaseEntityRelationship, RelationshipType

        case = make_case()
        entity_iri = build_entity_iri("person", "some-person")
        CaseEntityRelationship.objects.create(
            case=case, nes_id=entity_iri, relationship_type=RelationshipType.ACCUSED
        )

        with mock.patch("case_events.bus.publish") as pub:
            handlers.handle_matcher(signal_envelope(subject_refs=[entity_iri]), None)

        assert published(pub) == [], "an entity reference must not be treated as a match"

    def test_the_matched_key_is_derived_from_the_signal_so_a_redelivery_collapses(self):
        """Two EQUAL envelopes, not the same object.

        Passing one object twice let a key built from ``id(envelope)`` through —
        the redelivery this guards against arrives as a fresh dict.
        """
        case = make_case()
        iri = build_courtcase_iri("special", "082-CR-0154")
        CaseCourtCaseReference.objects.create(case=case, courtcase_iri=iri, ordinal=1)

        with mock.patch("case_events.bus.publish") as pub:
            handlers.handle_matcher(signal_envelope(subject_refs=[iri]), None)
            handlers.handle_matcher(signal_envelope(subject_refs=[iri]), None)

        first, second = [e["dedup_key"] for _, e in published(pub)]
        assert first == second
        assert signal_envelope()["dedup_key"] in first
        assert case.slug in first

    def test_a_signal_with_no_dedup_key_still_gets_a_deterministic_one(self):
        case = make_case()
        iri = build_courtcase_iri("special", "082-CR-0154")
        CaseCourtCaseReference.objects.create(case=case, courtcase_iri=iri, ordinal=1)

        with mock.patch("case_events.bus.publish") as pub:
            handlers.handle_matcher(signal_envelope(subject_refs=[iri], dedup_key=""), None)
            handlers.handle_matcher(signal_envelope(subject_refs=[iri], dedup_key=""), None)

        keys = [e["dedup_key"] for _, e in published(pub)]
        assert keys[0] == keys[1] and keys[0]

    def test_the_original_signal_is_carried_forward_intact(self):
        """It is the only record of what was actually observed."""
        case = make_case()
        iri = build_courtcase_iri("special", "082-CR-0154")
        CaseCourtCaseReference.objects.create(case=case, courtcase_iri=iri, ordinal=1)

        with mock.patch("case_events.bus.publish") as pub:
            handlers.handle_matcher(signal_envelope(subject_refs=[iri]), None)

        signal = published(pub)[0][1]["payload"]["signal"]
        assert signal["subject"] == subjects.SIGNAL_DOCKET_HEARING_ADDED
        assert signal["payload"] == {"hearing_date": "2082-12-01"}
        assert signal["source"] == "https://example.test/docket"

    def test_a_publish_the_bus_refused_raises_instead_of_acking(self):
        """The one handler whose output IS a publish must not swallow one.

        `bus.publish` is best-effort everywhere else because the message merely
        describes work already done. Here the message is the only trace of the
        match; dropping it and returning cleanly acks the signal, and nothing
        downstream ever hears the fact. No retry, no DLQ, no log above DEBUG.
        """
        case = make_case()
        iri = build_courtcase_iri("special", "082-CR-0154")
        CaseCourtCaseReference.objects.create(case=case, courtcase_iri=iri, ordinal=1)

        with mock.patch("case_events.bus.publish", return_value=False):
            with pytest.raises(RuntimeError, match="could not publish"):
                handlers.handle_matcher(signal_envelope(subject_refs=[iri]), None)

    def test_it_waits_for_the_jetstream_ack(self):
        """`wait=False` returns True for a publish the broker later rejects.

        Which is exactly what happens when no stream claims the subject — the
        "forgot to run nats_bootstrap" case — so without waiting, the check
        above would pass every message on a broker that accepted none of them.
        """
        case = make_case()
        iri = build_courtcase_iri("special", "082-CR-0154")
        CaseCourtCaseReference.objects.create(case=case, courtcase_iri=iri, ordinal=1)

        with mock.patch("case_events.bus.publish", return_value=True) as pub:
            handlers.handle_matcher(signal_envelope(subject_refs=[iri]), None)

        assert pub.call_args.kwargs.get("wait") is True

    def test_a_soft_deleted_case_is_not_matched(self):
        """CLOSED is this platform's soft delete — `Case.delete()` keeps the row.

        Without the filter the deleted case still joins on its court-case
        reference, so every re-scrape of that docket buys a premium model call
        and puts a review item in front of a caseworker for a case somebody
        deliberately removed.
        """
        case = make_case()
        iri = build_courtcase_iri("special", "082-CR-0154")
        CaseCourtCaseReference.objects.create(case=case, courtcase_iri=iri, ordinal=1)
        case.delete()

        with mock.patch("case_events.bus.publish") as pub:
            handlers.handle_matcher(signal_envelope(subject_refs=[iri]), None)

        assert published(pub) == []

    def test_a_soft_deleted_case_named_outright_is_not_matched_either(self):
        """The direct-@id path is a separate query and needs the same filter."""
        case = make_case()
        case.delete()

        with mock.patch("case_events.bus.publish") as pub:
            handlers.handle_matcher(signal_envelope(subject_refs=[build_case_iri(case.slug)]), None)

        assert published(pub) == []

    def test_a_draft_case_is_NOT_matched(self):
        """Reverses what this test used to assert, and the reversal is the point.

        It previously read `test_a_draft_case_IS_matched`, on the reasoning that
        a case being built is what new facts should land on. True of a case a
        human is writing; false of this archive, where DRAFT was 2919 of 3003
        non-closed cases on 2026-08-04 and is overwhelmingly bulk-imported stubs
        with no editorial owner. Proposing against those spends a premium call
        each to fill a queue nobody is clearing.
        """
        case = make_case(state=CaseState.DRAFT)
        iri = build_courtcase_iri("special", "082-CR-0154")
        CaseCourtCaseReference.objects.create(case=case, courtcase_iri=iri, ordinal=1)

        with mock.patch("case_events.bus.publish") as pub:
            handlers.handle_matcher(signal_envelope(subject_refs=[iri]), None)

        assert published(pub) == []

    def test_a_draft_case_NAMED_OUTRIGHT_is_still_matched(self):
        """The asymmetry, and it is deliberate — inference is filtered, assertion
        is not.

        A docket join is us guessing a fact belongs to a case, and guessing onto
        ownerless DRAFT stubs is the waste ENRICHABLE_STATES exists to stop. A
        caseworker's manual note names the case, and this is the path it takes;
        filtering it would make the note endpoint silently useless on 97% of the
        archive — it answers 202 "a proposal will appear if the note warrants
        one" and none ever would.
        """
        case = make_case(state=CaseState.DRAFT)

        with mock.patch("case_events.bus.publish") as pub:
            handlers.handle_matcher(signal_envelope(subject_refs=[build_case_iri(case.slug)]), None)

        assert len(published(pub)) == 1

    def test_a_closed_case_is_refused_on_the_assertion_path_too(self):
        """The floor that has no exception. Being named by a human is not a
        reason to enrich a case somebody deleted, so the assertion path relaxes
        DRAFT and nothing else."""
        case = make_case(state=CaseState.DRAFT)
        case.delete()

        with mock.patch("case_events.bus.publish") as pub:
            handlers.handle_matcher(signal_envelope(subject_refs=[build_case_iri(case.slug)]), None)

        assert published(pub) == []

    @pytest.mark.parametrize("state", [CaseState.IN_REVIEW, CaseState.PUBLISHED])
    def test_the_two_enrichable_states_are_matched(self, state):
        """The positive half, pinned per state rather than left to the fixture's
        default — otherwise retuning that default silently drops the coverage."""
        case = make_case(state=state)
        iri = build_courtcase_iri("special", "082-CR-0154")
        CaseCourtCaseReference.objects.create(case=case, courtcase_iri=iri, ordinal=1)

        with mock.patch("case_events.bus.publish") as pub:
            handlers.handle_matcher(signal_envelope(subject_refs=[iri]), None)

        assert len(published(pub)) == 1

    def test_every_enrichable_state_is_a_real_case_state(self):
        """Guards a typo in the allowlist, which would fail open to silence: an
        unknown string matches nothing and looks exactly like a quiet window."""
        assert set(handlers.ENRICHABLE_STATES) <= set(CaseState.values)


def matched_envelope(case, **overrides):
    return {
        "subject": subjects.CASE_MATCHED,
        "producer": "consumer:matcher",
        "subject_refs": ["urn:ref"],
        "dedup_key": "matched:docket:abc:hearing:2082-12-01:" + case.slug,
        "payload": {
            "case_id": case.pk,
            "case_slug": case.slug,
            "signal": {"subject": subjects.SIGNAL_DOCKET_HEARING_ADDED, "source": "https://x.test"},
            **overrides.pop("payload", {}),
        },
        **overrides,
    }


def stage_proposal(case, envelope, **overrides):
    """The row that means "this exact fact has already been proposed"."""
    from case_proposals.models import CaseUpdateProposal

    return CaseUpdateProposal.objects.create(
        case_slug=case.slug,
        dedup_key=envelope["dedup_key"],
        source_kind="ngm_docket",
        intent={"type": "append_timeline_entry", "entry": {}},
        confidence=0.9,
        **overrides,
    )


class TestProposalBuilder:
    def test_it_enqueues_a_job_and_does_not_call_a_model(self):
        """The model call belongs to the job, not to the ack window."""
        from jobs.models import Job

        case = make_case()
        with mock.patch("llm.prompts.PromptSpec.invoke") as invoke:
            handlers.handle_proposal_builder(matched_envelope(case), None)

        invoke.assert_not_called()
        job = Job.objects.get()
        assert job.kind == "case_proposal_intent"
        assert job.payload["case_id"] == case.pk
        assert job.payload["observation"]["subject"] == subjects.SIGNAL_DOCKET_HEARING_ADDED

    def test_the_proposals_dedup_key_is_the_matched_signals(self):
        """This is what makes a rejection sticky when the fact is re-observed."""
        from jobs.models import Job

        case = make_case()
        envelope = matched_envelope(case)
        handlers.handle_proposal_builder(envelope, None)

        assert Job.objects.get().payload["dedup_key"] == envelope["dedup_key"]

    def test_a_redelivered_match_does_not_enqueue_a_second_job(self):
        from jobs.models import Job

        case = make_case()
        envelope = matched_envelope(case)
        handlers.handle_proposal_builder(envelope, None)
        handlers.handle_proposal_builder(envelope, None)

        assert Job.objects.count() == 1

    def test_a_fact_already_staged_never_becomes_a_job(self):
        """The whole point of the check: no job means no premium call.

        The queue's own dedup does NOT cover this. ``jobs.queue.enqueue`` frees a
        dedup_key the moment the prior job is terminal, so once the first job is
        `done` the next observation of the same fact enqueues cleanly, pays for a
        full model call, and is only then discarded by ``on_result``. With the
        docket producer rescanning a 48h window every 6h, that is ~8 calls per
        fact for one proposal.
        """
        from jobs.models import Job

        case = make_case()
        envelope = matched_envelope(case)
        stage_proposal(case, envelope)

        with mock.patch("llm.prompts.PromptSpec.invoke") as invoke:
            handlers.handle_proposal_builder(envelope, None)

        assert Job.objects.count() == 0
        invoke.assert_not_called()

    def test_the_skip_survives_the_first_job_reaching_a_terminal_state(self):
        """The regression this actually fixes, reproduced through the real queue.

        Asserted by driving the job to `done` — the state that frees the dedup
        key — rather than by trusting the docstring above. Without the check in
        the handler this enqueues a second job and the test fails.
        """
        from jobs.models import Job

        case = make_case()
        envelope = matched_envelope(case)
        handlers.handle_proposal_builder(envelope, None)

        job = Job.objects.get()
        job.status = "done"
        job.save(update_fields=["status"])
        stage_proposal(case, envelope)

        handlers.handle_proposal_builder(envelope, None)

        assert Job.objects.count() == 1

    def test_a_rejected_proposal_also_blocks_the_rebuild(self):
        """Rejection has to stay sticky, and cheaply. A caseworker saying no to a
        fact must not buy another call to re-propose it next window."""
        from jobs.models import Job

        case = make_case()
        envelope = matched_envelope(case)
        stage_proposal(case, envelope, status="rejected")

        handlers.handle_proposal_builder(envelope, None)

        assert Job.objects.count() == 0

    def test_the_signals_subject_becomes_the_proposals_source_kind(self):
        from jobs.models import Job

        case = make_case()
        handlers.handle_proposal_builder(matched_envelope(case), None)
        assert Job.objects.get().payload["source_kind"] == "ngm_docket"

    @pytest.mark.parametrize(
        "subject,expected",
        [
            (subjects.SIGNAL_COURTORDER_PUBLISHED, "court_order"),
            (subjects.SIGNAL_CIAA_PRESSRELEASE, "ciaa_press"),
            (subjects.SIGNAL_NEWS_MATCHED, "news"),
            (subjects.SIGNAL_MANUAL_NOTE, "caseworker"),
            ("jaw.signal.something.new", ""),
        ],
    )
    def test_every_signal_subject_maps_to_a_declared_provenance(self, subject, expected):
        assert handlers._source_kind_for(subject) == expected

    def test_every_mapped_source_kind_is_one_the_proposal_model_accepts(self):
        """A mapping the serializer rejects would fail every proposal from it."""
        from case_proposals.models import SignalSource

        valid = {choice.value for choice in SignalSource}
        assert set(handlers._SOURCE_KIND_BY_SUBJECT.values()) <= valid

    def test_every_declared_signal_subject_has_a_mapping(self):
        """Otherwise a new subject silently stages proposals with no provenance."""
        declared = {
            value
            for name, value in vars(subjects).items()
            if name.startswith("SIGNAL_") and isinstance(value, str)
        }
        assert declared == set(handlers._SOURCE_KIND_BY_SUBJECT)

    def test_the_unproduced_subjects_are_real_subjects_with_a_stated_reason(self):
        """`UNPRODUCED` is documentation, and documentation that drifts is worse
        than none — a stale entry would claim a producer is missing after someone
        built it, or name a subject that no longer exists."""
        declared = {
            value
            for name, value in vars(subjects).items()
            if name.startswith("SIGNAL_") and isinstance(value, str)
        }
        assert set(subjects.UNPRODUCED) <= declared
        for subject, reason in subjects.UNPRODUCED.items():
            assert len(reason) > 40, f"{subject} needs a reason, not a label"

    def test_a_subject_listed_as_unproduced_really_has_no_producer(self):
        """The claim `UNPRODUCED` makes, checked against the code rather than
        trusted. A subject someone wired up while leaving the note behind is
        exactly the drift this guards.

        Parsed with ``ast`` rather than grepped, and that distinction IS the test:
        a substring search matches the comment in ``producers/dockets.py`` that
        explains why ``status.changed`` cannot be emitted, and so reports a
        producer that does not exist. This failed exactly that way when first
        written. ``ast`` sees ``subjects.X`` attribute access and never sees a
        comment.
        """
        import ast
        import pathlib

        producers = pathlib.Path(subjects.__file__).parent / "producers"
        referenced = set()
        for path in producers.glob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                    if node.value.id == "subjects":
                        referenced.add(node.attr)

        for name, value in vars(subjects).items():
            if name.startswith("SIGNAL_") and value in subjects.UNPRODUCED:
                assert name not in referenced, f"{name} has a producer now; drop it from UNPRODUCED"

        # The converse, so the guard cannot pass by finding nothing at all.
        assert "SIGNAL_DOCKET_HEARING_ADDED" in referenced

    @pytest.mark.parametrize("broken", [{"case_id": None}, {"case_id": 0}])
    def test_an_envelope_with_no_case_is_poison_not_a_retry(self, broken):
        case = make_case()
        envelope = matched_envelope(case)
        envelope["payload"].update(broken)
        with pytest.raises(PoisonMessage, match="case_id"):
            handlers.handle_proposal_builder(envelope, None)

    def test_an_envelope_with_no_dedup_key_is_poison(self):
        """Without one the fact would re-propose forever."""
        case = make_case()
        with pytest.raises(PoisonMessage, match="dedup_key"):
            handlers.handle_proposal_builder(matched_envelope(case, dedup_key=""), None)


def decision_envelope(case, status="approved", **overrides):
    return {
        "subject": subjects.CASE_UPDATE_APPROVED if status == "approved" else subjects.CASE_UPDATE_REJECTED,
        "payload": {
            "proposal_id": 7,
            "case_slug": case.slug,
            "status": status,
            "confidence": 0.9,
            "reviewer": "someone",
            **overrides.pop("payload", {}),
        },
        **overrides,
    }


class TestNotifier:
    def test_it_records_the_transition(self, caplog):
        case = make_case()
        handlers.handle_notifier(decision_envelope(case), None)
        assert "caseworker_notified" in caplog.text

    def test_it_does_not_raise_on_a_sparse_envelope(self):
        handlers.handle_notifier({}, None)


class TestDerive:
    def test_an_approved_change_reindexes_the_case(self):
        case = make_case()
        with mock.patch("cases.search_index.index_now") as index:
            handlers.handle_derive(decision_envelope(case), None)
        index.assert_called_once()
        assert index.call_args.args[0].pk == case.pk

    def test_an_index_failure_propagates_so_it_is_retried(self):
        """The on_commit hook this backstops swallows failures; this must not."""
        case = make_case()
        with mock.patch("cases.search_index.index_now", side_effect=RuntimeError("opensearch down")):
            with pytest.raises(RuntimeError):
                handlers.handle_derive(decision_envelope(case), None)

    def test_a_slug_no_case_ever_had_is_poison_rather_than_five_identical_retries(self):
        make_case()
        envelope = {"payload": {"case_slug": "no-such-case-anywhere"}}
        with pytest.raises(PoisonMessage, match="to re-index"):
            handlers.handle_derive(envelope, None)

    def test_a_re_slugged_case_is_found_through_its_retired_slug(self):
        """A message retried across a rename carries the slug that was current then.

        ``Case.delete()`` is a soft delete (state -> CLOSED, row kept), so the
        real way a decision envelope's slug stops resolving is a re-slug, not a
        deletion. The retrieve path already 301s through CaseSlugHistory; doing
        the same here turns a dead letter back into completed work.
        """
        from cases.models import CaseSlugHistory

        case = make_case(slug="new-slug")
        CaseSlugHistory.objects.create(slug="old-slug", case=case)
        envelope = {"payload": {"case_slug": "old-slug"}}

        with mock.patch("cases.search_index.index_now") as index:
            handlers.handle_derive(envelope, None)

        assert index.call_args.args[0].pk == case.pk

    def test_a_live_slug_always_wins_over_a_retired_one(self):
        from cases.models import CaseSlugHistory

        live = make_case(slug="contested-slug")
        other = make_case(slug="some-other-case")
        CaseSlugHistory.objects.create(slug="contested-slug", case=other)

        with mock.patch("cases.search_index.index_now") as index:
            handlers.handle_derive({"payload": {"case_slug": "contested-slug"}}, None)

        assert index.call_args.args[0].pk == live.pk

    def test_a_soft_deleted_case_is_still_reindexed(self):
        """Closing a case is a visibility change; the index needs to hear about it."""
        case = make_case()
        case.delete()  # soft: state -> CLOSED
        with mock.patch("cases.search_index.index_now") as index:
            handlers.handle_derive(decision_envelope(case), None)
        assert index.call_args.args[0].pk == case.pk

    def test_an_envelope_with_no_case_is_poison(self):
        with pytest.raises(PoisonMessage, match="case_slug"):
            handlers.handle_derive({"payload": {}}, None)

    def test_the_statistics_snapshot_is_not_refreshed_inline(self):
        """15-19s on prod; it would eat the ack window and pile up under a burst."""
        case = make_case()
        with mock.patch("cases.services.statistics.refresh_statistics") as refresh:
            with mock.patch("cases.search_index.index_now"):
                handlers.handle_derive(decision_envelope(case), None)
        refresh.assert_not_called()


class TestNoHandlerWritesACase:
    """The invariant the whole design rests on."""

    def test_the_full_signal_to_job_path_leaves_every_case_untouched(self):
        case = make_case()
        iri = build_courtcase_iri("special", "082-CR-0154")
        CaseCourtCaseReference.objects.create(case=case, courtcase_iri=iri, ordinal=1)
        before = (case.title, list(case.timeline or []), case.state, case.updated_at)

        published_msgs = []

        def record(subject, envelope, **kwargs):
            published_msgs.append(envelope)
            # Returns True: `bus.publish` returning False now means "this did
            # not go", and the matcher raises on it. The old fake returned
            # `list.append(...)` — i.e. None — which claimed a failed publish.
            return True

        with mock.patch("case_events.bus.publish", side_effect=record):
            handlers.handle_matcher(signal_envelope(subject_refs=[iri]), None)
        for envelope in published_msgs:
            handlers.handle_proposal_builder(envelope, None)

        case.refresh_from_db()
        assert (case.title, list(case.timeline or []), case.state, case.updated_at) == before

    def test_and_stages_no_proposal_either_until_the_job_runs(self):
        from case_proposals.models import CaseUpdateProposal

        case = make_case()
        handlers.handle_proposal_builder(matched_envelope(case), None)
        assert not CaseUpdateProposal.objects.exists()
