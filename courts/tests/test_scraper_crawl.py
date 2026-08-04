"""End-to-end orchestration test (courts.scraper.crawl.run_crawl) with a FAKE
fetcher — no network, no live portal. Proves the whole chain: fetch → parse →
ORM upsert → frontier → enrichment with write-time normalization, against the
ngm test DB. Uses the Special module."""

from datetime import date

from django.test import TestCase

from courts.case_status import ACQUITTED
from courts.models import CourtCase, CourtCaseHearing, ScrapedDate
from courts.scraper import special
from courts.scraper.crawl import run_crawl
from courts.tests.special_fixtures import fake_fetch


class _NgmTestCase(TestCase):
    databases = "__all__"


class CrawlOrchestrationTests(_NgmTestCase):
    def test_run_crawl_writes_causelist_frontier_and_enriches(self):
        stats = run_crawl(
            special,
            fetch=fake_fetch,
            today=date(2025, 4, 20),
            lookback_days=2,
            limit_dates=1,
            write=True,
            enrich=True,
        )
        [s] = stats
        self.assertEqual(s.court_id, "special")
        self.assertEqual(s.cases, 1)
        self.assertEqual(s.hearings, 1)
        self.assertEqual(s.enriched, 1)

        case = CourtCase.objects.using("ngm").get(court_id="special", case_number="082-CR-0015")
        self.assertEqual(case.case_type, "भ्रष्टाचार")
        self.assertEqual(case.status, "enriched")
        self.assertIsNone(case.case_status)  # header artifact dropped at write time
        self.assertEqual(case.verdict_type, ACQUITTED)  # derived from the detail hearing
        self.assertEqual(CourtCaseHearing.objects.using("ngm").filter(case_number="082-CR-0015").count(), 1)
        self.assertEqual(ScrapedDate.objects.using("ngm").filter(court_id="special").count(), 1)

    def test_enrich_is_scoped_to_this_crawls_cases(self):
        # A pre-existing non-enriched case from an earlier crawl must NOT be
        # enriched by a later limited run that never touched it (else
        # `--limit-dates 1 --enrich` fans out over the whole corpus).
        from courts.models import Court

        Court.objects.using("ngm").get_or_create(
            identifier="special", defaults={"court_type": "special", "full_name_nepali": "x"}
        )
        CourtCase.objects.using("ngm").create(
            court_id="special", case_number="070-CR-9999", status="pending"
        )
        run_crawl(special, fetch=fake_fetch, today=date(2025, 4, 20),
                  lookback_days=2, limit_dates=1, write=True, enrich=True)
        self.assertEqual(
            CourtCase.objects.using("ngm").get(case_number="082-CR-0015").status, "enriched"
        )
        self.assertEqual(
            CourtCase.objects.using("ngm").get(case_number="070-CR-9999").status, "pending"
        )

    def test_dry_run_writes_nothing(self):
        stats = run_crawl(special, fetch=fake_fetch, today=date(2025, 4, 20),
                          lookback_days=2, limit_dates=1, write=False)
        self.assertEqual(stats[0].cases, 1)  # parsed
        self.assertEqual(CourtCase.objects.using("ngm").count(), 0)  # but not written
        self.assertEqual(ScrapedDate.objects.using("ngm").count(), 0)
