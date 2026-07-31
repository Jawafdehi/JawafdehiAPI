"""The repair pass for cases falsely marked ``enriched``.

The empty-parse bug set ``status="enriched"`` on cases it wrote nothing to, and
``_enrich_pending`` excludes that status — so fixing the parser does not reach
them. These tests pin the selection (only rows with no enrichment evidence) and
the safety property that matters: a case the portal still can't enrich is left
exactly as it was.
"""

from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from courts.models import CaseEntity, Court, CourtCase
from courts.scraper.registry import REGISTRY
from courts.scraper.rows import ParsedEnrichment


def _enrichment():
    return ParsedEnrichment(
        core_fields={"registration_number": "0294", "hearing_count": 2},
        extra_data={"enrichment_hearings": [{"hearing_date": "2080-01-15"}]},
        entities=[{"side": "defendant", "name": "क", "address": None}],
    )


class RepairEnrichmentTests(TestCase):
    databases = "__all__"

    def setUp(self):
        Court.objects.using("ngm").get_or_create(
            identifier="special", defaults={"court_type": "", "full_name_nepali": ""}
        )

    def _case(self, case_number, **over):
        fields = dict(status="enriched", extra_data={"enrichment_hearings": []})
        fields.update(over)
        return CourtCase.objects.using("ngm").create(
            court_id="special", case_number=case_number, **fields
        )

    def _run(self, *args):
        out = StringIO()
        call_command("repair_enrichment", "--court", "special", *args, stdout=out, stderr=out)
        return out.getvalue()

    def test_finds_only_cases_with_no_enrichment_evidence(self):
        self._case("076-CR-0001")                                  # damaged
        self._case("076-CR-0002", extra_data={})                   # damaged: key absent
        self._case("076-CR-0003", registration_number="0003")      # real enrichment
        self._case("076-CR-0004", hearing_count=5)                 # real enrichment
        self._case("076-CR-0005", extra_data={"enrichment_hearings": [{"a": 1}]})
        self._case("076-CR-0006", status=None)                     # _enrich_pending still reaches it

        out = self._run()
        assert "2 case(s)" in out
        assert "076-CR-0001" in out and "076-CR-0002" in out
        assert "076-CR-0003" not in out

    def test_dry_run_is_the_default_and_writes_nothing(self):
        self._case("076-CR-0001")
        with mock.patch.object(REGISTRY["special"], "crawl_detail") as detail:
            out = self._run()
        detail.assert_not_called()
        assert "dry run" in out
        assert CourtCase.objects.using("ngm").get(case_number="076-CR-0001").registration_number is None

    def test_apply_re_enriches_the_damaged_case(self):
        self._case("076-CR-0001")
        with mock.patch("courts.management.commands.repair_enrichment.Fetcher"), \
             mock.patch.object(REGISTRY["special"], "crawl_detail", return_value=_enrichment()):
            out = self._run("--apply", "--delay", "0")
        assert "repaired 1" in out
        case = CourtCase.objects.using("ngm").get(case_number="076-CR-0001")
        assert case.registration_number == "0294"
        assert case.extra_data["enrichment_hearings"]
        assert CaseEntity.objects.using("ngm").filter(case_number="076-CR-0001").count() == 1

    def test_a_case_the_portal_still_cannot_enrich_is_left_untouched(self):
        # The whole point of the guard being fixed: an empty parse must not
        # rewrite the row. It stays selectable for a later run.
        self._case("076-CR-0001", extra_data={"enrichment_hearings": [], "keep": "me"})
        empty = ParsedEnrichment(
            core_fields={}, extra_data={"enrichment_hearings": []}, entities=[]
        )
        with mock.patch("courts.management.commands.repair_enrichment.Fetcher"), \
             mock.patch.object(REGISTRY["special"], "crawl_detail", return_value=empty):
            out = self._run("--apply", "--delay", "0")
        assert "repaired 0" in out
        case = CourtCase.objects.using("ngm").get(case_number="076-CR-0001")
        assert case.extra_data["keep"] == "me"
        assert case.status == "enriched"

    def test_a_portal_error_does_not_abandon_the_remaining_cases(self):
        self._case("076-CR-0001")
        self._case("076-CR-0002")
        calls = []

        def flaky(fetch, court_id, case_number):
            calls.append(case_number)
            if len(calls) == 1:
                raise RuntimeError("portal blew up")
            return _enrichment()

        with mock.patch("courts.management.commands.repair_enrichment.Fetcher"), \
             mock.patch.object(REGISTRY["special"], "crawl_detail", side_effect=flaky):
            out = self._run("--apply", "--delay", "0")
        assert len(calls) == 2
        assert "repaired 1" in out

    def test_limit_caps_the_run(self):
        for seq in range(1, 6):
            self._case(f"076-CR-{seq:04d}")
        assert "2 case(s)" in self._run("--limit", "2")

    def test_all_is_rejected(self):
        with self.assertRaises(CommandError):
            call_command("repair_enrichment", "--court", "all", stdout=StringIO())
