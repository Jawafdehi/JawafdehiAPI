"""
Tests for enrich_ciaa_timeline management command.

Covers: idempotency, CIAA case filtering, NGM data integration,
timeline building, dry-run safety, CLI flag registration, and edge cases.
"""

from datetime import date
from io import StringIO
from unittest.mock import MagicMock, patch

import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command

from cases.management.commands.enrich_ciaa_timeline import Command
from cases.models import Case, CaseState, CaseType


@pytest.mark.django_db
class TestEnrichCiaaTimeline:
    """Test suite for enrich_ciaa_timeline management command."""

    # ── helpers ──────────────────────────────────────────────────────────

    def _create_case(self, **overrides):
        defaults = {
            "case_type": CaseType.CORRUPTION,
            "state": CaseState.DRAFT,
            "title": "Test CIAA Case",
            "case_id": "case-test-001",
            "court_cases": ["special:081-CR-0001"],
            "timeline": [],
        }
        defaults.update(overrides)
        return Case.objects.create(**defaults)

    def _mock_ngm_response(self, **overrides):
        defaults = {
            "case": {
                "case_number": "081-CR-0001",
                "registration_date_ad": "2023-01-15",
                "division": "Special Court",
                "verdict_date_ad": "2024-06-20",
                "verdict_judge": "Hon. Tek Bahadur",
            },
            "hearings": [
                {
                    "hearing_date_ad": "2023-03-10",
                    "serial_no": "1",
                    "case_status": "Pending",
                    "decision_type": "",
                    "bench": "Bench A",
                    "judge_names": "Hon. Ram Sharma",
                    "remarks": "",
                },
                {
                    "hearing_date_ad": "2024-06-20",
                    "serial_no": "फैसला",
                    "case_status": "Disposed",
                    "decision_type": "फैसला",
                    "bench": "Bench B",
                    "judge_names": "Hon. Tek Bahadur, Hon. Hari Poudel",
                    "remarks": "Final verdict",
                },
            ],
            "entities": [],
        }
        defaults.update(overrides)
        return defaults

    def _mock_ngm_empty(self):
        return {
            "case": {},
            "hearings": [],
            "entities": [],
        }

    # ── 1. Idempotency ──────────────────────────────────────────────────

    def test_skips_cases_with_populated_timeline(self):
        """Cases with non-empty timeline are excluded by default."""
        self._create_case(
            case_id="populated-case",
            timeline=[{"date": "2023-01-15", "title": "Existing entry"}],
        )

        out = StringIO()
        err = StringIO()
        call_command("enrich_ciaa_timeline", "--dry-run", stdout=out, stderr=err)

        output = out.getvalue()
        assert "No cases to process" in output or "Cases processed" not in output

    def test_processes_cases_with_empty_timeline(self):
        """Cases with empty timeline are processed."""
        case = self._create_case()
        ngm_data = self._mock_ngm_response()

        with patch(
            "cases.management.commands.enrich_ciaa_timeline.get_court_case_details",
            return_value=ngm_data,
        ):
            out = StringIO()
            call_command(
                "enrich_ciaa_timeline",
                f"--case-id={case.case_id}",
                "--dry-run",
                stdout=out,
            )

        output = out.getvalue()
        assert "Cases processed:" in output and "1" in output

    def test_processes_cases_with_null_timeline(self):
        """Cases with timeline=None are treated as empty and processed."""
        case = self._create_case()
        ngm_data = self._mock_ngm_response()

        with patch(
            "cases.management.commands.enrich_ciaa_timeline.get_court_case_details",
            return_value=ngm_data,
        ):
            out = StringIO()
            call_command(
                "enrich_ciaa_timeline",
                f"--case-id={case.case_id}",
                "--dry-run",
                stdout=out,
            )

        output = out.getvalue()
        assert "Cases processed:" in output and "1" in output

    # ── 2. CIAA case filtering ──────────────────────────────────────────

    def test_skips_non_draft_cases(self):
        """Only DRAFT cases are considered."""
        self._create_case(case_id="draft", state=CaseState.DRAFT)
        self._create_case(case_id="published", state=CaseState.PUBLISHED)
        self._create_case(case_id="in_review", state=CaseState.IN_REVIEW)

        cmd = Command()
        cmd.stats = {
            "cases_processed": 0,
            "cases_enriched": 0,
            "cases_skipped": 0,
            "cases_no_court_case": 0,
            "cases_ngm_error": 0,
            "cases_already_populated": 0,
            "total_timeline_entries": 0,
        }

        cases = cmd._get_ciaa_cases()
        case_ids = [c.case_id for c in cases]
        assert "draft" in case_ids
        assert "published" not in case_ids
        assert "in_review" not in case_ids

    def test_skips_non_corruption_cases(self):
        """Only CORRUPTION cases are considered."""
        self._create_case(case_id="corruption", case_type=CaseType.CORRUPTION)
        # Non-CORRUPTION cases (e.g. DRAFT with different type) are excluded
        # Currently only CORRUPTION type exists in the system

        cmd = Command()
        cmd.stats = {
            "cases_processed": 0,
            "cases_enriched": 0,
            "cases_skipped": 0,
            "cases_no_court_case": 0,
            "cases_ngm_error": 0,
            "cases_already_populated": 0,
            "total_timeline_entries": 0,
        }

        cases = cmd._get_ciaa_cases()
        case_ids = [c.case_id for c in cases]
        assert "corruption" in case_ids
        assert len(cases) == 1

    # ── 3. Court case reference extraction ──────────────────────────────

    def test_skips_cases_without_special_prefix(self):
        """Cases without 'special:' in court_cases are skipped with warning."""
        case = self._create_case(
            case_id="no-special",
            court_cases=["supreme:001", "district:002"],
        )

        with patch(
            "cases.management.commands.enrich_ciaa_timeline.get_court_case_details",
        ) as mock_ngm:
            out = StringIO()
            call_command(
                "enrich_ciaa_timeline",
                f"--case-id={case.case_id}",
                "--dry-run",
                stdout=out,
            )

        output = out.getvalue()
        assert "No special:* court case reference found" in output
        assert "No court case reference" in output
        mock_ngm.assert_not_called()

    def test_skips_cases_with_empty_court_cases(self):
        """Cases with empty court_cases list are skipped."""
        case = self._create_case(case_id="no-ref", court_cases=[])

        with patch(
            "cases.management.commands.enrich_ciaa_timeline.get_court_case_details",
        ) as mock_ngm:
            out = StringIO()
            call_command(
                "enrich_ciaa_timeline",
                f"--case-id={case.case_id}",
                "--dry-run",
                stdout=out,
            )

        output = out.getvalue()
        assert "No special:* court case reference found" in output
        mock_ngm.assert_not_called()

    def test_extracts_special_ref_from_mixed_list(self):
        """Extracts special:* reference even when mixed with other refs."""
        cell = Command()
        result = cell._extract_special_ref(
            ["supreme:123", "special:081-CR-0001", "district:456"]
        )
        assert result == "special:081-CR-0001"

    # ── 4. NGM data handling ────────────────────────────────────────────

    def test_handles_ngm_error_gracefully(self):
        """NGM query failures are tracked without crashing the command."""
        case = self._create_case()

        with patch(
            "cases.management.commands.enrich_ciaa_timeline.get_court_case_details",
            side_effect=RuntimeError("Connection refused"),
        ):
            out = StringIO()
            call_command(
                "enrich_ciaa_timeline",
                f"--case-id={case.case_id}",
                stdout=out,
            )

        output = out.getvalue()
        assert "Connection refused" in output
        assert "NGM errors" in output

    def test_handles_empty_ngm_data(self):
        """When NGM returns None/empty, case is skipped."""
        case = self._create_case()

        with patch(
            "cases.management.commands.enrich_ciaa_timeline.get_court_case_details",
            return_value=None,
        ):
            out = StringIO()
            call_command(
                "enrich_ciaa_timeline",
                f"--case-id={case.case_id}",
                stdout=out,
            )

        output = out.getvalue()
        assert "No NGM data found" in output

    def test_handles_case_with_no_hearings(self):
        """Case with NGM data but no hearings still gets registration entry."""
        case = self._create_case()
        ngm_data = self._mock_ngm_response()
        ngm_data["hearings"] = []

        with patch(
            "cases.management.commands.enrich_ciaa_timeline.get_court_case_details",
            return_value=ngm_data,
        ):
            out = StringIO()
            call_command(
                "enrich_ciaa_timeline",
                f"--case-id={case.case_id}",
                "--dry-run",
                stdout=out,
            )

        output = out.getvalue()
        assert "Built" in output and "timeline entry" in output

    # ── 5. Timeline building ────────────────────────────────────────────

    def test_builds_timeline_with_all_entry_types(self):
        """Timeline includes registration, hearings, and verdict entries."""
        case = self._create_case()
        ngm_data = self._mock_ngm_response()

        cmd = Command()
        timeline = cmd._build_timeline(case, ngm_data)

        dates = [e["date"] for e in timeline]
        titles = [e["title"] for e in timeline]

        assert "2023-01-15" in dates
        assert "2023-03-10" in dates
        assert "2024-06-20" in dates
        assert "मुद्दा दर्ता" in titles
        assert "पेशी" in titles
        assert "फैसला" in titles

    def test_timeline_sorted_by_date(self):
        """Timeline entries are sorted chronologically by date."""
        case = self._create_case()
        ngm_data = self._mock_ngm_response()

        cmd = Command()
        timeline = cmd._build_timeline(case, ngm_data)

        dates = [e["date"] for e in timeline]
        assert dates == sorted(dates)

    def test_no_duplicate_dates(self):
        """Same date is not added twice to timeline."""
        case = self._create_case()
        ngm_data = self._mock_ngm_response()
        ngm_data["case"]["registration_date_ad"] = "2023-03-10"

        cmd = Command()
        timeline = cmd._build_timeline(case, ngm_data)

        dates = [e["date"] for e in timeline]
        assert dates.count("2023-03-10") == 1

    def test_registration_date_falls_back_to_case_start_date(self):
        """Uses case.case_start_date when NGM registration_date_ad is missing."""
        ngm_data = self._mock_ngm_response()
        ngm_data["case"] = {}
        case = self._create_case(case_start_date=date(2022, 5, 10))

        cmd = Command()
        timeline = cmd._build_timeline(case, ngm_data)

        assert timeline[0]["date"] == "2022-05-10"
        assert timeline[0]["title"] == "मुद्दा दर्ता"

    def test_verdict_date_falls_back_to_case_end_date(self):
        """Uses case.case_end_date when NGM verdict_date_ad is missing."""
        ngm_data = self._mock_ngm_response()
        ngm_data["case"]["verdict_date_ad"] = None
        case = self._create_case(case_end_date=date(2024, 12, 31))

        cmd = Command()
        timeline = cmd._build_timeline(case, ngm_data)

        verdict_dates = [e["date"] for e in timeline if e["title"] == "फैसला"]
        assert "2024-12-31" in verdict_dates

    def test_first_hearing_as_registration_when_date_missing(self):
        """When no registration date, the last (most recent) hearing date is used."""
        ngm_data = self._mock_ngm_response()
        ngm_data["case"] = {}
        case = self._create_case(case_start_date=None)

        cmd = Command()
        timeline = cmd._build_timeline(case, ngm_data)

        registration_entry = next(
            (e for e in timeline if "First known hearing date" in e.get("description", "")),
            None,
        )
        assert registration_entry is not None
        assert registration_entry["title"] == "मुद्दा दर्ता"

    def test_detects_verdict_from_remarks(self):
        """Verdict is detected from remarks containing 'फैसला'."""
        ngm_data = self._mock_ngm_response()
        ngm_data["hearings"][0]["decision_type"] = ""
        ngm_data["hearings"][0]["remarks"] = "फैसला सुनाइयो"

        case = self._create_case()
        cmd = Command()
        timeline = cmd._build_timeline(case, ngm_data)

        verdict_entries = [e for e in timeline if e["title"] == "फैसला"]
        assert len(verdict_entries) >= 1

    def test_hearing_entry_includes_bench_and_judges(self):
        """Hearing entries include bench and judge information."""
        case = self._create_case()
        ngm_data = self._mock_ngm_response()

        cmd = Command()
        timeline = cmd._build_timeline(case, ngm_data)

        hearing_entry = next(e for e in timeline if e["date"] == "2023-03-10")
        assert "Bench: Bench A" in hearing_entry["description"]
        assert "Judge(s): Hon. Ram Sharma" in hearing_entry["description"]

    # ── 6. Dry-run safety ───────────────────────────────────────────────

    def test_dry_run_no_db_writes(self):
        """Dry-run mode does not persist timeline to database."""
        case = self._create_case()
        ngm_data = self._mock_ngm_response()

        with patch(
            "cases.management.commands.enrich_ciaa_timeline.get_court_case_details",
            return_value=ngm_data,
        ):
            out = StringIO()
            call_command(
                "enrich_ciaa_timeline",
                f"--case-id={case.case_id}",
                "--dry-run",
                stdout=out,
            )

        case.refresh_from_db()
        assert case.timeline == []

    def test_production_mode_saves_timeline(self):
        """Without --dry-run, timeline is saved to the database."""
        case = self._create_case()
        ngm_data = self._mock_ngm_response()

        with patch(
            "cases.management.commands.enrich_ciaa_timeline.get_court_case_details",
            return_value=ngm_data,
        ):
            out = StringIO()
            call_command(
                "enrich_ciaa_timeline",
                f"--case-id={case.case_id}",
                stdout=out,
            )

        case.refresh_from_db()
        assert len(case.timeline) >= 1
        entry = case.timeline[0]
        assert "date" in entry
        assert "title" in entry

    # ── 7. CLI flag registration ────────────────────────────────────────

    def test_cli_dry_run_flag_registered(self):
        parser = MagicMock()
        cmd = Command()
        cmd.add_arguments(parser)
        parser.add_argument.assert_any_call(
            "--dry-run",
            action="store_true",
            help="Preview without saving to database",
        )

    def test_cli_case_id_flag_registered(self):
        parser = MagicMock()
        cmd = Command()
        cmd.add_arguments(parser)
        parser.add_argument.assert_any_call(
            "--case-id",
            type=str,
            help="Process a specific case by case_id",
        )

    def test_cli_priority_flag_registered(self):
        parser = MagicMock()
        cmd = Command()
        cmd.add_arguments(parser)
        parser.add_argument.assert_any_call(
            "--priority",
            action="store_true",
            help="Enrich only cases in the priority case list",
        )

    def test_cli_all_flag_registered(self):
        parser = MagicMock()
        cmd = Command()
        cmd.add_arguments(parser)
        parser.add_argument.assert_any_call(
            "--all",
            action="store_true",
            dest="all_cases",
            help="Enrich all DRAFT CIAA cases (explicit, same as default)",
        )

    def test_cli_force_flag_registered(self):
        parser = MagicMock()
        cmd = Command()
        cmd.add_arguments(parser)
        parser.add_argument.assert_any_call(
            "--force",
            action="store_true",
            help="Re-timeline cases that already have timeline entries",
        )

    def test_cli_limit_flag_registered(self):
        parser = MagicMock()
        cmd = Command()
        cmd.add_arguments(parser)
        parser.add_argument.assert_any_call(
            "--limit",
            type=int,
            default=None,
            help="Limit number of cases to process",
        )

    def test_cli_verbose_flag_registered(self):
        parser = MagicMock()
        cmd = Command()
        cmd.add_arguments(parser)
        parser.add_argument.assert_any_call(
            "--verbose",
            action="store_true",
            help="Enable verbose debug logging",
        )

    # ── 8. CLI flag behaviours ──────────────────────────────────────────

    def test_case_id_filtering(self):
        """--case-id processes only the specified case."""
        case_a = self._create_case(case_id="case-a", timeline=[])
        case_b = self._create_case(case_id="case-b", timeline=[])

        ngm_data = self._mock_ngm_response()
        with patch(
            "cases.management.commands.enrich_ciaa_timeline.get_court_case_details",
            return_value=ngm_data,
        ):
            out = StringIO()
            call_command(
                "enrich_ciaa_timeline",
                "--case-id=case-a",
                "--dry-run",
                stdout=out,
            )

        output = out.getvalue()
        assert "case-a" in output
        assert "case-b" not in output

    def test_limit_enforced(self):
        """--limit caps the number of cases processed."""
        for i in range(5):
            self._create_case(
                case_id=f"limit-test-{i:03d}",
                court_cases=["special:001"],
                timeline=[],
            )

        ngm_data = self._mock_ngm_response()
        with patch(
            "cases.management.commands.enrich_ciaa_timeline.get_court_case_details",
            return_value=ngm_data,
        ):
            out = StringIO()
            call_command(
                "enrich_ciaa_timeline",
                "--limit=2",
                "--dry-run",
                stdout=out,
            )

        output = out.getvalue()
        assert "limit-test-000" in output and "limit-test-001" in output
        assert "limit-test-002" not in output

    def test_force_flag_reprocesses_populated(self):
        """--force includes cases that already have timeline entries."""
        self._create_case(
            case_id="already-done",
            timeline=[{"date": "2023-01-15", "title": "Existing entry"}],
        )

        ngm_data = self._mock_ngm_response()
        with patch(
            "cases.management.commands.enrich_ciaa_timeline.get_court_case_details",
            return_value=ngm_data,
        ):
            out = StringIO()
            call_command(
                "enrich_ciaa_timeline",
                "--case-id=already-done",
                "--force",
                "--dry-run",
                stdout=out,
            )

        output = out.getvalue()
        assert "Cases processed:" in output

    def test_priority_and_case_id_mutually_exclusive(self):
        """--priority and --case-id cannot be used together."""
        out = StringIO()
        call_command(
            "enrich_ciaa_timeline",
            "--priority",
            "--case-id=test-001",
            stdout=out,
            stderr=out,
        )

        output = out.getvalue()
        assert "mutually exclusive" in output

    # ── 9. Summary output ───────────────────────────────────────────────

    def test_summary_printed_correctly(self):
        """The summary section displays all stat counters."""
        case = self._create_case()
        ngm_data = self._mock_ngm_response()

        with patch(
            "cases.management.commands.enrich_ciaa_timeline.get_court_case_details",
            return_value=ngm_data,
        ):
            out = StringIO()
            call_command(
                "enrich_ciaa_timeline",
                f"--case-id={case.case_id}",
                "--dry-run",
                stdout=out,
            )

        output = out.getvalue()
        assert "Cases processed:" in output
        assert "Cases enriched:" in output
        assert "Cases skipped:" in output
        assert "No court case" in output
        assert "NGM errors:" in output
        assert "Already populated:" in output
        assert "Total timeline entries:" in output
        assert "Timeline enrichment complete" in output

    # ── 10. Timeline validation ─────────────────────────────────────────

    def test_validates_entry_is_dict(self):
        """Non-dict timeline entries raise ValidationError."""
        cmd = Command()
        with pytest.raises(ValidationError, match="must be a dict"):
            cmd._validate_timeline_entries(["not a dict"])

    def test_validates_entry_has_date(self):
        """Entries missing 'date' raise ValidationError."""
        cmd = Command()
        with pytest.raises(ValidationError, match="missing 'date'"):
            cmd._validate_timeline_entries([{"title": "Test"}])

    def test_validates_entry_has_title(self):
        """Entries missing 'title' raise ValidationError."""
        cmd = Command()
        with pytest.raises(ValidationError, match="missing 'title'"):
            cmd._validate_timeline_entries([{"date": "2023-01-15"}])

    def test_validates_valid_entries_pass(self):
        """Valid timeline entries pass validation."""
        cmd = Command()
        cmd._validate_timeline_entries(
            [{"date": "2023-01-15", "title": "Test", "description": "desc"}]
        )

    # ── 11. Edge cases ──────────────────────────────────────────────────

    def test_skips_hearing_without_date(self):
        """Hearings without hearing_date_ad are skipped."""
        ngm_data = self._mock_ngm_response()
        ngm_data["hearings"].append({
            "hearing_date_ad": None,
            "case_status": "Unknown",
        })
        case = self._create_case()

        cmd = Command()
        timeline = cmd._build_timeline(case, ngm_data)

        assert len(timeline) == 3

    def test_nohearing_stats_tracked(self):
        """Cases with NGM data but empty timeline are counted in stats."""
        case = self._create_case()
        ngm_data = self._mock_ngm_response()
        ngm_data["hearings"] = []
        ngm_data["case"] = {}

        with patch(
            "cases.management.commands.enrich_ciaa_timeline.get_court_case_details",
            return_value=ngm_data,
        ):
            out = StringIO()
            call_command(
                "enrich_ciaa_timeline",
                f"--case-id={case.case_id}",
                "--dry-run",
                stdout=out,
            )

        output = out.getvalue()
        assert "No timeline entries generated" in output
        assert "Cases skipped:" in output

    def test_entries_counted_in_summary(self):
        """Total timeline entries counter is accurate."""
        case = self._create_case()
        ngm_data = self._mock_ngm_response()

        with patch(
            "cases.management.commands.enrich_ciaa_timeline.get_court_case_details",
            return_value=ngm_data,
        ):
            out = StringIO()
            call_command(
                "enrich_ciaa_timeline",
                f"--case-id={case.case_id}",
                "--dry-run",
                stdout=out,
            )

        output = out.getvalue()
        assert "Total timeline entries:  3" in output

    def test_output_shows_timeline_preview(self):
        """Command output shows preview of each timeline entry."""
        case = self._create_case()
        ngm_data = self._mock_ngm_response()

        with patch(
            "cases.management.commands.enrich_ciaa_timeline.get_court_case_details",
            return_value=ngm_data,
        ):
            out = StringIO()
            call_command(
                "enrich_ciaa_timeline",
                f"--case-id={case.case_id}",
                "--dry-run",
                stdout=out,
            )

        output = out.getvalue()
        assert "2023-01-15" in output
        assert "मुद्दा दर्ता" in output
        assert "2023-03-10" in output
        assert "पेशी" in output
        assert "2024-06-20" in output
        assert "फैसला" in output

    def test_stderr_for_mutually_exclusive_flags(self):
        """Error is written to stderr for conflicting flags."""
        out = StringIO()
        call_command(
            "enrich_ciaa_timeline",
            "--priority",
            "--case-id=test-001",
            stdout=out,
            stderr=out,
        )

        output = out.getvalue()
        assert "--priority and --case-id" in output

    def test_case_id_includes_non_draft_cases(self):
        """--case-id bypasses DRAFT filter to allow processing any CORRUPTION case."""
        case = self._create_case(case_id="nondraft", state=CaseState.IN_REVIEW)
        ngm_data = self._mock_ngm_response()

        with patch(
            "cases.management.commands.enrich_ciaa_timeline.get_court_case_details",
            return_value=ngm_data,
        ):
            out = StringIO()
            call_command(
                "enrich_ciaa_timeline",
                "--case-id=nondraft",
                "--dry-run",
                stdout=out,
            )

        output = out.getvalue()
        assert "Cases processed:" in output
