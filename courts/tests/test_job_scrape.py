"""court_scrape job wiring: kind registration, handler, enqueuer, and the
in-process scrape_worker consumer — all against a FAKE fetch (no network).

Mirrors test_scraper_crawl.py's fake-fetcher approach; the crawl parse/write is
proven there, so these tests focus on the queue wiring: enqueue+dedup, the
handler's write + stats, and the worker's claim → run → finalize lifecycle
(including retryable vs. terminal failure classification).
"""

from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

from courts.models import CourtCase
from jobs import queue as jobs_queue
from jobs.models import Job
from jobs.registry import get as get_kind

_BENCH_SELECT = """<select name="bench_type">
  <option value="">--</option><option value="1">इजलास १</option></select>"""

_BENCH_PAGE = """<table width="100%" border="1">
  <tr><th>क्र</th><th>फाँट</th><th>मिति</th><th>मुद्दा</th><th>नं</th><th>वादी</th>
      <th>प्रतिवादी</th><th>मूल</th><th>कैफियत</th><th>स्थिति</th><th>किसिम</th></tr>
  <tr><td>१</td><td>क</td><td>२०८१/०५/१२</td><td>भ्रष्टाचार</td><td>०८२-CR-००१५</td>
      <td>नेपाल सरकार</td><td>क</td><td></td><td>-</td><td>पेशी</td><td>-</td></tr>
</table>"""

_DETAIL = """<table width="100%" border="0" cellspacing="0" cellpadding="1">
  <tr><td class="caption">मुद्दा</td><td>भ्रष्टाचार</td></tr>
  <tr><td class="caption">मुद्दाको स्थिती</td><td>आदेश /फैसलाको किसिम</td></tr>
</table>
<table><tr><td>पेशी को विवरण</td></tr>
<tr><td><table class="utivtbl"><tr><th>x</th><th>x</th><th>x</th><th>x</th></tr>
  <tr><td>२०८२/०९/२८</td><td>राम</td><td>फैसला</td><td>सफाई</td></tr></table></td></tr></table>"""


def _fake_fetch(url, data=None):
    data = data or {}
    if data.get("mode") == "showbench":
        return _BENCH_SELECT
    if "case_details" in url:
        return _DETAIL
    if data.get("mode") == "show":
        return _BENCH_PAGE
    return ""


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

        result = handle_court_scrape(_SPECIAL_PAYLOAD, fetch=_fake_fetch)

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
            handle_court_scrape({}, fetch=_fake_fetch)

    def test_handler_heartbeats_once_per_crawled_date(self):
        from courts.job_handlers import handle_court_scrape

        stages = []
        handle_court_scrape(_SPECIAL_PAYLOAD, on_stage=stages.append, fetch=_fake_fetch)
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
        with patch("courts.job_handlers.Fetcher", lambda: _fake_fetch):
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
        with patch("courts.job_handlers.Fetcher", lambda: _fake_fetch):
            call_command("scrape_worker", "--apply", "--once", "--max-jobs", "0")
        # --max-jobs 0 is a no-op-and-exit (not falsy-unlimited): job stays QUEUED.
        self.assertEqual(Job.objects.get(kind="court_scrape").status, Job.QUEUED)
