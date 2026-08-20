"""DB tests for the court-agnostic crawl write path (courts.scraper.base):
cause-list upsert, the extra_data-union (never-clobber) rule, and enrichment
with write-time case_status normalization. Uses the ngm test DB."""

from datetime import date

from django.test import TestCase

from courts.case_status import ACQUITTED, CONVICTED, PARTIALLY_CONVICTED
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
    # hearing_date_ad is overridable (via kw) so the verdict-promotion tests can
    # pin a BS/AD pair, or drop the AD date to exercise the undated path.
    return ParsedHearing(
        case_number=cn, court_identifier="special", hearing_date_bs=date_bs,
        hearing_date_ad=kw.pop("hearing_date_ad", date(2025, 4, 18)),
        serial_no=serial, **kw,
    )


class CauselistUpsertTests(_NgmTestCase):
    def test_upsert_creates_case_and_hearing(self):
        stats = upsert_causelist([(_case(), _hearing(case_status="पेशी"))])
        self.assertEqual(stats, {"cases": 1, "hearings": 1, "verdicts_promoted": 0})
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

    def test_relist_null_key_does_not_clobber_existing(self):
        upsert_causelist([(_case("082-CR-0017"), _hearing("082-CR-0017"))])
        case = CourtCase.objects.using("ngm").get(case_number="082-CR-0017")
        case.extra_data = {"division": "रिट १", "category": "फाँट क"}
        case.save(using="ngm")
        # A later re-list emits division=None (weaker) — it must NOT overwrite the
        # enriched "रिट १", but a genuinely new key is still added.
        pc = _case("082-CR-0017", division=None, new="x")
        upsert_causelist([(pc, _hearing("082-CR-0017", date_bs="2082-03-03"))])
        case.refresh_from_db()
        self.assertEqual(case.extra_data["division"], "रिट १")
        self.assertEqual(case.extra_data["new"], "x")


class CauselistVerdictPromotionTests(_NgmTestCase):
    """A decisive sitting must land on the CASE row, not just the hearing row.

    Regression: enrichment runs once per case (crawl excludes status="enriched")
    and is the only writer of case_status/verdict_*, so a case decided after that
    one pass read as ongoing forever — 114 Special Court cases held a फैसला
    hearing while their case row still said चलिरहेको.
    """

    # The real 081-CR-0111 disposing sitting: BS 2083-03-25 == AD 2026-07-09,
    # decided सफाई. Kept a true BS/AD pair so the fixtures can't teach a wrong one.
    _VERDICT_BS = "2083-03-25"
    _VERDICT_AD = date(2026, 7, 9)

    def _decided(self, cn, date_bs=_VERDICT_BS, decision="सफाई", **kw):
        kw.setdefault("hearing_date_ad", self._VERDICT_AD)
        return upsert_causelist(
            [(_case(cn), _hearing(cn, date_bs=date_bs, case_status="फैसला",
                                  decision_type=decision, **kw))]
        )

    def _row(self, cn):
        return CourtCase.objects.using("ngm").get(court_id="special", case_number=cn)

    def test_faisala_promotes_onto_the_case_row(self):
        stats = self._decided("083-CR-0001")
        self.assertEqual(stats["verdicts_promoted"], 1)
        case = self._row("083-CR-0001")
        self.assertEqual(case.case_status, "फैसला")
        self.assertEqual(case.verdict_type, ACQUITTED)
        self.assertEqual(case.verdict_date_bs, self._VERDICT_BS)
        self.assertEqual(case.verdict_date_ad, self._VERDICT_AD)

    def test_promotion_overwrites_an_ongoing_status_from_enrichment(self):
        # The exact 081-CR-0111 shape: enriched pre-verdict, so the case row says
        # ongoing with NULL verdicts, then the deciding sitting is listed.
        upsert_causelist([(_case("083-CR-0002"), _hearing("083-CR-0002"))])
        case = self._row("083-CR-0002")
        case.case_status, case.status = "चलिरहेको", "enriched"
        case.save(using="ngm")
        self._decided("083-CR-0002")
        case.refresh_from_db()
        self.assertEqual(case.case_status, "फैसला")
        self.assertEqual(case.verdict_type, ACQUITTED)

    def test_interlocutory_sitting_promotes_nothing(self):
        stats = upsert_causelist([
            (_case("083-CR-0003"),
             _hearing("083-CR-0003", case_status="आदेश", decision_type="साक्षी बुझ्ने")),
        ])
        self.assertEqual(stats["verdicts_promoted"], 0)
        case = self._row("083-CR-0003")
        self.assertIsNone(case.case_status)
        self.assertIsNone(case.verdict_type)

    def test_unrecognised_decision_is_not_guessed(self):
        # Terminal status, but a decision_type outside the vocabulary: record
        # nothing rather than a guess.
        stats = self._decided("083-CR-0004", decision="कुनै अज्ञात व्यहोरा")
        self.assertEqual(stats["verdicts_promoted"], 0)
        self.assertIsNone(self._row("083-CR-0004").verdict_type)

    def test_partial_conviction_is_not_recorded_as_full(self):
        # आंशिक must be matched ahead of ठहर — 593 rows once carried CONVICTED
        # when their own hearing said आंशिक ठहर.
        self._decided("083-CR-0005", decision="आंशिक ठहर")
        self.assertEqual(self._row("083-CR-0005").verdict_type, PARTIALLY_CONVICTED)

    def test_older_relist_does_not_regress_a_newer_verdict(self):
        # Lookbacks re-crawl historical dates, and a case can be decided, reopened
        # and decided again. The earlier sitting must not overwrite the later one.
        self._decided("083-CR-0006", decision="ठहर")
        self._decided(
            "083-CR-0006", date_bs="2082-05-03", decision="सफाई",
            hearing_date_ad=date(2025, 8, 19), serial="2",
        )
        case = self._row("083-CR-0006")
        self.assertEqual(case.verdict_type, CONVICTED)
        self.assertEqual(case.verdict_date_bs, self._VERDICT_BS)

    def test_undated_sitting_never_clobbers_a_held_verdict_date(self):
        self._decided("083-CR-0007", decision="ठहर")
        stats = self._decided(
            "083-CR-0007", date_bs="", decision="सफाई",
            hearing_date_ad=None, serial="3",
        )
        self.assertEqual(stats["verdicts_promoted"], 0)
        case = self._row("083-CR-0007")
        self.assertEqual(case.verdict_type, CONVICTED)
        self.assertEqual(case.verdict_date_ad, self._VERDICT_AD)

    def test_relist_of_the_same_sitting_is_idempotent(self):
        self._decided("083-CR-0008")
        before = self._row("083-CR-0008").verdict_date_ad
        self._decided("083-CR-0008")
        case = self._row("083-CR-0008")
        self.assertEqual(case.verdict_type, ACQUITTED)
        self.assertEqual(case.verdict_date_ad, before)

    def test_promotion_still_unions_extra_data(self):
        # The promotion must not cost us the never-clobber rule.
        upsert_causelist([(_case("083-CR-0009"), _hearing("083-CR-0009"))])
        case = self._row("083-CR-0009")
        case.extra_data = {"category": "फाँट क", "enrichment_hearings": [{"d": "2081"}]}
        case.save(using="ngm")
        self._decided("083-CR-0009")
        case.refresh_from_db()
        self.assertEqual(case.extra_data["enrichment_hearings"], [{"d": "2081"}])
        self.assertEqual(case.verdict_type, ACQUITTED)


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

    def test_mark_scraped_creates_court_for_empty_causelist(self):
        # A crawled date with an EMPTY cause-list still gets marked scraped; the
        # court FK must not fail just because no cases were written and the court
        # is not yet in the DB. Regression: district courts with empty days threw
        # IntegrityError (FOREIGN KEY constraint failed) on a fresh/unseeded DB.
        self.assertFalse(Court.objects.using("ngm").filter(identifier="achhamdc").exists())
        mark_scraped("achhamdc", "2082-01-06")
        self.assertTrue(Court.objects.using("ngm").filter(identifier="achhamdc").exists())
        self.assertEqual(scraped_dates_for("achhamdc"), {"2082-01-06"})


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

    def test_empty_enrichment_does_not_mark_enriched_or_delete_parties(self):
        # A WAF-rejection / empty detail page parses into an empty enrichment.
        # It must NOT flip status to "enriched" nor wipe the existing party.
        self._seed("082-CR-0030")
        CaseEntity.objects.using("ngm").create(
            court_id="special", case_number="082-CR-0030", side="defendant", name="क"
        )
        self.assertFalse(apply_enrichment("special", "082-CR-0030", ParsedEnrichment()))
        case = CourtCase.objects.using("ngm").get(case_number="082-CR-0030")
        self.assertEqual(case.status, "pending")  # unchanged — retried next run
        self.assertEqual(
            CaseEntity.objects.using("ngm").filter(case_number="082-CR-0030").count(), 1
        )
