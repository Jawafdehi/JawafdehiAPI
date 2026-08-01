# SPDX-License-Identifier: Hippocratic-3.0
"""The producers: docket change-detect, and the Material post_save signal.

Two properties are load-bearing and everything else is detail.

**Dedup keys are deterministic and derived from the FACT.** Producers re-emit an
overlapping window by design, so a key built from a row pk, a timestamp or
anything else that moves would turn every rescan into a fresh proposal. Several
tests here re-run a scan and assert the keys are identical.

**Subject refs carry a join key the matcher can actually use.** A signal with no
court-case IRI is a message about nothing — it will match no case, and the
failure is silent because "no match" is a normal outcome.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone as dt_timezone
from unittest import mock

import pytest
from django.utils import timezone

from case_events import subjects
from case_events.producers import dockets, materials as material_producer
from jawafdehi_shared.entities.ids import build_courtcase_iri

pytestmark = [pytest.mark.django_db(databases="__all__")]


def make_court(identifier="special"):
    from courts.models import Court

    return Court.objects.create(
        identifier=identifier,
        court_type="special",
        full_name_nepali="विशेष अदालत",
        full_name_english="Special Court",
    )


def make_case(court, case_number="082-CR-0154", **kwargs):
    from courts.models import CourtCase

    return CourtCase.objects.create(case_number=case_number, court=court, **kwargs)


def make_hearing(court, case_number="082-CR-0154", **kwargs):
    from courts.models import CourtCaseHearing

    defaults = {
        "hearing_date_bs": "2082-12-01",
        "hearing_date_ad": date(2026, 3, 14),
        "scraped_at": datetime(2026, 3, 14, tzinfo=dt_timezone.utc),
    }
    return CourtCaseHearing.objects.create(
        case_number=case_number, court=court, **{**defaults, **kwargs}
    )


class TestHearingSignals:
    def test_a_new_hearing_becomes_a_signal_joined_to_its_court_case(self):
        court = make_court()
        make_hearing(court)

        (subject, payload, refs, dedup_key, occurred_at), = list(
            dockets.hearing_signals(timezone.now() - timedelta(hours=1))
        )

        assert subject == subjects.SIGNAL_DOCKET_HEARING_ADDED
        assert refs == [build_courtcase_iri("special", "082-CR-0154")]
        assert payload["hearing_date_bs"] == "2082-12-01"
        assert occurred_at == date(2026, 3, 14)

    def test_the_join_key_is_built_by_the_shared_helper_not_by_hand(self):
        """``build_courtcase_iri`` LOWERCASES both segments.

        A hand-formatted `.../courtcase/special/082-CR-0154` matches nothing the
        matcher holds, and the failure is a silent no-match.
        """
        court = make_court(identifier="Special")
        make_hearing(court, case_number="082-CR-0154")

        (_, _, refs, _, _), = list(dockets.hearing_signals(timezone.now() - timedelta(hours=1)))
        # Both segments lowercased, despite the court id and case number being
        # mixed case on the row.
        assert refs[0].endswith("/courtcase/special/082-cr-0154")
        assert refs[0] == build_courtcase_iri("Special", "082-CR-0154")

    def test_the_dedup_key_is_the_docket_date_not_our_row_id(self):
        """A re-imported hearing gets a new pk; the fact is the same fact."""
        court = make_court()
        hearing = make_hearing(court)
        first = list(dockets.hearing_signals(timezone.now() - timedelta(hours=1)))[0][3]

        hearing.delete()
        make_hearing(court)
        second = list(dockets.hearing_signals(timezone.now() - timedelta(hours=1)))[0][3]

        assert first == second
        assert "2082-12-01" in first

    def test_rescanning_the_same_window_produces_identical_keys(self):
        """The whole basis of the stateless design."""
        court = make_court()
        make_hearing(court)
        since = timezone.now() - timedelta(hours=1)

        assert [s[3] for s in dockets.hearing_signals(since)] == [
            s[3] for s in dockets.hearing_signals(since)
        ]

    def test_a_hearing_outside_the_window_is_not_emitted(self):
        court = make_court()
        make_hearing(court)
        from courts.models import CourtCaseHearing

        CourtCaseHearing.objects.update(created_at=timezone.now() - timedelta(days=10))

        assert list(dockets.hearing_signals(timezone.now() - timedelta(hours=1))) == []

    def test_a_future_hearing_is_news_now_not_on_the_day(self):
        """Keyed on created_at, so a hearing scheduled for next month emits today."""
        court = make_court()
        make_hearing(court, hearing_date_ad=date(2027, 1, 1), hearing_date_bs="2083-09-17")

        assert len(list(dockets.hearing_signals(timezone.now() - timedelta(hours=1)))) == 1

    def test_the_limit_is_respected(self):
        court = make_court()
        for i in range(5):
            make_hearing(court, hearing_date_bs=f"2082-12-{i:02d}")

        assert len(list(dockets.hearing_signals(timezone.now() - timedelta(hours=1), limit=2))) == 2


class TestVerdictSignals:
    def test_a_decided_case_becomes_a_verdict_signal(self):
        court = make_court()
        make_case(
            court,
            verdict_type="सफाई",
            verdict_date_bs="2082-11-20",
            verdict_date_ad=date(2026, 3, 4),
        )

        (subject, payload, refs, dedup_key, occurred_at), = list(
            dockets.verdict_signals(timezone.now() - timedelta(hours=1))
        )

        assert subject == subjects.SIGNAL_DOCKET_VERDICT_ENTERED
        assert payload["verdict_type"] == "सफाई"
        assert refs == [build_courtcase_iri("special", "082-CR-0154")]
        assert "2082-11-20" in dedup_key
        assert occurred_at == date(2026, 3, 4)

    def test_a_case_with_no_verdict_is_not_emitted(self):
        make_case(make_court(), case_status="चालु")
        assert list(dockets.verdict_signals(timezone.now() - timedelta(hours=1))) == []

    def test_a_verdict_date_with_no_verdict_type_is_not_emitted(self):
        """Half a verdict is a scrape artefact, not a decision."""
        make_case(make_court(), verdict_date_ad=date(2026, 3, 4), verdict_type="")
        assert list(dockets.verdict_signals(timezone.now() - timedelta(hours=1))) == []

    def test_a_soft_deleted_case_is_not_emitted(self):
        make_case(
            make_court(),
            verdict_type="सफाई",
            verdict_date_ad=date(2026, 3, 4),
            is_deleted=True,
        )
        assert list(dockets.verdict_signals(timezone.now() - timedelta(hours=1))) == []

    def test_a_corrected_judge_re_emits_under_the_same_key(self):
        """Keyed on the verdict DATE, so a correction reaches a caseworker again."""
        court = make_court()
        case = make_case(
            court,
            verdict_type="सफाई",
            verdict_date_bs="2082-11-20",
            verdict_date_ad=date(2026, 3, 4),
            verdict_judge="A",
        )
        since = timezone.now() - timedelta(hours=1)
        first = list(dockets.verdict_signals(since))[0]

        case.verdict_judge = "A, B"
        case.save()
        second = list(dockets.verdict_signals(since))[0]

        assert first[3] == second[3]
        assert second[1]["verdict_judge"] == "A, B"

    def test_a_different_verdict_date_is_a_different_fact(self):
        court = make_court()
        case = make_case(
            court, verdict_type="सफाई", verdict_date_bs="2082-11-20", verdict_date_ad=date(2026, 3, 4)
        )
        since = timezone.now() - timedelta(hours=1)
        first = list(dockets.verdict_signals(since))[0][3]

        case.verdict_date_bs = "2082-11-25"
        case.save()
        assert list(dockets.verdict_signals(since))[0][3] != first


class TestPublishWindow:
    def test_it_emits_everything_in_the_window_and_counts_it(self):
        court = make_court()
        make_hearing(court)
        make_case(
            court,
            case_number="082-CR-0999",
            verdict_type="सफाई",
            verdict_date_bs="2082-11-20",
            verdict_date_ad=date(2026, 3, 4),
        )

        with mock.patch("case_events.bus.publish", return_value=True) as pub:
            counts = dockets.publish_window(window_hours=24)

        assert counts[subjects.SIGNAL_DOCKET_HEARING_ADDED] == 1
        assert counts[subjects.SIGNAL_DOCKET_VERDICT_ENTERED] == 1
        assert pub.call_count == 2

    def test_a_signal_the_bus_refused_is_counted_separately(self):
        """A run that published nothing must not read as an empty window."""
        court = make_court()
        make_hearing(court)

        with mock.patch("case_events.bus.publish", return_value=False):
            counts = dockets.publish_window(window_hours=24)

        assert any("not sent" in key for key in counts)

    def test_a_broker_failure_does_not_stop_the_scan(self):
        court = make_court()
        make_hearing(court)
        make_hearing(court, hearing_date_bs="2082-12-02")

        with mock.patch("case_events.bus.publish", side_effect=RuntimeError("broker gone")):
            counts = dockets.publish_window(window_hours=24)

        assert sum(counts.values()) == 2

    def test_occurred_at_is_the_court_date_not_the_scrape_time(self):
        """The number an audit of enrichment lag needs."""
        court = make_court()
        make_hearing(court)

        with mock.patch("case_events.bus.publish", return_value=True) as pub:
            dockets.publish_window(window_hours=24)

        assert pub.call_args.args[1]["occurred_at"].startswith("2026-03-14")

    def test_a_signal_with_no_dedup_key_is_refused_rather_than_published(self):
        from case_events import producers

        with mock.patch("case_events.bus.publish") as pub:
            assert producers.emit("jaw.signal.x", producer="p", payload={}, subject_refs=[], dedup_key="") is False
        pub.assert_not_called()


class TestMaterialProducer:
    """One post_save covers court orders and CIAA press releases."""

    def material(self, source, iri="https://jawafdehi.org/material/court_order/abc", data=None):
        from materials.models import Material

        return Material(
            iri=iri,
            material_type="CourtOrder",
            source=source,
            ident="abc",
            data=data if data is not None else {"@id": iri, "name": "An order"},
        )

    def test_a_court_order_raises_the_court_order_signal(self):
        signal = material_producer.signal_for(self.material("court_order"))
        assert signal is not None
        assert signal[0] == subjects.SIGNAL_COURTORDER_PUBLISHED

    def test_a_ciaa_press_release_raises_its_own_signal(self):
        signal = material_producer.signal_for(self.material("ciaa_press_release"))
        assert signal[0] == subjects.SIGNAL_CIAA_PRESSRELEASE

    def test_the_two_sources_match_the_constants_the_shapers_actually_use(self):
        """A drifted string here means the producer silently never fires."""
        from materials.jsonld import COURT_ORDER_SOURCE
        from materials.sourcing.ciaa.shaper import CIAA_PRESS_SOURCE

        assert COURT_ORDER_SOURCE in material_producer.SOURCE_SUBJECTS
        assert CIAA_PRESS_SOURCE in material_producer.SOURCE_SUBJECTS

    def test_an_unremarkable_material_raises_nothing(self):
        """A Material appearing is not by itself news about a case."""
        assert material_producer.signal_for(self.material("nkp")) is None

    def test_a_court_orders_case_iri_is_carried_as_the_join_key(self):
        """Without isPartOf the signal names a document and nothing else."""
        case_iri = build_courtcase_iri("special", "082-CR-0154")
        m = self.material(
            "court_order",
            data={"@id": "https://jawafdehi.org/material/court_order/abc", "isPartOf": {"@id": case_iri}},
        )
        _, payload, refs, _ = material_producer.signal_for(m)

        assert payload["part_of"] == case_iri
        assert case_iri in refs

    def test_a_string_ispartof_is_accepted_too(self):
        case_iri = build_courtcase_iri("special", "082-CR-0154")
        m = self.material("court_order", data={"isPartOf": case_iri})
        assert material_producer.signal_for(m)[1]["part_of"] == case_iri

    def test_a_press_release_with_no_case_still_emits(self):
        """CIAA releases generally name no docket; that is not a broken signal."""
        _, payload, refs, _ = material_producer.signal_for(self.material("ciaa_press_release"))
        assert payload["part_of"] == ""
        assert refs  # its own IRI is still a ref

    def test_the_dedup_key_is_the_material_iri(self):
        m = self.material("court_order")
        assert material_producer.signal_for(m)[3] == f"material:{m.iri}"

    def test_a_material_created_soft_deleted_is_not_an_archival_event(self):
        m = self.material("court_order")
        m.is_deleted = True
        assert material_producer.signal_for(m) is None

    def test_malformed_data_does_not_raise(self):
        for bad in (None, [], "a string", 7):
            m = self.material("court_order", data=bad)
            assert material_producer.signal_for(m) is not None


class TestTheMaterialReceiverFiresOnlyOnCreate:
    """A Material is re-saved for conversion, visibility and index churn.

    These use a REAL Material, not a Mock. An earlier version passed a
    ``mock.Mock``, and a mutation removing the ``created`` check survived it —
    because ``getattr(mock, "is_deleted", False)`` returns a truthy Mock, so
    ``signal_for`` bailed out for the wrong reason and the test proved nothing.
    """

    def unsaved_order(self):
        from materials.models import Material

        return Material(
            iri="https://jawafdehi.org/material/court_order/receiver-1",
            material_type="CourtOrder",
            source="court_order",
            ident="receiver-1",
            data={},
        )

    def _callbacks(self, sender, created):
        """The on_commit callbacks the receiver registers for one call."""
        with django_capture_on_commit_callbacks_noexec(using="ngm") as callbacks:
            material_producer.emit_material_signal(
                sender, instance=self.unsaved_order(), created=created
            )
        return callbacks

    def test_an_update_registers_nothing(self):
        from materials.models import Material

        assert self._callbacks(Material, created=False) == []

    def test_a_create_registers_the_announcement(self):
        """Paired with the test above so neither can pass for the wrong reason."""
        from materials.models import Material

        assert len(self._callbacks(Material, created=True)) == 1

    def test_a_save_of_another_model_registers_nothing(self):
        from cases.models import Case

        assert self._callbacks(Case, created=True) == []


class TestTheMaterialProducerIsActuallyConnected:
    """Everything above tests the logic. This tests that it RUNS.

    A ``post_save`` receiver that is never connected — an app whose ``ready``
    does not import it, a ``dispatch_uid`` collision, an import error swallowed
    by the guard — is invisible: every unit test passes and no signal is ever
    emitted in production. So this saves a real Material through the real ORM.
    """

    def test_the_app_config_is_what_connects_it(self):
        """A source-level guard, because the runtime check below cannot catch this.

        The obvious test — save a Material, assert a signal — passes even with
        ``EventsConfig.ready`` gutted, because THIS TEST MODULE imports
        ``case_events.producers.materials`` at the top and the ``@receiver``
        decorator connects on import. The test file wires up the thing it is
        checking. A mutation removing the import from ``ready()`` survived the
        whole suite.

        In production nothing imports that module, so ``ready()`` is the only
        thing that connects the receiver and its absence means no signal is ever
        emitted, silently. Same shape as the ``ensure_streams`` guard in
        test_nats_bootstrap.
        """
        import inspect

        from case_events.apps import EventsConfig

        source = inspect.getsource(EventsConfig.ready)
        assert "case_events.producers" in source and "materials" in source, (
            "EventsConfig.ready must import case_events.producers.materials — it is "
            "the only thing that connects the post_save producer in production."
        )

    def test_the_receiver_is_connected_to_post_save(self):
        from django.db.models.signals import post_save

        # Django's receivers entries are (lookup_key, receiver[, is_async]) and
        # the arity has changed across versions, so index rather than unpack.
        uids = [entry[0][0] for entry in post_save.receivers]
        assert "case_events_material_signal" in uids

    def test_saving_a_real_court_order_material_emits(self, django_capture_on_commit_callbacks):
        from materials.models import Material

        case_iri = build_courtcase_iri("special", "082-CR-0154")
        with mock.patch("case_events.bus.publish", return_value=True) as pub:
            with django_capture_on_commit_callbacks(execute=True, using="ngm"):
                Material.objects.create(
                    iri="https://jawafdehi.org/material/court_order/wired-1",
                    material_type="CourtOrder",
                    source="court_order",
                    ident="wired-1",
                    data={"isPartOf": {"@id": case_iri}, "name": "An order"},
                )

        subjects_sent = [call.args[0] for call in pub.call_args_list]
        assert subjects.SIGNAL_COURTORDER_PUBLISHED in subjects_sent

    def test_the_emit_is_deferred_until_the_row_commits(self):
        """Announcing a document before its row is durable is the trap here.

        ``Material`` lives on ``ngm``, so an unqualified ``on_commit`` would
        resolve against ``default`` — still in autocommit inside
        ``atomic(using="ngm")`` — and fire the callback immediately.
        """
        from materials.models import Material

        with mock.patch("case_events.bus.publish") as pub:
            with django_capture_on_commit_callbacks_noexec(using="ngm") as callbacks:
                Material.objects.create(
                    iri="https://jawafdehi.org/material/court_order/wired-2",
                    material_type="CourtOrder",
                    source="court_order",
                    ident="wired-2",
                    data={},
                )
            # Captured, NOT run: nothing reached the bus inside the transaction.
            pub.assert_not_called()
        assert callbacks, "the producer did not register an on_commit callback at all"

    def test_an_unremarkable_material_save_stays_silent_end_to_end(self):
        from materials.models import Material

        with mock.patch("case_events.bus.publish") as pub:
            with django_capture_on_commit_callbacks_noexec(using="ngm"):
                Material.objects.create(
                    iri="https://jawafdehi.org/material/nkp/wired-3",
                    material_type="Judgment",
                    source="nkp",
                    ident="wired-3",
                    data={},
                )
        pub.assert_not_called()


def django_capture_on_commit_callbacks_noexec(*, using):
    """``django_capture_on_commit_callbacks`` without executing, as a plain CM.

    The pytest-django fixture cannot be nested inside another `with` in the same
    test and also inspected afterwards, so use Django's own helper directly.
    """
    from django.test import TestCase

    return TestCase.captureOnCommitCallbacks(execute=False, using=using)
