"""Tests for the case_status backfill (courts.management.commands.backfill_case_status).

Two layers: the pure ``compute_case_updates`` transform (no DB) and ``run_backfill``
against the ngm test DB (dry-run writes nothing; execute applies + is idempotent;
keyset pagination covers every row). Issue IDs (DQ-01…03) refer to the court-case
data-quality baseline.
"""

from datetime import date

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from courts.case_status import ACQUITTED, DECIDED, DISMISSED, OTHER, PENDING, UNKNOWN
from courts.management.commands.backfill_case_status import (
    compute_case_updates,
    run_backfill,
)
from courts.models import Court, CourtCase


# --- pure transform (no DB) --------------------------------------------------


class ComputeCaseUpdatesTests(TestCase):
    def test_header_artifact_row_is_cleared(self):
        # DQ-01: the ~103k Supreme rows whose status is the column header.
        updates, parsed = compute_case_updates("आदेश /फैसलाको किसिम", None, None, None, None)
        self.assertEqual(updates, {"case_status": None})
        self.assertEqual(parsed.lifecycle_status, UNKNOWN)

    def test_arrow_row_sets_verdict_type_only(self):
        # DQ-02: outcome enum from status; the arrow form carries no date.
        updates, parsed = compute_case_updates(
            "फैसला / अन्तिम आदेश >> अभियोग दावी पुग्ने", None, None, None, None
        )
        self.assertEqual(updates, {"verdict_type": "CONVICTED"})
        self.assertEqual(parsed.verdict_type, "CONVICTED")

    def test_paren_row_fills_verdict_date(self):
        # DQ-03: Special-court shape — recover the verdict date from case_status.
        updates, parsed = compute_case_updates("फैसला (मिती: २०८२/०९/२८)", None, None, None, None)
        self.assertEqual(updates["verdict_date_bs"], "2082-09-28")
        self.assertIsNotNone(updates["verdict_date_ad"])
        self.assertEqual(parsed.lifecycle_status, DECIDED)

    def test_paren_row_uses_hearing_fallback_for_verdict_type(self):
        hearings = [{"case_status": "फैसला", "decision_type": "सफाई"}]
        updates, _ = compute_case_updates("फैसला (मिती: २०८२/०९/२८)", None, None, None, hearings)
        self.assertEqual(updates["verdict_type"], ACQUITTED)
        self.assertEqual(updates["verdict_date_bs"], "2082-09-28")

    def test_unmapped_arrow_sets_other_and_flags(self):
        # An arrow outcome absent from _OUTCOME_MAP → OTHER, surfaced via unmapped.
        updates, parsed = compute_case_updates(
            "फैसला / अन्तिम आदेश >> कुनै नयाँ नतिजा", None, None, None, None
        )
        self.assertEqual(updates, {"verdict_type": OTHER})
        self.assertTrue(parsed.unmapped)

    def test_idempotent_when_already_correct(self):
        # Re-running must be a no-op: verdict_type already set, arrow has no date.
        updates, _ = compute_case_updates(
            "फैसला / अन्तिम आदेश >> डिसमिस", "DISMISSED", None, None, None
        )
        self.assertEqual(updates, {})

    def test_existing_verdict_date_not_overwritten(self):
        updates, _ = compute_case_updates(
            "फैसला (मिती: २०८२/०९/२८)", "CONVICTED", "2081-01-17", date(2024, 4, 29), None
        )
        self.assertNotIn("verdict_date_bs", updates)
        self.assertEqual(updates, {})

    def test_pending_row_no_column_updates(self):
        updates, parsed = compute_case_updates("चालु", None, None, None, None)
        self.assertEqual(updates, {})
        self.assertEqual(parsed.lifecycle_status, PENDING)


# --- run_backfill against the ngm test DB ------------------------------------


def _mk(cn, status_raw=None, court="special", **kw):
    Court.objects.using("ngm").get_or_create(
        identifier=court, defaults={"court_type": court, "full_name_nepali": "x"}
    )
    return CourtCase.objects.using("ngm").create(
        court_id=court, case_number=cn, case_status=status_raw, **kw
    )


class RunBackfillDBTests(TestCase):
    databases = "__all__"

    def test_dry_run_counts_but_writes_nothing(self):
        _mk("082-CR-0001", "आदेश /फैसलाको किसिम")
        _mk("082-CR-0002", "फैसला (मिती: २०८२/०९/२८)")
        stats = run_backfill(execute=False)
        self.assertEqual(stats["scanned"], 2)
        self.assertEqual(stats["header_cleared"], 1)
        self.assertEqual(stats["verdict_date_set"], 1)
        # Nothing persisted.
        self.assertEqual(
            CourtCase.objects.using("ngm").get(case_number="082-CR-0001").case_status,
            "आदेश /फैसलाको किसिम",
        )
        self.assertIsNone(
            CourtCase.objects.using("ngm").get(case_number="082-CR-0002").verdict_date_bs
        )

    def test_execute_applies_and_is_idempotent(self):
        _mk("082-CR-0001", "आदेश /फैसलाको किसिम")  # DQ-01 header
        _mk(
            "082-CR-0002", "फैसला (मिती: २०८२/०९/२८)",  # DQ-03 date + DQ-02 hearing fallback
            extra_data={"enrichment_hearings": [{"case_status": "फैसला", "decision_type": "सफाई"}]},
        )
        _mk("082-CR-0003", "फैसला / अन्तिम आदेश >> डिसमिस")  # DQ-02 arrow enum

        stats = run_backfill(execute=True)
        self.assertEqual(stats["rows_changed"], 3)
        self.assertEqual(stats["header_cleared"], 1)
        self.assertEqual(stats["verdict_date_set"], 1)
        self.assertEqual(stats["verdict_type_set"], 2)

        c1 = CourtCase.objects.using("ngm").get(case_number="082-CR-0001")
        self.assertIsNone(c1.case_status)
        c2 = CourtCase.objects.using("ngm").get(case_number="082-CR-0002")
        self.assertEqual(c2.verdict_date_bs, "2082-09-28")
        self.assertIsNotNone(c2.verdict_date_ad)
        self.assertEqual(c2.verdict_type, ACQUITTED)
        c3 = CourtCase.objects.using("ngm").get(case_number="082-CR-0003")
        self.assertEqual(c3.verdict_type, DISMISSED)

        # Second pass changes nothing.
        stats2 = run_backfill(execute=True)
        self.assertEqual(stats2["rows_changed"], 0)

    def test_existing_verdict_date_not_overwritten(self):
        _mk(
            "082-CR-0004", "फैसला (मिती: २०८२/०९/२८)",
            verdict_date_bs="2081-01-01", verdict_date_ad=date(2024, 4, 13),
        )
        run_backfill(execute=True)
        c = CourtCase.objects.using("ngm").get(case_number="082-CR-0004")
        self.assertEqual(c.verdict_date_bs, "2081-01-01")

    def test_execute_bumps_updated_at_only_on_changed_rows(self):
        # updated_at IS bumped on changed rows so `reindex_courtcases --since`
        # finds exactly them; an unchanged row is never saved, so it's untouched.
        _mk("082-CR-0050", "आदेश /फैसलाको किसिम")  # header → changes
        _mk("082-CR-0051", "चालु")  # pending → no change
        changed_before = CourtCase.objects.using("ngm").get(case_number="082-CR-0050").updated_at
        same_before = CourtCase.objects.using("ngm").get(case_number="082-CR-0051").updated_at
        run_backfill(execute=True)
        self.assertGreater(
            CourtCase.objects.using("ngm").get(case_number="082-CR-0050").updated_at, changed_before
        )
        self.assertEqual(
            CourtCase.objects.using("ngm").get(case_number="082-CR-0051").updated_at, same_before
        )

    def test_muted_indexing_disconnects_then_restores_signal(self):
        from django.db.models.signals import post_save

        from courts import signals as s
        from courts.management.commands.backfill_case_status import _muted_indexing

        uid = "ngm_courtcase_search_index"
        with _muted_indexing():
            # already disconnected inside the context → a disconnect returns False
            self.assertFalse(post_save.disconnect(dispatch_uid=uid, sender=CourtCase))
        # restored on exit; put it back unconditionally before asserting
        restored = post_save.disconnect(dispatch_uid=uid, sender=CourtCase)
        post_save.connect(s._index_courtcase, sender=CourtCase, dispatch_uid=uid)
        self.assertTrue(restored)

    def test_keyset_paginates_every_row_across_courts(self):
        for i in range(1, 4):
            _mk(f"082-CR-000{i}", "आदेश /फैसलाको किसिम", court="special")
        for i in range(1, 3):
            _mk(f"080-WO-000{i}", "आदेश /फैसलाको किसिम", court="supreme")
        stats = run_backfill(execute=True, batch_size=2)  # 5 rows, 2 per page
        self.assertEqual(stats["scanned"], 5)
        self.assertEqual(stats["header_cleared"], 5)
        self.assertEqual(
            CourtCase.objects.using("ngm").filter(case_status__isnull=True).count(), 5
        )


class CommandGuardTests(TestCase):
    def test_execute_without_confirm_refuses(self):
        # The guard short-circuits before any DB scan — --execute alone must fail.
        with self.assertRaisesMessage(CommandError, "--i-understand-this-writes-prod"):
            call_command("backfill_case_status", execute=True)
