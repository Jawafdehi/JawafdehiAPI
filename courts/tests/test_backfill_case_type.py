"""Replacing a coarse case_type with the real charge.

The command exists because ``case_type`` is outside ``_ENRICH_COLUMNS`` by design,
so no amount of re-enrichment reaches the 46k Supreme rows that hold only
"criminal"/"civil". The tests pin the two properties that make it safe to point at
a live 1.6M-row corpus: it rewrites nothing but the label, and it never makes a row
worse than it found it.
"""

from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from courts.models import CaseEntity, Court, CourtCase
from courts.scraper.registry import REGISTRY
from courts.scraper.rows import ParsedEnrichment

NES_IRI = "https://jawafdehi.org/entity/person/ram-bahadur"


def _page(charge="घुस", klass="फौजदारी"):
    return ParsedEnrichment(
        core_fields={"case_type": charge, "case_subject": charge},
        extra_data={"case_class": klass, "enrichment_hearings": []},
        entities=[{"side": "defendant", "name": "ख", "address": None}],
    )


class BackfillCaseTypeTests(TestCase):
    databases = "__all__"

    def setUp(self):
        Court.objects.using("ngm").get_or_create(
            identifier="supreme", defaults={"court_type": "", "full_name_nepali": ""}
        )

    def _case(self, case_number, case_type="फौजदारी", **over):
        return CourtCase.objects.using("ngm").create(
            court_id="supreme", case_number=case_number, case_type=case_type, **over
        )

    def _run(self, *args, page=None, side_effect=None):
        out = StringIO()
        with mock.patch("courts.scraper.fetch.Fetcher"), \
             mock.patch.object(REGISTRY["supreme"], "crawl_detail",
                               side_effect=side_effect,
                               **({} if side_effect else {"return_value": page or _page()})):
            call_command("backfill_case_type", "--court", "supreme", *args,
                         stdout=out, stderr=out)
        return out.getvalue()

    def test_selects_only_rows_holding_a_class(self):
        self._case("081-CR-0001")                              # फौजदारी  -> selected
        self._case("081-CR-0002", case_type="देवानी")           # selected
        self._case("081-CR-0003", case_type="घुस")              # already a charge
        out = self._run("--delay", "0")
        assert "2 row(s)" in out
        assert "081-CR-0003" not in out

    def test_dry_run_is_the_default_and_fetches_nothing(self):
        self._case("081-CR-0001")
        with mock.patch.object(REGISTRY["supreme"], "crawl_detail") as detail:
            out = self._run("--delay", "0")
        detail.assert_not_called()
        assert "dry run" in out

    def test_apply_replaces_the_class_with_the_charge(self):
        self._case("081-CR-0001")
        out = self._run("--apply", "--delay", "0")
        case = CourtCase.objects.using("ngm").get(case_number="081-CR-0001")
        assert case.case_type == "घुस"
        assert case.case_subject == "घुस"
        assert case.extra_data["case_class"] == "फौजदारी", "the class is kept, not lost"
        assert "rewrote 1" in out

    def test_a_page_with_no_finer_label_leaves_the_row_alone(self):
        # Rewriting फौजदारी with फौजदारी would churn the row and its search doc
        # for no gain.
        self._case("081-CR-0001")
        out = self._run("--apply", "--delay", "0", page=_page(charge="फौजदारी"))
        assert CourtCase.objects.using("ngm").get(case_number="081-CR-0001").case_type == "फौजदारी"
        assert "rewrote 0" in out

    def test_it_never_touches_parties(self):
        """The nes_id guard, again.

        This command deliberately does NOT call apply_enrichment — that would run
        _replace_entities, which drops every party row and recreates it without
        nes_id. Only 607 such links exist corpus-wide.
        """
        self._case("081-CR-0001")
        CaseEntity.objects.using("ngm").create(
            court_id="supreme", case_number="081-CR-0001",
            side="defendant", name="क", nes_id=NES_IRI,
        )
        self._run("--apply", "--delay", "0")
        rows = CaseEntity.objects.using("ngm").filter(case_number="081-CR-0001")
        assert rows.count() == 1, "the page's party list must not be applied"
        assert rows.first().nes_id == NES_IRI

    def test_it_never_touches_status(self):
        self._case("081-CR-0001", status="enriched")
        self._run("--apply", "--delay", "0")
        assert CourtCase.objects.using("ngm").get(case_number="081-CR-0001").status == "enriched"

    def test_series_narrows_the_run(self):
        self._case("081-CR-0001")
        self._case("081-RI-0001")
        assert "1 row(s)" in self._run("--series", "cr", "--delay", "0")

    def test_a_portal_error_does_not_abandon_the_rest(self):
        self._case("081-CR-0001")
        self._case("081-CR-0002")
        calls = []

        def flaky(fetch, court_id, case_number):
            calls.append(case_number)
            if len(calls) == 1:
                raise RuntimeError("portal blew up")
            return _page()

        out = self._run("--apply", "--delay", "0", side_effect=flaky)
        assert len(calls) == 2
        assert "rewrote 1" in out and "failed 1" in out

    def test_a_repaired_row_drops_out_of_the_next_run(self):
        # Resumability comes from the filter itself, not from bookkeeping.
        self._case("081-CR-0001")
        self._run("--apply", "--delay", "0")
        assert "0 row(s)" in self._run("--delay", "0")

    def test_all_is_rejected(self):
        with self.assertRaises(CommandError):
            call_command("backfill_case_type", "--court", "all", stdout=StringIO())
