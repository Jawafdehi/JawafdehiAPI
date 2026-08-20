"""Tests for the promote_hearing_verdicts backfill.

Repairs cases decided AFTER their one-shot enrichment: the verdict sits on a
``court_case_hearings`` row while the case row still reads ``चलिरहेको`` with NULL
verdict columns. Companion to the write-path fix in #459, which stops new drift
but cannot repair the 114 Special Court cases already in that state.
"""

from datetime import date

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone

from courts.case_status import (
    ACQUITTED,
    CLAIM_DENIED,
    CONVICTED,
    PARTIALLY_CONVICTED,
)
from courts.management.commands.promote_hearing_verdicts import run_promotion
from courts.models import Court, CourtCase, CourtCaseHearing

# The real 081-CR-0111 disposing sitting: BS 2083-03-25 == AD 2026-07-09, सफाई.
VERDICT_BS = "2083-03-25"
VERDICT_AD = date(2026, 7, 9)


class _Ngm(TestCase):
    databases = "__all__"

    def setUp(self):
        Court.objects.using("ngm").get_or_create(
            identifier="special",
            defaults={"court_type": "special", "full_name_nepali": "विशेष अदालत"},
        )

    def _case(self, cn, *, case_status="चलिरहेको", **kw):
        return CourtCase.objects.using("ngm").create(
            court_id="special", case_number=cn, case_status=case_status,
            status="enriched", **kw,
        )

    def _hearing(self, cn, date_bs, date_ad, status, decision, serial="1"):
        return CourtCaseHearing.objects.using("ngm").create(
            court_id="special", case_number=cn, hearing_date_bs=date_bs,
            hearing_date_ad=date_ad, serial_no=serial, case_status=status,
            decision_type=decision, scraped_at=timezone.now(),
        )

    def _decided(self, cn, *, decision="सफाई", case_status="चलिरहेको", **kw):
        """The canonical broken shape: ongoing case row, disposing hearing."""
        case = self._case(cn, case_status=case_status, **kw)
        self._hearing(cn, "2082-05-03", date(2025, 8, 19), "आदेश", "थुनछेक आदेश (धरौटी)")
        self._hearing(cn, VERDICT_BS, VERDICT_AD, "फैसला", decision, serial="2")
        return case

    def _run(self, **kw):
        return run_promotion(court="special", **kw)

    def _row(self, cn):
        return CourtCase.objects.using("ngm").get(court_id="special", case_number=cn)


class PromotionTests(_Ngm):
    def test_promotes_verdict_and_corrects_the_ongoing_status(self):
        self._decided("081-CR-0111")
        stats = self._run(execute=True)
        self.assertEqual(stats["rows_changed"], 1)
        self.assertEqual(stats["case_status_fixed"], 1)
        self.assertEqual(stats["verdict_type_set"], 1)
        self.assertEqual(stats["verdict_date_set"], 1)
        case = self._row("081-CR-0111")
        self.assertEqual(case.case_status, "फैसला")
        self.assertEqual(case.verdict_type, ACQUITTED)
        self.assertEqual(case.verdict_date_bs, VERDICT_BS)
        self.assertEqual(case.verdict_date_ad, VERDICT_AD)

    def test_dry_run_writes_nothing_but_still_projects(self):
        self._decided("081-CR-0112")
        stats = self._run()
        self.assertEqual(stats["rows_changed"], 1)
        case = self._row("081-CR-0112")
        self.assertEqual(case.case_status, "चलिरहेको")
        self.assertIsNone(case.verdict_type)

    def test_second_pass_changes_nothing(self):
        self._decided("081-CR-0113")
        self._run(execute=True)
        before = self._row("081-CR-0113").updated_at
        stats = self._run(execute=True)
        self.assertEqual(stats["rows_changed"], 0)
        self.assertEqual(self._row("081-CR-0113").updated_at, before)

    def test_each_disposition_maps_through_the_shared_vocabulary(self):
        # The four dispositions behind the 114: सफाई 46, आंशिक ठहर 37, ठहर 27,
        # खारेज 3. आंशिक must beat ठहर or a partial conviction becomes a full one.
        for cn, decision, expected in (
            ("082-CR-0001", "सफाई", ACQUITTED),
            ("082-CR-0002", "आंशिक ठहर", PARTIALLY_CONVICTED),
            ("082-CR-0003", "ठहर", CONVICTED),
            ("082-CR-0004", "खारेज", CLAIM_DENIED),
        ):
            with self.subTest(decision=decision):
                self._decided(cn, decision=decision)
        self._run(execute=True)
        for cn, _, expected in (
            ("082-CR-0001", "सफाई", ACQUITTED),
            ("082-CR-0002", "आंशिक ठहर", PARTIALLY_CONVICTED),
            ("082-CR-0003", "ठहर", CONVICTED),
            ("082-CR-0004", "खारेज", CLAIM_DENIED),
        ):
            self.assertEqual(self._row(cn).verdict_type, expected, cn)

    def test_null_decision_is_left_alone_and_reported(self):
        # One of the 114 has a NULL decision_type. Record nothing, report it as a
        # coverage gap rather than guessing from the फैसला label alone.
        self._decided("082-CR-0005", decision=None)
        stats = self._run(execute=True)
        self.assertEqual(stats["rows_changed"], 0)
        self.assertEqual(stats["no_classifiable_outcome"], 1)
        case = self._row("082-CR-0005")
        self.assertEqual(case.case_status, "चलिरहेको")
        self.assertIsNone(case.verdict_type)

    def test_interlocutory_only_case_is_not_a_candidate(self):
        self._case("082-CR-0006")
        self._hearing("082-CR-0006", "2082-05-03", date(2025, 8, 19), "आदेश", "साक्षी बुझ्ने")
        stats = self._run(execute=True)
        self.assertEqual(stats["candidates"], 0)
        self.assertEqual(self._row("082-CR-0006").case_status, "चलिरहेको")

    def test_antim_adesh_with_an_interlocutory_order_promotes_nothing(self):
        # The portal writes अन्तिम आदेश on plainly interlocutory orders, so the
        # status label alone must never be treated as a disposition.
        self._case("082-CR-0007")
        self._hearing(
            "082-CR-0007", VERDICT_BS, VERDICT_AD, "अन्तिम आदेश",
            "कैफियत प्रतिवेदन माग्ने",
        )
        stats = self._run(execute=True)
        self.assertEqual(stats["candidates"], 1)
        self.assertEqual(stats["rows_changed"], 0)
        self.assertIsNone(self._row("082-CR-0007").verdict_type)


class NeverClobberTests(_Ngm):
    def test_an_existing_decided_status_is_left_verbatim(self):
        # ~2,970 special cases already carry the paren form. The command must not
        # rewrite them to a bare label.
        self._decided("082-CR-0010", case_status="फैसला (२०८३/०३/२५)")
        stats = self._run(execute=True)
        self.assertEqual(stats["case_status_fixed"], 0)
        self.assertEqual(self._row("082-CR-0010").case_status, "फैसला (२०८३/०३/२५)")

    def test_an_existing_verdict_type_is_never_overwritten(self):
        self._decided("082-CR-0011", decision="सफाई", verdict_type=CONVICTED)
        self._run(execute=True)
        case = self._row("082-CR-0011")
        self.assertEqual(case.verdict_type, CONVICTED)  # preserved
        self.assertEqual(case.verdict_date_bs, VERDICT_BS)  # gap still filled

    def test_an_existing_verdict_date_is_never_overwritten(self):
        self._decided(
            "082-CR-0012", verdict_date_bs="2080-01-01", verdict_date_ad=date(2023, 4, 14),
        )
        self._run(execute=True)
        case = self._row("082-CR-0012")
        self.assertEqual(case.verdict_date_bs, "2080-01-01")
        self.assertEqual(case.verdict_date_ad, date(2023, 4, 14))
        self.assertEqual(case.verdict_type, ACQUITTED)  # gap still filled

    def test_last_disposing_sitting_wins_on_a_reopened_case(self):
        # Decided, reopened on review, decided again — only the latest is operative.
        case = self._case("082-CR-0013")
        self._hearing(case.case_number, "2081-05-05", date(2024, 8, 20), "फैसला", "सफाई")
        self._hearing(
            case.case_number, VERDICT_BS, VERDICT_AD, "फैसला", "ठहर", serial="2",
        )
        self._run(execute=True)
        row = self._row("082-CR-0013")
        self.assertEqual(row.verdict_type, CONVICTED)
        self.assertEqual(row.verdict_date_bs, VERDICT_BS)


class ScopingAndSafetyTests(_Ngm):
    def test_case_flag_scopes_to_one_docket(self):
        self._decided("082-CR-0020")
        self._decided("082-CR-0021")
        stats = self._run(only=["082-CR-0020"], execute=True)
        self.assertEqual(stats["candidates"], 1)
        self.assertEqual(self._row("082-CR-0020").verdict_type, ACQUITTED)
        self.assertIsNone(self._row("082-CR-0021").verdict_type)

    def test_execute_without_the_confirm_flag_refuses(self):
        with self.assertRaises(CommandError):
            call_command("promote_hearing_verdicts", "--court", "special", "--execute")

    def test_command_dry_run_is_the_default(self):
        self._decided("082-CR-0022")
        call_command("promote_hearing_verdicts", "--court", "special", verbosity=0)
        self.assertIsNone(self._row("082-CR-0022").verdict_type)

    def test_command_applies_with_both_flags(self):
        self._decided("082-CR-0023")
        call_command(
            "promote_hearing_verdicts", "--court", "special", "--execute",
            "--i-understand-this-writes-prod", verbosity=0,
        )
        self.assertEqual(self._row("082-CR-0023").verdict_type, ACQUITTED)

    def test_another_court_is_untouched(self):
        Court.objects.using("ngm").get_or_create(
            identifier="patanhc",
            defaults={"court_type": "high", "full_name_nepali": "पाटन"},
        )
        CourtCase.objects.using("ngm").create(
            court_id="patanhc", case_number="082-CR-0030", case_status="चलिरहेको",
        )
        CourtCaseHearing.objects.using("ngm").create(
            court_id="patanhc", case_number="082-CR-0030", hearing_date_bs=VERDICT_BS,
            hearing_date_ad=VERDICT_AD, serial_no="1", case_status="फैसला",
            decision_type="सफाई", scraped_at=timezone.now(),
        )
        self._run(execute=True)
        other = CourtCase.objects.using("ngm").get(
            court_id="patanhc", case_number="082-CR-0030"
        )
        self.assertIsNone(other.verdict_type)
