"""Tests for the production court-case importer (spec 01).

Exercises the importer service via COPY mode with an INJECTED ``source_rows``
iterable (so the cross-DB ETL path is tested without a live source Postgres) plus
INPLACE mode over rows seeded directly in the ``ngm`` test DB, and the command's
argument validation. Run from the repo root:

    TESTING=true DATABASE_URL=sqlite:// NES_DB_URL=sqlite:// NGM_DATABASE_URL=sqlite:// \
        uv run pytest courts/tests/test_import_courtcases.py
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from courts import search_index
from courts.importer import (
    CourtCaseImporter,
    ImportConfig,
    ImportMode,
)
from courts.models import CaseEntity, Court, CourtCase, CourtCaseHearing

_SCRAPED = datetime(2024, 5, 15, tzinfo=timezone.utc)


def _src_row(court="supreme", case="081-CR-0081", court_type="supreme", **over):
    """A legacy-shaped COPY-mode source row (all CourtCase columns + children)."""
    row = {
        "court_identifier": court,
        "court_type": court_type,
        "court_full_name_nepali": "अदालत",
        "court_full_name_english": "Court",
        "case_number": case,
        "registration_date_bs": "2081-01-01",
        "registration_date_ad": date(2024, 4, 13),
        "case_type": "भ्रष्टाचार",
        "case_status": "चालु",
        "plaintiff": "वादी",
        "defendant": "प्रतिवादी",
        "status": "enriched",
        "verdict_type": None,
        "verdict_date_bs": None,
        "verdict_date_ad": None,
        "verdict_judge": None,
        "case_subject": None,
        "hearing_count": 1,
        "registration_number": "123",
        "nes_id": None,
        "extra_data": None,
        "document_sources": None,
        "hearings": [],
        "entities": [],
    }
    row.update(over)
    return row


def _hearing(**over):
    h = {
        "hearing_date_bs": "2081-02-02",
        "hearing_date_ad": date(2024, 5, 15),
        "bench": "इजलास",
        "scraped_at": _SCRAPED,
    }
    h.update(over)
    return h


def _copy(rows, **cfg):
    options = dict(
        mode=ImportMode.COPY, courts=["supreme"], source_rows=rows, batch_size=10
    )
    options.update(cfg)
    return CourtCaseImporter(ImportConfig(**options))


class _NgmTestCase(TestCase):
    databases = "__all__"


class CopyModeLoadTests(_NgmTestCase):
    def test_loads_cases_hearings_parties_by_natural_key(self):
        row = _src_row(
            hearings=[_hearing(), _hearing(hearing_date_bs="2081-03-03")],
            entities=[
                {"side": "plaintiff", "name": "Ram", "nes_id": None},
                {"side": "defendant", "name": "Shyam", "nes_id": "entity:person/shyam"},
            ],
        )
        res = _copy([row]).run()
        self.assertEqual(CourtCase.objects.using("ngm").count(), 1)
        self.assertEqual(CourtCaseHearing.objects.using("ngm").count(), 2)
        self.assertEqual(CaseEntity.objects.using("ngm").count(), 2)
        self.assertEqual(res.upserted, 1)
        self.assertEqual(res.failed, 0)
        # nes_id is NOT imported — parties load with nes_id NULL even when the
        # source carries a (legacy-format) value.
        shyam = CaseEntity.objects.using("ngm").get(name="Shyam")
        self.assertIsNone(shyam.nes_id)

    def test_idempotent_rerun(self):
        row = _src_row(
            hearings=[_hearing()],
            entities=[{"side": "plaintiff", "name": "Ram", "nes_id": None}],
        )
        _copy([row]).run()
        _copy([row], allow_nonempty_target=True).run()
        self.assertEqual(CourtCase.objects.using("ngm").count(), 1)
        self.assertEqual(CourtCaseHearing.objects.using("ngm").count(), 1)
        self.assertEqual(CaseEntity.objects.using("ngm").count(), 1)

    def test_dry_run_writes_nothing_but_counts(self):
        row = _src_row(
            verdict_date_bs="**** ** **",
            entities=[{"side": "plaintiff", "name": "Ram", "nes_id": "entity:person/ram"}],
        )
        res = _copy([row], dry_run=True).run()
        self.assertEqual(CourtCase.objects.using("ngm").count(), 0)
        self.assertEqual(CaseEntity.objects.using("ngm").count(), 0)
        self.assertGreaterEqual(res.scanned, 1)
        self.assertGreaterEqual(res.upserted, 1)
        self.assertEqual(res.dq_verdict_nulled, 1)

    def test_limit_caps_total(self):
        rows = [_src_row(case=f"081-CR-000{i}") for i in range(1, 4)]
        res = _copy(rows, batch_size=1, limit=1).run()
        self.assertEqual(res.scanned, 1)
        self.assertEqual(CourtCase.objects.using("ngm").count(), 1)

    def test_special_defendants_flagged_untrusted(self):
        row = _src_row(
            court="special", court_type="special", case="080-CR-0007",
            entities=[{"side": "defendant", "name": "प्रतिवादी क…", "nes_id": None}],
        )
        res = CourtCaseImporter(
            ImportConfig(mode=ImportMode.COPY, courts=["special"], source_rows=[row])
        ).run()
        case = CourtCase.objects.using("ngm").get(court_id="special", case_number="080-CR-0007")
        self.assertTrue(case.extra_data["_dq"]["special_defendants_untrusted"])
        self.assertEqual(res.dq_special_flagged, 1)

    def test_high_court_devanagari_fields_recovered(self):
        row = _src_row(
            court="hcpatan", court_type="high", case="081-CR-0099",
            plaintiff=None, defendant=None, case_type=None, case_status=None,
            extra_data={
                "वादीहरु": "वादी नाम",
                "प्रतिवादीहरु": "प्रतिवादी नाम",
                "case_type_display": "रिट",
                "raw_status_display": "फैसला",
            },
        )
        res = CourtCaseImporter(
            ImportConfig(mode=ImportMode.COPY, courts=["hcpatan"], source_rows=[row])
        ).run()
        case = CourtCase.objects.using("ngm").get(court_id="hcpatan", case_number="081-CR-0099")
        self.assertEqual(case.plaintiff, "वादी नाम")
        self.assertEqual(case.defendant, "प्रतिवादी नाम")
        self.assertEqual(case.case_type, "रिट")
        self.assertEqual(case.case_status, "फैसला")
        self.assertEqual(res.dq_hc_recovered, 1)

    def test_case_type_normalized_and_raw_archived(self):
        # A leading case-number token is stripped in place; the raw value is kept
        # under extra_data._dq for reversibility, and the guard is counted.
        row = _src_row(case="081-CR-0300", case_type="080-cp-1852 लेनदेन")
        res = _copy([row]).run()
        case = CourtCase.objects.using("ngm").get(
            court_id="supreme", case_number="081-CR-0300"
        )
        self.assertEqual(case.case_type, "लेनदेन")
        self.assertEqual(case.extra_data["_dq"]["case_type_raw"], "080-cp-1852 लेनदेन")
        self.assertEqual(res.dq_case_type_normalized, 1)

    def test_case_type_statute_label_preserved(self):
        # A charge label with its statute section is meaningful, not noise — it
        # must pass through untouched (no rewrite, no raw archived, not counted).
        row = _src_row(case="081-CR-0301", case_type="चोरी गरेको (दफा 241)")
        res = _copy([row]).run()
        case = CourtCase.objects.using("ngm").get(
            court_id="supreme", case_number="081-CR-0301"
        )
        self.assertEqual(case.case_type, "चोरी गरेको (दफा 241)")
        self.assertEqual(res.dq_case_type_normalized, 0)
        self.assertNotIn("_dq", case.extra_data or {})

    def test_verdict_sentinel_not_surfaced(self):
        row = _src_row(case="081-CR-0042", verdict_date_bs="**** ** **")
        res = _copy([row]).run()
        case = CourtCase.objects.using("ngm").get(court_id="supreme", case_number="081-CR-0042")
        # physical column left as the scraper wrote it …
        self.assertEqual(case.verdict_date_bs, "**** ** **")
        self.assertEqual(res.dq_verdict_nulled, 1)
        # … but never surfaced to a consumer (search doc).
        self.assertNotIn("verdict_date_bs", search_index.build_doc(case))

    def test_non_ascii_natural_key_skipped_not_failed(self):
        row = _src_row(court="सर्वोच्च", court_type="supreme", case="०८१-CR-0081")
        res = CourtCaseImporter(
            ImportConfig(mode=ImportMode.COPY, courts=None, source_rows=[row])
        ).run()
        self.assertEqual(res.skipped, 1)
        self.assertEqual(res.failed, 0)
        self.assertEqual(res.upserted, 0)
        self.assertEqual(CourtCase.objects.using("ngm").count(), 0)

    def test_party_nes_id_is_ignored_loaded_null(self):
        row = _src_row(
            case="081-CR-0050",
            entities=[{"side": "plaintiff", "name": "Ram", "nes_id": "entity:person/ram"}],
        )
        res = _copy([row]).run()
        # The source nes_id is ignored entirely (assumed null) — party loads null.
        ent = CaseEntity.objects.using("ngm").get(name="Ram")
        self.assertIsNone(ent.nes_id)
        self.assertEqual(res.skipped, 0)
        self.assertEqual(res.upserted, 1)

    def test_copy_into_nonempty_target_refused_without_flag(self):
        row = _src_row(case="081-CR-0001")
        _copy([row]).run()
        with self.assertRaises(ValueError):
            _copy([_src_row(case="081-CR-0002")]).run()  # target nonempty, no flag


class InplaceModeTests(_NgmTestCase):
    def test_inplace_leaves_nes_id_and_status_untouched(self):
        court = Court.objects.using("ngm").create(
            identifier="supreme", court_type="supreme", full_name_nepali="स"
        )
        CourtCase.objects.using("ngm").create(
            court=court, case_number="081-CR-0081", status="enriched",
            verdict_date_bs="**** ** **",
        )
        CaseEntity.objects.using("ngm").create(
            court=court, case_number="081-CR-0081", side="defendant",
            name="Shyam", nes_id="entity:person/shyam",
        )
        res = CourtCaseImporter(
            ImportConfig(mode=ImportMode.INPLACE, courts=["supreme"])
        ).run()
        # nes_id is not imported/re-keyed → left exactly as it was.
        ent = CaseEntity.objects.using("ngm").get(name="Shyam")
        self.assertEqual(ent.nes_id, "entity:person/shyam")
        case = CourtCase.objects.using("ngm").get(court_id="supreme", case_number="081-CR-0081")
        # scraper-owned status NEVER overwritten in inplace.
        self.assertEqual(case.status, "enriched")
        self.assertEqual(res.dq_verdict_nulled, 1)

    def test_inplace_dry_run_is_read_only_but_counts(self):
        court = Court.objects.using("ngm").create(
            identifier="supreme", court_type="supreme", full_name_nepali="स"
        )
        CourtCase.objects.using("ngm").create(
            court=court, case_number="081-CR-0081", verdict_date_bs="**** ** **",
        )
        CaseEntity.objects.using("ngm").create(
            court=court, case_number="081-CR-0081", side="defendant",
            name="Shyam", nes_id="entity:person/shyam",
        )
        res = CourtCaseImporter(
            ImportConfig(mode=ImportMode.INPLACE, courts=["supreme"], dry_run=True)
        ).run()
        # the DQ counter populates …
        self.assertEqual(res.dq_verdict_nulled, 1)
        self.assertEqual(res.upserted, 1)
        # … but NOTHING is persisted (read-only: no UPDATE issued).
        ent = CaseEntity.objects.using("ngm").get(name="Shyam")
        self.assertEqual(ent.nes_id, "entity:person/shyam")


class SignalAndReindexTests(_NgmTestCase):
    def test_signals_muted_then_restored(self):
        from django.db.models.signals import post_save
        from courts import signals as s

        uid = "ngm_courtcase_search_index"
        imp = CourtCaseImporter(ImportConfig(mode=ImportMode.INPLACE))
        with imp._signals_muted():
            # disconnected inside → a probe disconnect finds nothing.
            self.assertFalse(post_save.disconnect(dispatch_uid=uid, sender=CourtCase))
        # reconnected outside → a probe disconnect now succeeds.
        self.assertTrue(post_save.disconnect(dispatch_uid=uid, sender=CourtCase))
        # restore global signal state for the rest of the suite.
        post_save.connect(s._index_courtcase, sender=CourtCase, dispatch_uid=uid)

    def test_reindex_drives_the_shared_driver_with_subset(self):
        imp = CourtCaseImporter(
            ImportConfig(
                mode=ImportMode.INPLACE, courts=["supreme"],
                since="2026-06-01T00:00:00+00:00",
            )
        )
        with mock.patch(
            "jawafdehi_shared.search.reindex.reindex",
            return_value={"indexed": 0, "skipped": 0},
        ) as m:
            imp.reindex(rebuild=False)
        self.assertEqual(m.call_count, 1)
        from jawafdehi_shared.search.opensearch import COURTCASE_INDEX

        self.assertEqual(m.call_args.kwargs["index"], COURTCASE_INDEX)
        self.assertIs(m.call_args.kwargs["build_doc"], search_index.build_doc)
        self.assertFalse(m.call_args.kwargs["rebuild"])


class CommandArgValidationTests(_NgmTestCase):
    def test_copy_requires_source_dsn(self):
        with self.assertRaises(CommandError):
            call_command("import_courtcases", "--all-courts", "--mode", "copy")

    def test_court_and_all_courts_mutually_exclusive(self):
        with self.assertRaises(CommandError):
            call_command("import_courtcases", "--court", "supreme", "--all-courts")

    def test_requires_one_of_court_or_all(self):
        with self.assertRaises(CommandError):
            call_command("import_courtcases")

    def test_rebuild_requires_yes(self):
        with self.assertRaises(CommandError):
            call_command(
                "import_courtcases", "--all-courts", "--reindex", "rebuild"
            )

    def test_inplace_empty_db_exits_clean(self):
        out = StringIO()
        call_command(
            "import_courtcases", "--all-courts", "--mode", "inplace",
            "--reindex", "none", "--json", stdout=out,
        )
        self.assertIn('"scanned": 0', out.getvalue())
