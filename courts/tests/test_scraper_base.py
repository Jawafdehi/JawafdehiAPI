"""DB tests for the court-agnostic crawl write path (courts.scraper.base):
cause-list upsert, the extra_data-union (never-clobber) rule, and enrichment
with write-time case_status normalization. Uses the ngm test DB."""

from datetime import date

from django.test import TestCase

from courts.case_status import ACQUITTED
from courts.models import CaseEntity, Court, CourtCase, CourtCaseHearing, ScrapedDate
from courts.scraper.base import (
    apply_enrichment,
    mark_scraped,
    scraped_dates_for,
    upsert_causelist,
)
from courts.scraper.rows import ParsedCase, ParsedEnrichment, ParsedHearing


class _NgmTestCase(TestCase):
    databases = "__all__"


def _case(cn="082-CR-0015", **extra):
    return ParsedCase(
        case_number=cn, court_identifier="special", case_type="भ्रष्टाचार",
        plaintiff="नेपाल सरकार", defendant="क", extra_data={"category": "फाँट क", **extra},
    )


def _hearing(cn="082-CR-0015", date_bs="2082-01-05", serial="1", **kw):
    return ParsedHearing(
        case_number=cn, court_identifier="special", hearing_date_bs=date_bs,
        hearing_date_ad=date(2025, 4, 18), serial_no=serial, **kw,
    )


class CauselistUpsertTests(_NgmTestCase):
    def test_upsert_creates_case_and_hearing(self):
        stats = upsert_causelist([(_case(), _hearing(case_status="पेशी"))])
        self.assertEqual(stats, {"cases": 1, "hearings": 1})
        case = CourtCase.objects.using("ngm").get(court_id="special", case_number="082-CR-0015")
        self.assertEqual(case.case_type, "भ्रष्टाचार")
        self.assertEqual(case.extra_data["category"], "फाँट क")
        self.assertEqual(
            CourtCaseHearing.objects.using("ngm").filter(case_number="082-CR-0015").count(), 1
        )

    def test_relist_unions_extra_data_and_keeps_enrichment(self):
        upsert_causelist([(_case("082-CR-0016"), _hearing("082-CR-0016"))])
        case = CourtCase.objects.using("ngm").get(case_number="082-CR-0016")
        case.extra_data = {"category": "फाँट क", "enrichment_hearings": [{"d": "2081"}]}
        case.status = "enriched"
        case.save(using="ngm")
        # re-list on a later hearing date must NOT clobber the enriched extra_data
        upsert_causelist(
            [(_case("082-CR-0016"), _hearing("082-CR-0016", date_bs="2082-02-10"))]
        )
        case.refresh_from_db()
        self.assertEqual(case.extra_data["enrichment_hearings"], [{"d": "2081"}])
        self.assertEqual(case.status, "enriched")


class FrontierTests(_NgmTestCase):
    def test_mark_and_read_frontier(self):
        Court.objects.using("ngm").get_or_create(
            identifier="special", defaults={"court_type": "special", "full_name_nepali": "x"}
        )
        self.assertEqual(scraped_dates_for("special"), set())
        mark_scraped("special", "2082-01-05", note="1 bench")
        mark_scraped("special", "2082-01-05")  # idempotent
        self.assertEqual(scraped_dates_for("special"), {"2082-01-05"})
        self.assertEqual(ScrapedDate.objects.using("ngm").count(), 1)


class EnrichmentTests(_NgmTestCase):
    def _seed(self, cn="082-CR-0020"):
        Court.objects.using("ngm").get_or_create(
            identifier="special", defaults={"court_type": "special", "full_name_nepali": "x"}
        )
        CourtCase.objects.using("ngm").create(court_id="special", case_number=cn, status="pending")

    def test_enrichment_drops_header_and_derives_verdict(self):
        self._seed()
        enr = ParsedEnrichment(
            core_fields={"case_status": "आदेश /फैसलाको किसिम", "case_subject": "घुस लिएको"},
            extra_data={
                "enrichment_hearings": [{"case_status": "फैसला", "decision_type": "सफाई"}],
                "division": "रिट १",
            },
            entities=[
                {"side": "plaintiff", "name": "नेपाल सरकार"},
                {"side": "defendant", "name": "क"},
            ],
        )
        self.assertTrue(apply_enrichment("special", "082-CR-0020", enr))
        case = CourtCase.objects.using("ngm").get(case_number="082-CR-0020")
        self.assertIsNone(case.case_status)  # header artifact dropped
        self.assertEqual(case.verdict_type, ACQUITTED)  # derived from the final hearing
        self.assertEqual(case.case_subject, "घुस लिएको")
        self.assertEqual(case.status, "enriched")
        self.assertEqual(case.extra_data["division"], "रिट १")
        self.assertEqual(
            CaseEntity.objects.using("ngm").filter(case_number="082-CR-0020").count(), 2
        )

    def test_enrichment_missing_case_returns_false(self):
        self.assertFalse(apply_enrichment("special", "999-XX-9999", ParsedEnrichment()))
