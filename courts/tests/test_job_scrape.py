"""court_scrape job wiring: kind registration, handler, enqueuer, and the
in-process scrape_worker consumer — all against a FAKE fetch (no network).

Mirrors test_scraper_crawl.py's fake-fetcher approach; the crawl parse/write is
proven there, so these tests focus on the queue wiring: enqueue+dedup, the
handler's write + stats, and the worker's claim → run → finalize lifecycle
(including retryable vs. terminal failure classification).
"""

from io import StringIO
from unittest import mock
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from courts.models import Court, CourtCase
from courts.scraper import registry
from jobs import queue as jobs_queue
from jobs.models import Job
from jobs.registry import get as get_kind

from courts.tests.special_fixtures import fake_fetch

#: A special-court scrape sized to the fake fetch (one date, one case).
_SPECIAL_PAYLOAD = {
    "court": "special",
    "enrich": True,
    "lookback_days": 2,
    "limit_dates": 1,
    "today": "2025-04-20",
}


class _NgmTestCase(TestCase):
    databases = "__all__"


class KindRegistrationTests(_NgmTestCase):
    def test_court_scrape_kind_is_registered(self):
        spec = get_kind("court_scrape")
        self.assertEqual(spec.kind, "court_scrape")
        self.assertEqual(spec.lease_seconds, 1800)
        self.assertEqual(spec.max_attempts, 3)


class HandlerTests(_NgmTestCase):
    def test_handler_writes_and_returns_stats(self):
        from courts.job_handlers import handle_court_scrape

        result = handle_court_scrape(_SPECIAL_PAYLOAD, fetch=fake_fetch)

        self.assertEqual(result["cases"], 1)
        self.assertEqual(result["hearings"], 1)
        self.assertEqual(result["enriched"], 1)
        self.assertEqual(result["per_court"][0]["court"], "special")
        self.assertEqual(
            CourtCase.objects.using("ngm").filter(court_id="special").count(), 1
        )

    def test_handler_missing_court_raises(self):
        from courts.job_handlers import handle_court_scrape

        with self.assertRaises(ValueError):
            handle_court_scrape({}, fetch=fake_fetch)

    def test_handler_heartbeats_once_per_crawled_date(self):
        from courts.job_handlers import handle_court_scrape

        stages = []
        handle_court_scrape(_SPECIAL_PAYLOAD, on_stage=stages.append, fetch=fake_fetch)
        # on_stage fires per crawled date so the worker can extend the lease.
        self.assertTrue(stages)
        self.assertTrue(stages[0].startswith("special "))


class EnqueueTests(_NgmTestCase):
    def test_enqueue_all_posts_one_job_per_leaf_court_and_dedups(self):
        from courts.scraper import registry

        call_command("enqueue_scrape", "--court", "all")
        # one job per LEAF court_id across all tiers (district ~77, high ~18, ...)
        expected = sum(len(m.court_ids(None)) for m in registry.REGISTRY.values())
        self.assertEqual(Job.objects.filter(kind="court_scrape").count(), expected)
        # dedup key is per leaf court: <tier>:<court_id>
        self.assertTrue(
            Job.objects.filter(dedup_key="court_scrape:special:special").exists()
        )

        # Re-enqueuing while the jobs are still QUEUED is a no-op (dedup key).
        call_command("enqueue_scrape", "--court", "all")
        self.assertEqual(Job.objects.filter(kind="court_scrape").count(), expected)

    def test_enqueue_unknown_court_errors(self):
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError):
            call_command("enqueue_scrape", "--court", "bogus")


class ScrapeWorkerTests(_NgmTestCase):
    def test_readonly_does_not_claim(self):
        jobs_queue.enqueue(
            kind="court_scrape", payload=_SPECIAL_PAYLOAD,
            dedup_key="court_scrape:special",
        )
        call_command("scrape_worker")  # no --apply
        self.assertEqual(
            Job.objects.get(kind="court_scrape").status, Job.QUEUED
        )

    def test_apply_once_claims_runs_and_finalizes(self):
        jobs_queue.enqueue(
            kind="court_scrape", payload=_SPECIAL_PAYLOAD,
            dedup_key="court_scrape:special",
        )
        with patch("courts.job_handlers.Fetcher", lambda: fake_fetch):
            call_command("scrape_worker", "--apply", "--once")

        job = Job.objects.get(kind="court_scrape")
        self.assertEqual(job.status, Job.DONE)
        self.assertEqual(job.result["cases"], 1)
        self.assertEqual(
            CourtCase.objects.using("ngm").filter(court_id="special").count(), 1
        )

    def test_unknown_court_fails_terminally_not_retried(self):
        # An unknown court is a BadCourtScrapePayload (non-retryable): the worker
        # finalizes it FAILED on the first attempt rather than re-queuing.
        jobs_queue.enqueue(
            kind="court_scrape", payload={"court": "bogus"},
            dedup_key="court_scrape:bogus",
        )
        call_command("scrape_worker", "--apply", "--once")

        job = Job.objects.get(kind="court_scrape")
        self.assertEqual(job.status, Job.FAILED)
        self.assertEqual(job.attempts, 1)

    def test_max_jobs_zero_finalizes_nothing(self):
        jobs_queue.enqueue(
            kind="court_scrape", payload=_SPECIAL_PAYLOAD,
            dedup_key="court_scrape:special",
        )
        with patch("courts.job_handlers.Fetcher", lambda: fake_fetch):
            call_command("scrape_worker", "--apply", "--once", "--max-jobs", "0")
        # --max-jobs 0 is a no-op-and-exit (not falsy-unlimited): job stays QUEUED.
        self.assertEqual(Job.objects.get(kind="court_scrape").status, Job.QUEUED)


class SweepJobWiringTests(_NgmTestCase):
    """The register sweep rides the SAME ``court_scrape`` kind — no new job kind,
    no new CronJob. It is the cause-list job with its listing half switched off."""

    _SWEEP_PAYLOAD = {
        "court": "special", "court_id": "special",
        "sweep": True, "causelist": False, "sweep_budget": 5, "sweep_delay": 0,
        "today": "2025-04-20",
    }

    def test_sweep_payload_skips_the_causelist_half(self):
        from courts.job_handlers import handle_court_scrape

        result = handle_court_scrape(self._SWEEP_PAYLOAD, fetch=fake_fetch)
        self.assertEqual(result["dates"], 0)
        self.assertEqual(result["cases"], 0)
        self.assertIn("sweeps", result)

    def test_sweep_reports_what_it_deferred(self):
        from courts.job_handlers import handle_court_scrape

        # Two held dockets 20 apart leaves 19 interior gaps against a budget of 5.
        for seq in (1, 21):
            CourtCase.objects.using("ngm").create(
                court_id="special", case_number=f"076-CR-{seq:04d}"
            )
        result = handle_court_scrape(self._SWEEP_PAYLOAD, fetch=fake_fetch)
        self.assertEqual(result["swept"], 5)
        self.assertGreater(result["deferred"], 0, "a capped run must not read as complete")

    def test_cause_list_job_is_unchanged_by_default(self):
        from courts.job_handlers import handle_court_scrape

        result = handle_court_scrape(_SPECIAL_PAYLOAD, fetch=fake_fetch)
        self.assertNotIn("sweeps", result)
        self.assertGreater(result["cases"], 0)

    def test_sweep_jobs_dedup_separately_from_causelist_jobs(self):
        # Same court, both kinds queued: the sweep must not be deduped away by
        # the cause-list job (or vice versa).
        call_command("enqueue_scrape", "--court", "special")
        call_command("enqueue_scrape", "--court", "special", "--sweep")
        keys = set(Job.objects.filter(kind="court_scrape").values_list("dedup_key", flat=True))
        self.assertIn("court_scrape:special:special", keys)
        self.assertIn("court_scrape:special:special:sweep", keys)

    def test_sweep_enqueue_sets_the_payload_flags(self):
        call_command("enqueue_scrape", "--court", "special", "--sweep", "--sweep-budget", "7")
        job = Job.objects.get(dedup_key="court_scrape:special:special:sweep")
        self.assertTrue(job.payload["sweep"])
        self.assertFalse(job.payload["causelist"])
        self.assertEqual(job.payload["sweep_budget"], 7)

    def test_sweep_courts_rotates_instead_of_queueing_the_whole_fleet(self):
        """A recurring sweep can't queue all 99 courts at once.

        scrape_worker --once drains sequentially in one pod, under one
        activeDeadlineSeconds the cause-list crawl also has to fit inside. The
        rotation is what makes an automated sweep bounded per tick.
        """
        out = StringIO()
        call_command("enqueue_scrape", "--court", "high", "--sweep",
                     "--sweep-courts", "3", stdout=out)
        queued = Job.objects.filter(kind="court_scrape")
        self.assertEqual(queued.count(), 3)
        self.assertIn("wait for a later run", out.getvalue())

    def test_the_rotation_advances_to_courts_not_yet_swept(self):
        call_command("enqueue_scrape", "--court", "high", "--sweep", "--sweep-courts", "3")
        first = set(Job.objects.values_list("payload__court_id", flat=True))
        # Finish them, which both frees the dedup keys and stamps the cursor.
        Job.objects.update(status=Job.DONE, completed_at=timezone.now())

        call_command("enqueue_scrape", "--court", "high", "--sweep", "--sweep-courts", "3")
        second = set(
            Job.objects.filter(status=Job.QUEUED).values_list("payload__court_id", flat=True)
        )
        self.assertEqual(len(second), 3)
        self.assertFalse(second & first, "a second run must move on, not re-sweep the same courts")

    def test_a_court_whose_sweep_never_finished_keeps_its_turn(self):
        # Self-correcting cursor: a failed sweep leaves completed_at NULL, so the
        # court stays at the front rather than silently losing its slot.
        call_command("enqueue_scrape", "--court", "high", "--sweep", "--sweep-courts", "1")
        stuck = Job.objects.get().payload["court_id"]
        Job.objects.update(status=Job.FAILED, completed_at=None, dedup_key=None)

        call_command("enqueue_scrape", "--court", "high", "--sweep", "--sweep-courts", "1")
        retried = Job.objects.filter(status=Job.QUEUED).get().payload["court_id"]
        self.assertEqual(retried, stuck)

    def test_sweep_courts_without_sweep_is_rejected(self):
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError):
            call_command("enqueue_scrape", "--court", "high", "--sweep-courts", "3")

    def test_series_filter_reaches_the_sweep(self):
        """CR-only is the difference between a 4-minute backfill and an hour."""
        from courts.job_handlers import handle_court_scrape

        for series in ("CR", "OA"):
            for seq in (1, 5):
                CourtCase.objects.using("ngm").create(
                    court_id="special", case_number=f"076-{series}-{seq:04d}"
                )
        result = handle_court_scrape(
            {**self._SWEEP_PAYLOAD, "sweep_series": ["CR"], "sweep_tail": 0,
             "sweep_budget": 50},
            fetch=fake_fetch,
        )
        # 3 interior holes in CR; OA's 3 are left alone. sweep_tail=0 must be
        # honoured as zero rather than read as "unset" and defaulted.
        self.assertEqual(result["swept"], 3)

    def test_enqueue_sets_series_and_tail_and_dedups_them_apart(self):
        call_command("enqueue_scrape", "--court", "special", "--sweep",
                     "--sweep-series", "cr", "--sweep-tail", "100")
        job = Job.objects.get(dedup_key="court_scrape:special:special:sweep:CR")
        self.assertEqual(job.payload["sweep_series"], ["CR"])
        self.assertEqual(job.payload["sweep_tail"], 100)

        # An all-series sweep must not be deduped away by the CR one.
        call_command("enqueue_scrape", "--court", "special", "--sweep")
        self.assertTrue(Job.objects.filter(dedup_key="court_scrape:special:special:sweep").exists())

    def test_the_budget_is_shared_across_courts_not_granted_to_each(self):
        """A tier payload with no ``court_id`` sweeps every leaf court under ONE
        job. Per-court budgets would multiply by 77 for district and overrun the
        CronJob's deadline, taking the cause-list crawl down with it.
        """
        from courts.job_handlers import handle_court_scrape

        for court_id in ("special", "other"):
            Court.objects.using("ngm").get_or_create(
                identifier=court_id, defaults={"court_type": "", "full_name_nepali": ""}
            )
            for seq in (1, 21):
                CourtCase.objects.using("ngm").create(
                    court_id=court_id, case_number=f"076-CR-{seq:04d}"
                )
        with mock.patch.object(
            registry.REGISTRY["special"], "court_ids", return_value=["special", "other"]
        ):
            result = handle_court_scrape(
                {**self._SWEEP_PAYLOAD, "court_id": None}, fetch=fake_fetch
            )
        self.assertEqual(sum(s["attempts"] for s in result["sweeps"]), 5)
        self.assertGreater(result["deferred"] + result["courts_skipped"], 0)
