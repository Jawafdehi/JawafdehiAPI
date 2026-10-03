"""The offline backfill that projects stored hearing JSON into relational rows.

The hearings this command recovers were never lost — a key-name mismatch in
``materialise_detail_hearings`` meant supreme and district payloads produced no
rows, silently. These tests pin the two properties that make re-running it over
a live 180k-case corpus safe: it only ever adds, and a second pass is a no-op.
"""

from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from courts.models import Court, CourtCase, CourtCaseHearing

SUPREME_HEARINGS = [
    {
        "date": "2078-11-03",
        "type": "hearing",
        "judges": "मा.न्या. श्री दीपककुमार कार्की,",
        "status": "हेर्न नभ्याइने",
    },
    {
        "date": "2082-03-20",
        "type": "hearing",
        "judges": "मा.न्या. श्री सपना प्रधान मल्ल,",
        "status": "फैसला",
        "order_type": "सदर",
    },
]


class MaterialiseSweptHearingsTests(TestCase):
    databases = "__all__"

    def setUp(self):
        Court.objects.using("ngm").get_or_create(
            identifier="supreme", defaults={"court_type": "", "full_name_nepali": ""}
        )

    def _case(self, case_number, hearings):
        return CourtCase.objects.using("ngm").create(
            court_id="supreme",
            case_number=case_number,
            status="enriched",
            extra_data={"source": "register_sweep", "enrichment_hearings": hearings},
        )

    def _run(self, *args):
        out = StringIO()
        call_command("materialise_swept_hearings", *args, stdout=out, stderr=out)
        return out.getvalue()

    def _hearings(self, case_number="078-CR-0041"):
        return CourtCaseHearing.objects.using("ngm").filter(
            court_id="supreme", case_number=case_number
        )

    def test_dry_run_reports_without_writing(self):
        self._case("078-CR-0041", SUPREME_HEARINGS)
        out = self._run("--court", "supreme")
        assert "up to 2 rows would be written" in out
        assert "dry run" in out
        assert not self._hearings().exists()

    def test_apply_writes_the_stored_hearings(self):
        self._case("078-CR-0041", SUPREME_HEARINGS)
        out = self._run("--court", "supreme", "--apply")
        assert self._hearings().count() == 2
        assert "2 rows written" in out
        decided = self._hearings().get(hearing_date_bs="2082-03-20")
        assert decided.decision_type == "सदर"
        assert decided.judge_names == "मा.न्या. श्री सपना प्रधान मल्ल,"
        assert decided.extra_data["source"] == "register_sweep"

    def test_second_pass_writes_nothing(self):
        # The run is resumable and re-runnable: a Job that dies halfway can be
        # restarted without double-writing the cases it already covered.
        self._case("078-CR-0041", SUPREME_HEARINGS)
        self._run("--court", "supreme", "--apply")
        out = self._run("--court", "supreme", "--apply")
        assert self._hearings().count() == 2
        assert "0 rows written" in out

    def test_leaves_a_hearing_the_causelist_already_wrote_alone(self):
        # Additive only. The cause-list row is richer (it has a serial_no); the
        # backfill must not overwrite it with the thinner detail-page version.
        self._case("078-CR-0041", SUPREME_HEARINGS)
        CourtCaseHearing.objects.using("ngm").create(
            court_id="supreme",
            case_number="078-CR-0041",
            hearing_date_bs="2078-11-03",
            hearing_date_ad="2022-02-15",
            serial_no="12",
            scraped_at="2026-01-19T00:00:00Z",
        )
        self._run("--court", "supreme", "--apply")
        assert self._hearings().count() == 2
        kept = self._hearings().get(hearing_date_bs="2078-11-03")
        assert kept.serial_no == "12"

    def test_skips_cases_with_no_stored_hearings(self):
        # Most of the FY2081-2082 tail: registered, not yet listed, empty list.
        # Nothing to project, and that is not a defect to report.
        self._case("082-CR-0377", [])
        out = self._run("--court", "supreme", "--apply")
        assert "0 cases scanned" in out

    def test_flags_a_payload_shape_the_aliases_do_not_cover(self):
        # A parser emitting a new key name must surface as a warning, not as a
        # quiet zero — that silence is the original bug.
        self._case("079-CR-0003", [{"मिती": "2079-06-10"}])
        out = self._run("--court", "supreme")
        assert "1 entries carry no recognised date key" in out
        assert "alias tuples" in out

    def test_rejects_a_court_id_outside_the_named_tier(self):
        with self.assertRaises(CommandError):
            self._run("--court", "supreme", "--court-id", "patanhc")
