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

    def test_case_type_normalized_with_non_dict_extra_data(self):
        # A malformed (non-dict) extra_data must not crash the row: case_type is
        # still normalised, the raw archive is skipped, and extra_data is untouched.
        row = _src_row(
            case="081-CR-0302", case_type="080-cp-1852 लेनदेन", extra_data=["junk"]
        )
        res = _copy([row]).run()
        case = CourtCase.objects.using("ngm").get(
            court_id="supreme", case_number="081-CR-0302"
        )
        self.assertEqual(case.case_type, "लेनदेन")
        self.assertEqual(case.extra_data, ["junk"])
        self.assertEqual(res.failed, 0)
        self.assertEqual(res.dq_case_type_normalized, 1)

    def test_case_type_normalized_with_non_dict_dq(self):
        # A malformed non-dict _dq must not crash the row: it is replaced with a
        # proper dict carrying the archived raw value.
        row = _src_row(
            case="081-CR-0303", case_type="080-cp-1852 लेनदेन",
            extra_data={"_dq": "junk", "keep": 1},
        )
        res = _copy([row]).run()
        case = CourtCase.objects.using("ngm").get(
            court_id="supreme", case_number="081-CR-0303"
        )
        self.assertEqual(case.case_type, "लेनदेन")
        self.assertEqual(case.extra_data["_dq"]["case_type_raw"], "080-cp-1852 लेनदेन")
        self.assertEqual(case.extra_data["keep"], 1)  # other keys preserved
        self.assertEqual(res.failed, 0)
        self.assertEqual(res.dq_case_type_normalized, 1)

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


class VerdictJudgeDeriveTests(_NgmTestCase):
    _VDATE = date(2024, 6, 1)

    def test_derives_from_verdict_date_hearing_and_de_run_ons(self):
        # Decided case, no verdict_judge; the verdict-date hearing carries the bench
        # (glued, as legacy hearings are) → filled, de-run-on to ", "-separated.
        row = _src_row(
            case="081-CR-0091", verdict_date_ad=self._VDATE, verdict_judge=None,
            hearings=[
                _hearing(hearing_date_ad=date(2024, 5, 15), judge_names="मा. न्या. श्री क"),
                _hearing(
                    hearing_date_ad=self._VDATE, decision_type="फैसला",
                    judge_names="मा. न्या. श्री राममा. न्या. श्री श्याम",
                ),
            ],
        )
        res = _copy([row]).run()
        case = CourtCase.objects.using("ngm").get(case_number="081-CR-0091")
        self.assertEqual(case.verdict_judge, "मा. न्या. श्री राम, मा. न्या. श्री श्याम")
        self.assertTrue(case.extra_data["_dq"]["verdict_judge_derived"])
        self.assertEqual(res.dq_verdict_judge_derived, 1)

    def test_does_not_overwrite_existing_verdict_judge(self):
        row = _src_row(
            case="081-CR-0092", verdict_date_ad=self._VDATE,
            verdict_judge="मा. न्या. श्री मौजुदा",
            hearings=[_hearing(hearing_date_ad=self._VDATE, judge_names="मा. न्या. श्री अर्को")],
        )
        res = _copy([row]).run()
        case = CourtCase.objects.using("ngm").get(case_number="081-CR-0092")
        self.assertEqual(case.verdict_judge, "मा. न्या. श्री मौजुदा")
        self.assertEqual(res.dq_verdict_judge_derived, 0)

    def test_undecided_or_no_matching_hearing_leaves_null(self):
        # Not decided (no verdict_date_ad) → skip; and decided but the only hearing
        # with a judge is on a DIFFERENT date → nothing to derive.
        undecided = _src_row(case="081-CR-0093", verdict_date_ad=None, verdict_judge=None,
                             hearings=[_hearing(hearing_date_ad=self._VDATE, judge_names="मा. न्या. श्री क")])
        no_hit = _src_row(case="081-CR-0094", verdict_date_ad=self._VDATE, verdict_judge=None,
                          hearings=[_hearing(hearing_date_ad=date(2024, 1, 1), judge_names="मा. न्या. श्री क")])
        res = _copy([undecided, no_hit]).run()
        self.assertIsNone(CourtCase.objects.using("ngm").get(case_number="081-CR-0093").verdict_judge)
        self.assertIsNone(CourtCase.objects.using("ngm").get(case_number="081-CR-0094").verdict_judge)
        self.assertEqual(res.dq_verdict_judge_derived, 0)

    def test_prefers_decisive_sitting_when_several_on_verdict_date(self):
        row = _src_row(
            case="081-CR-0095", verdict_date_ad=self._VDATE, verdict_judge=None,
            hearings=[
                _hearing(hearing_date_ad=self._VDATE, case_status="पेशी",
                         judge_names="मा. न्या. श्री पेशी"),
                _hearing(hearing_date_ad=self._VDATE, decision_type="फैसला",
                         judge_names="मा. न्या. श्री फैसला"),
            ],
        )
        _copy([row]).run()
        case = CourtCase.objects.using("ngm").get(case_number="081-CR-0095")
        self.assertEqual(case.verdict_judge, "मा. न्या. श्री फैसला")

    def test_inplace_derives_from_db_hearing_and_is_idempotent(self):
        court = Court.objects.using("ngm").create(
            identifier="special", court_type="special", full_name_nepali="वि"
        )
        CourtCase.objects.using("ngm").create(
            court=court, case_number="082-CR-0007", verdict_date_ad=self._VDATE,
            verdict_judge=None,
        )
        CourtCaseHearing.objects.using("ngm").create(
            court=court, case_number="082-CR-0007", hearing_date_ad=self._VDATE,
            decision_type="अन्तिम आदेश", judge_names="मा. न्या. श्री एकमा. न्या. श्री दुई",
            scraped_at=_SCRAPED,
        )
        cfg = dict(mode=ImportMode.INPLACE, courts=["special"])
        first = CourtCaseImporter(ImportConfig(**cfg)).run()
        self.assertEqual(first.dq_verdict_judge_derived, 1)
        case = CourtCase.objects.using("ngm").get(case_number="082-CR-0007")
        self.assertEqual(case.verdict_judge, "मा. न्या. श्री एक, मा. न्या. श्री दुई")
        self.assertTrue(case.extra_data["_dq"]["verdict_judge_derived"])
        # Re-run finds it populated → no further derivation.
        second = CourtCaseImporter(ImportConfig(**cfg)).run()
        self.assertEqual(second.dq_verdict_judge_derived, 0)


class HearingVerdictPromoteTests(_NgmTestCase):
    """DQ guard (6) — decided on the hearing sheet, still "ongoing" on the row.

    Measured on prod 2026-08-04: 105 special-court cases in exactly this state,
    104 of them decided during 2026, with ``updated_at`` AFTER the verdict — the
    scraper touched the row and still left it reading ``चलिरहेको``, so a stale
    ``updated_at`` is not the tell.

    ``case_events.producers.dockets.verdict_signals`` selects on
    ``verdict_date_ad__isnull=False``, so none of those judgments could raise a
    docket signal, and ``hearing_signals``' 48h stateless window had long closed.
    Three of the 104 back PUBLISHED Jawafdehi cases still calling themselves
    विचाराधीन weeks after judgment.
    """

    def _special(self, case, hearings, **over):
        row = _src_row(
            court="special", court_type="special", case=case,
            hearings=hearings, **over,
        )
        return _copy([row], courts=["special"]).run()

    def _get(self, case):
        return CourtCase.objects.using("ngm").get(case_number=case)

    def test_a_decided_case_stops_reading_as_ongoing(self):
        """081-CR-0142 as it actually stands in prod today."""
        res = self._special(
            "081-CR-0142",
            [
                _hearing(hearing_date_bs="2083-03-29", hearing_date_ad=date(2026, 7, 13),
                         case_status="आदेश", decision_type="हेर्दा हेर्दै भोलीलाई"),
                _hearing(hearing_date_bs="2083-03-30", hearing_date_ad=date(2026, 7, 14),
                         case_status="फैसला", decision_type="आंशिक ठहर"),
            ],
        )
        case = self._get("081-CR-0142")
        self.assertEqual(res.dq_hearing_verdict_promoted, 1)
        self.assertEqual(case.verdict_date_ad, date(2026, 7, 14))
        self.assertEqual(case.verdict_date_bs, "2083-03-30")
        self.assertEqual(case.case_status, "फैसला (मिती: 2083-03-30)")
        self.assertTrue(case.extra_data["_dq"]["verdict_promoted_from_hearing"])
        self.assertEqual(case.extra_data["_dq"]["case_status_raw"], "चालु")

    def test_a_partial_conviction_is_not_promoted_as_a_full_one(self):
        """Why this guard must land AFTER the आंशिक/ठहर ordering fix: with ठहर
        matched first, 081-CR-0142 becomes a full CONVICTED — the error 593
        existing rows already carry."""
        self._special(
            "081-CR-0142",
            [_hearing(hearing_date_bs="2083-03-30", hearing_date_ad=date(2026, 7, 14),
                      case_status="फैसला", decision_type="आंशिक ठहर")],
        )
        self.assertEqual(self._get("081-CR-0142").verdict_type, "PARTIALLY_CONVICTED")

    def test_the_other_two_live_dockets(self):
        """081-CR-0058 (ठहर, convicted) and 081-CR-0098 (सफाई, acquitted). Loaded in
        one COPY run — a second one would refuse the now non-empty target."""
        expectations = {"081-CR-0058": ("ठहर", "CONVICTED"),
                        "081-CR-0098": ("सफाई", "ACQUITTED")}
        rows = [
            _src_row(
                court="special", court_type="special", case=number,
                hearings=[_hearing(hearing_date_bs="2083-03-32",
                                   hearing_date_ad=date(2026, 7, 16),
                                   case_status="फैसला", decision_type=decision)],
            )
            for number, (decision, _) in expectations.items()
        ]
        res = _copy(rows, courts=["special"]).run()

        self.assertEqual(res.dq_hearing_verdict_promoted, 2)
        for number, (_, expected) in expectations.items():
            with self.subTest(case=number):
                case = self._get(number)
                self.assertEqual(case.verdict_type, expected)
                self.assertEqual(case.verdict_date_ad, date(2026, 7, 16))

    def test_the_rows_own_ad_date_beats_reconverting_the_bs_one(self):
        """``bs_to_ad`` differs from the gazette by a day on some BS 2083 dates,
        and the scraper's own hearing_date_ad is the recorded fact."""
        self._special(
            "081-CR-0200",
            [_hearing(hearing_date_bs="2083-03-30", hearing_date_ad=date(2026, 1, 1),
                      case_status="फैसला", decision_type="ठहर")],
        )
        self.assertEqual(self._get("081-CR-0200").verdict_date_ad, date(2026, 1, 1))

    def test_the_last_disposing_sitting_wins(self):
        """A case can be decided, reopened on review, and decided again."""
        self._special(
            "081-CR-0201",
            [
                _hearing(hearing_date_bs="2081-01-01", hearing_date_ad=date(2024, 4, 13),
                         case_status="फैसला", decision_type="ठहर"),
                _hearing(hearing_date_bs="2083-03-30", hearing_date_ad=date(2026, 7, 14),
                         case_status="फैसला", decision_type="सफाई"),
            ],
        )
        case = self._get("081-CR-0201")
        self.assertEqual(case.verdict_date_ad, date(2026, 7, 14))
        self.assertEqual(case.verdict_type, "ACQUITTED")

    def test_a_genuinely_pending_case_is_left_pending(self):
        """The failure that would matter most: marking a live case decided."""
        res = self._special(
            "081-CR-0202",
            [_hearing(hearing_date_bs="2083-03-29", hearing_date_ad=date(2026, 7, 13),
                      case_status="आदेश", decision_type="हेर्दा हेर्दै भोलीलाई")],
        )
        case = self._get("081-CR-0202")
        self.assertEqual(res.dq_hearing_verdict_promoted, 0)
        self.assertIsNone(case.verdict_date_ad)
        self.assertEqual(case.case_status, "चालु")

    def test_an_unrecognised_disposition_yields_nothing(self):
        res = self._special(
            "081-CR-0203",
            [_hearing(hearing_date_bs="2083-03-30", hearing_date_ad=date(2026, 7, 14),
                      case_status="फैसला", decision_type="कुनै अज्ञात कारबाही")],
        )
        self.assertEqual(res.dq_hearing_verdict_promoted, 0)
        self.assertIsNone(self._get("081-CR-0203").verdict_date_ad)

    def test_an_existing_verdict_is_never_clobbered(self):
        res = self._special(
            "081-CR-0204",
            [_hearing(hearing_date_bs="2083-03-30", hearing_date_ad=date(2026, 7, 14),
                      case_status="फैसला", decision_type="सफाई")],
            verdict_date_ad=date(2024, 1, 1), verdict_date_bs="2080-09-16",
            verdict_type="CONVICTED",
        )
        case = self._get("081-CR-0204")
        self.assertEqual(res.dq_hearing_verdict_promoted, 0)
        self.assertEqual(case.verdict_date_ad, date(2024, 1, 1))
        self.assertEqual(case.verdict_type, "CONVICTED")

    def test_an_unrecognised_case_status_is_preserved(self):
        """Only a value the parser KNOWS to be pending is replaced; anything else
        is left for a human rather than guessed at."""
        self._special(
            "081-CR-0205",
            [_hearing(hearing_date_bs="2083-03-30", hearing_date_ad=date(2026, 7, 14),
                      case_status="फैसला", decision_type="ठहर")],
            case_status="कुनै अपरिचित अवस्था",
        )
        case = self._get("081-CR-0205")
        self.assertEqual(case.verdict_date_ad, date(2026, 7, 14))
        self.assertEqual(case.case_status, "कुनै अपरिचित अवस्था")

    def test_a_re_run_promotes_nothing_further(self):
        """Idempotence over the INPLACE path, which is what a real re-import uses
        (a second COPY would refuse the non-empty target)."""
        first = self._special(
            "081-CR-0206",
            [_hearing(hearing_date_bs="2083-03-30", hearing_date_ad=date(2026, 7, 14),
                      case_status="फैसला", decision_type="ठहर")],
        )
        self.assertEqual(first.dq_hearing_verdict_promoted, 1)

        second = CourtCaseImporter(
            ImportConfig(mode=ImportMode.INPLACE, courts=["special"])
        ).run()
        self.assertEqual(second.dq_hearing_verdict_promoted, 0)


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

    def test_inplace_case_type_normalization_is_idempotent(self):
        court = Court.objects.using("ngm").create(
            identifier="supreme", court_type="supreme", full_name_nepali="स"
        )
        CourtCase.objects.using("ngm").create(
            court=court, case_number="081-CR-0400", status="enriched",
            case_type="080-cp-1852 लेनदेन",
        )
        cfg = dict(mode=ImportMode.INPLACE, courts=["supreme"])
        first = CourtCaseImporter(ImportConfig(**cfg)).run()
        self.assertEqual(first.dq_case_type_normalized, 1)
        case = CourtCase.objects.using("ngm").get(
            court_id="supreme", case_number="081-CR-0400"
        )
        self.assertEqual(case.case_type, "लेनदेन")
        self.assertEqual(case.extra_data["_dq"]["case_type_raw"], "080-cp-1852 लेनदेन")

        # Second pass: already canonical → no rewrite, no re-archive, not re-counted.
        second = CourtCaseImporter(ImportConfig(**cfg)).run()
        self.assertEqual(second.dq_case_type_normalized, 0)
        case = CourtCase.objects.using("ngm").get(
            court_id="supreme", case_number="081-CR-0400"
        )
        self.assertEqual(case.case_type, "लेनदेन")
        self.assertEqual(case.extra_data["_dq"]["case_type_raw"], "080-cp-1852 लेनदेन")


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
