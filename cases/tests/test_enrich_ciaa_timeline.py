"""
Tests for enrich_ciaa_timeline management command.

Phase 1 (A.3) of CIAA FY 080/081 Case Enrichment pipeline.
Covers: idempotency, CIAA case filtering, JSON response parsing,
source content acquisition, dry-run safety, --force flag,
--fiscal-year filtering, and CLI flag registration.
"""

import json
from io import StringIO
from unittest.mock import MagicMock, patch

import pytest
import requests
from django.core.management import call_command
from django.core.management.base import CommandError

from cases.management.commands.enrich_ciaa_timeline import Command
from cases.models import Case, CaseState, CaseType, DocumentSource, SourceType


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
            "court_cases": ["special:test-001"],
            "timeline": [],
            "evidence": [],
        }
        defaults.update(overrides)
        return Case.objects.create(**defaults)

    def _create_source(self, source_type=SourceType.LEGAL_PROCEDURAL, **overrides):
        defaults = {
            "title": "CIAA Press Release",
            "description": "Source document content with timeline information. " * 30,
            "url": ["https://ciaa.gov.np/pressrelease/3173"],
            "source_type": source_type,
        }
        defaults.update(overrides)
        return DocumentSource.objects.create(**defaults)

    def _session(self):
        """Create a mock requests.Session for direct method tests."""
        session = MagicMock(spec=requests.Session)
        return session

    def _mock_llm_response(self, entries=None):
        if entries is None:
            entries = [
                {
                    "date": "2023-08-15",
                    "title": "अख्तियारले अनुसन्धान शुरु",
                    "description": "विशेष अदालतमा मुद्दा दायर गर्ने निर्णय",
                },
                {
                    "date": "2023-09-20",
                    "title": "विशेष अदालतमा मुद्दा दायर",
                    "description": "विशेष अदालतमा मुद्दा दर्ता भएको",
                },
                {"date": "2024-03-10", "title": "फैसला सुनाइएको", "description": ""},
            ]
        return json.dumps(entries)

    def _mock_call_llm(self, entries=None):
        """Patch call_llm in the enrich_ciaa_timeline namespace."""
        return patch(
            "cases.management.commands.enrich_ciaa_timeline.call_llm",
            return_value=self._mock_llm_response(entries),
        )

    def _mock_call_llm_error(self, exc=None):
        """Patch call_llm to raise an error."""
        if exc is None:
            exc = requests.ConnectionError("Connection refused")
        return patch(
            "cases.management.commands.enrich_ciaa_timeline.call_llm",
            side_effect=exc,
        )

    def _mock_ngm_data(self, hearings=None, case_overrides=None):
        """Create mock NGM data dict."""
        data = {
            "case": {
                "registration_date_ad": "2023-09-20",
                "verdict_date_ad": "2024-03-10",
                "case_status": "फैसला भएको",
                "verdict_judge": "Hon. Judge Name",
                **(case_overrides or {}),
            },
            "hearings": hearings
            or [
                {
                    "hearing_date_ad": "2023-11-15",
                    "decision_type": "पेशी",
                    "remarks": "सुनुवाइ भएको",
                },
                {
                    "hearing_date_ad": "2024-01-20",
                    "decision_type": "पेशी",
                    "remarks": "अन्तिम सुनुवाइ",
                },
            ],
        }
        return data

    # ── 1. Idempotency ──────────────────────────────────────────────────

    def test_skips_cases_with_populated_timeline(self):
        """Idempotency: cases with non-empty timeline are skipped."""
        self._create_case(
            case_id="populated-test",
            timeline=[{"date": "2023-08-15", "title": "Test event"}],
        )

        out = StringIO()
        call_command("enrich_ciaa_timeline", "--dry-run", stdout=out)

        output = out.getvalue()
        assert "Already populated:      1" in output
        assert "Cases processed:        0" in output

    def test_processes_cases_with_empty_timeline(self):
        """Cases with empty timeline are processed."""
        pr_source = self._create_source()
        case = self._create_case(
            timeline=[],
            evidence=[{"source_id": pr_source.source_id, "description": "test"}],
        )

        with self._mock_call_llm():
            out = StringIO()
            call_command(
                "enrich_ciaa_timeline",
                f"--case-id={case.case_id}",
                "--dry-run",
                stdout=out,
            )

        output = out.getvalue()
        assert "Cases processed:        1" in output
        assert "Cases enriched:         0" in output
        assert "Already populated:      0" in output
        assert "DRY RUN" in output

    # ── 2. --force flag ─────────────────────────────────────────────────

    def test_force_reprocesses_populated_cases(self):
        """--force flag re-generates timeline even when already populated."""
        pr_source = self._create_source()
        case = self._create_case(
            timeline=[{"date": "2022-01-01", "title": "Old event"}],
            evidence=[{"source_id": pr_source.source_id, "description": "test"}],
        )

        with self._mock_call_llm():
            out = StringIO()
            call_command(
                "enrich_ciaa_timeline",
                f"--case-id={case.case_id}",
                "--force",
                "--dry-run",
                stdout=out,
            )

        output = out.getvalue()
        assert "force" in output.lower()
        assert "Cases processed:        1" in output
        assert "Already populated:      0" in output

    # ── 3. --fiscal-year filtering ───────────────────────────────────────

    def test_fiscal_year_filter_matches_court_cases(self):
        """--fiscal-year 080 filters cases with 080-CR court cases."""
        self._create_case(
            case_id="fy-080-case",
            court_cases=["special:080-CR-0007"],
            timeline=[],
        )
        self._create_case(
            case_id="fy-081-case",
            court_cases=["special:081-CR-0123"],
            timeline=[],
        )

        cmd = Command()
        cmd.stats = {
            "cases_processed": 0,
            "cases_enriched": 0,
            "cases_skipped": 0,
            "cases_no_content": 0,
            "cases_llm_error": 0,
            "cases_already_populated": 0,
            "cases_ngm_used": 0,
        }

        cases = cmd._get_ciaa_cases(fiscal_year="080")
        case_ids = [c.case_id for c in cases]
        assert "fy-080-case" in case_ids
        assert "fy-081-case" not in case_ids

    def test_fiscal_year_filter_normalized(self):
        """--fiscal-year handles leading zeros (e.g., '080' and '80' both work)."""
        self._create_case(
            case_id="fy-080-case",
            court_cases=["special:080-CR-0007"],
            timeline=[],
        )

        cmd = Command()
        cmd.stats = dict.fromkeys(
            [
                "cases_processed",
                "cases_enriched",
                "cases_skipped",
                "cases_no_content",
                "cases_llm_error",
                "cases_already_populated",
                "cases_ngm_used",
            ],
            0,
        )

        cases = cmd._get_ciaa_cases(fiscal_year="80")
        case_ids = [c.case_id for c in cases]
        assert "fy-080-case" in case_ids

    def test_fiscal_year_rejects_invalid_format(self):
        """Invalid fiscal year format raises CommandError."""
        with pytest.raises(CommandError) as exc_info:
            call_command(
                "enrich_ciaa_timeline",
                "--fiscal-year=not-a-year",
                "--dry-run",
            )
        assert "Invalid fiscal year" in str(exc_info.value)

    # ── 4. CIAA case filtering ───────────────────────────────────────────

    def test_skips_non_draft_cases(self):
        """Only DRAFT cases are considered for enrichment."""
        self._create_case(
            case_id="draft-case",
            state=CaseState.DRAFT,
            timeline=[],
        )
        self._create_case(
            case_id="published-case",
            state=CaseState.PUBLISHED,
            timeline=[],
        )

        cmd = Command()
        cmd.stats = dict.fromkeys(
            [
                "cases_processed",
                "cases_enriched",
                "cases_skipped",
                "cases_no_content",
                "cases_llm_error",
                "cases_already_populated",
                "cases_ngm_used",
            ],
            0,
        )

        cases = cmd._get_ciaa_cases()
        case_ids = [c.case_id for c in cases]
        assert "draft-case" in case_ids
        assert "published-case" not in case_ids

    # ── 5. JSON response parsing ────────────────────────────────────────

    def test_parse_clean_json_array(self):
        """Clean JSON array of timeline entries parses correctly."""
        cmd = Command()
        entries = [
            {"date": "2023-08-15", "title": "घटना १"},
            {"date": "2023-09-20", "title": "घटना २", "description": "विवरण"},
        ]
        result = cmd._parse_timeline_response(json.dumps(entries))
        assert len(result) == 2
        assert result[0]["date"] == "2023-08-15"
        assert result[0]["title"] == "घटना १"

    def test_parse_json_with_markdown_wrappers(self):
        """Timeline wrapped in markdown code fences is extracted."""
        cmd = Command()
        response = (
            "Here is the timeline:\n```json\n"
            + json.dumps([{"date": "2023-08-15", "title": "Test"}])
            + "\n```\nDone."
        )
        result = cmd._parse_timeline_response(response)
        assert len(result) == 1
        assert result[0]["date"] == "2023-08-15"

    def test_parse_invalid_json_returns_none(self):
        """Invalid JSON returns None."""
        cmd = Command()
        result = cmd._parse_timeline_response("This is not JSON at all.")
        assert result is None

    def test_parse_empty_array_returns_none(self):
        """Empty JSON array returns None (no timeline entries)."""
        cmd = Command()
        result = cmd._parse_timeline_response("[]")
        assert result is None

    def test_parse_missing_required_fields(self):
        """Entries missing date or title are filtered out."""
        cmd = Command()
        entries = [
            {"title": "No date entry"},
            {"date": "2023-08-15"},
            {"date": "2023-09-20", "title": "Valid entry"},
        ]
        result = cmd._parse_timeline_response(json.dumps(entries))
        assert len(result) == 1
        assert result[0]["date"] == "2023-09-20"

    def test_parse_accepts_alternate_field_names(self):
        """Fallback field names (date_bs, event, desc) are accepted."""
        cmd = Command()
        entries = [
            {"date_bs": "2023-08-15", "event": "Alt field test", "desc": "Alt desc"},
        ]
        result = cmd._parse_timeline_response(json.dumps(entries))
        assert len(result) == 1
        assert result[0]["date"] == "2023-08-15"
        assert result[0]["title"] == "Alt field test"
        assert result[0]["description"] == "Alt desc"

    def test_parse_nested_timeline_key(self):
        """Response with 'timeline' wrapper key is extracted."""
        cmd = Command()
        entries = [
            {"date": "2023-08-15", "title": "Wrapped"},
        ]
        result = cmd._parse_timeline_response(json.dumps({"timeline": entries}))
        assert len(result) == 1

    # ── 6. Source content acquisition ───────────────────────────────────

    def test_extract_source_from_description(self):
        """Source content extracted from DocumentSource.description when >200 chars."""
        source = self._create_source(
            source_type=SourceType.LEGAL_PROCEDURAL,
            description="Detailed case information with timeline data. " * 20,
        )
        case = self._create_case(
            evidence=[{"source_id": source.source_id, "description": "test"}],
        )

        cmd = Command()
        session = self._session()
        content = cmd._get_source_content(case, session)

        assert content is not None
        assert "Detailed case information" in content

    def test_skips_case_without_evidence(self):
        """Cases with no evidence return None."""
        case = self._create_case(evidence=[])

        cmd = Command()
        session = self._session()
        content = cmd._get_source_content(case, session)

        assert content is None

    def test_source_from_url_when_description_short(self):
        """When description is short, download from URL via _convert_to_markdown."""
        source = DocumentSource.objects.create(
            title="CIAA Press Release",
            description="Short.",
            url=["https://ciaa.gov.np/pressrelease/3173"],
            source_type=SourceType.LEGAL_PROCEDURAL,
        )
        case = self._create_case(
            evidence=[{"source_id": source.source_id, "description": "test"}],
        )

        cmd = Command()
        session = self._session()
        with patch(
            "cases.management.commands.enrich_ciaa_timeline.convert_to_markdown",
            return_value="Extracted text. " * 50,
        ):
            content = cmd._get_source_content(case, session)

        assert content is not None
        assert "Extracted text" in content

    def test_combines_multiple_source_types(self):
        """Content from LEGAL_PROCEDURAL + LEGAL_COURT_ORDER are combined."""
        legal_proc = DocumentSource.objects.create(
            title="CIAA Press Release",
            description="Press release content with dates. " * 30,
            url=["https://ciaa.gov.np/pressrelease/3173"],
            source_type=SourceType.LEGAL_PROCEDURAL,
        )
        court_order = DocumentSource.objects.create(
            title="Court Order",
            description="Court order with hearing and verdict dates. " * 30,
            url=["https://ngm-store.jawafdehi.org/court/123"],
            source_type=SourceType.LEGAL_COURT_ORDER,
        )
        case = self._create_case(
            evidence=[
                {"source_id": legal_proc.source_id, "description": "press release"},
                {"source_id": court_order.source_id, "description": "court order"},
            ],
        )

        cmd = Command()
        session = self._session()
        content = cmd._get_source_content(case, session)

        assert content is not None
        assert "Press release content" in content
        assert "Court order" in content
        assert "---" in content

    # ── 7. Dry-run safety ───────────────────────────────────────────────

    def test_dry_run_no_db_writes(self):
        """Dry-run does not modify the database."""
        pr_source = self._create_source()
        case = self._create_case(
            timeline=[],
            evidence=[{"source_id": pr_source.source_id, "description": "test"}],
        )

        with self._mock_call_llm():
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
        """Without --dry-run, timeline entries are saved to the database."""
        pr_source = self._create_source()
        case = self._create_case(
            timeline=[],
            evidence=[{"source_id": pr_source.source_id, "description": "test"}],
        )

        with self._mock_call_llm(
            entries=[
                {"date": "2023-08-15", "title": "Saved event"},
            ]
        ):

            out = StringIO()
            call_command(
                "enrich_ciaa_timeline",
                f"--case-id={case.case_id}",
                "--llm-api-key=test-key",
                stdout=out,
            )

        case.refresh_from_db()
        assert len(case.timeline) == 1
        assert case.timeline[0]["date"] == "2023-08-15"
        assert case.timeline[0]["title"] == "Saved event"

    # ── 8. CLI flag registration ────────────────────────────────────────

    def test_cli_dry_run_flag_registered(self):
        """--dry-run flag is properly registered."""
        parser = MagicMock()
        cmd = Command()
        cmd.add_arguments(parser)
        parser.add_argument.assert_any_call(
            "--dry-run",
            action="store_true",
            help="Preview without saving to database",
        )

    def test_cli_force_flag_registered(self):
        """--force flag is properly registered."""
        parser = MagicMock()
        cmd = Command()
        cmd.add_arguments(parser)
        parser.add_argument.assert_any_call(
            "--force",
            action="store_true",
            help="Re-generate timeline even if timeline already exists",
        )

    def test_cli_fiscal_year_flag_registered(self):
        """--fiscal-year flag is properly registered."""
        parser = MagicMock()
        cmd = Command()
        cmd.add_arguments(parser)
        parser.add_argument.assert_any_call(
            "--fiscal-year",
            type=str,
            help="Filter by fiscal year (e.g., '080' or '081')",
        )

    def test_cli_case_id_flag_registered(self):
        """--case-id flag is properly registered."""
        parser = MagicMock()
        cmd = Command()
        cmd.add_arguments(parser)
        parser.add_argument.assert_any_call(
            "--case-id",
            type=str,
            help="Process a specific case by case_id",
        )

    def test_cli_limit_flag_registered(self):
        """--limit flag is properly registered."""
        parser = MagicMock()
        cmd = Command()
        cmd.add_arguments(parser)
        parser.add_argument.assert_any_call(
            "--limit",
            type=int,
            help="Maximum number of cases to process",
        )

    def test_cli_limit_enforced(self):
        """--limit option caps the number of cases processed."""
        for i in range(5):
            self._create_case(
                case_id=f"limit-test-{i:03d}",
                timeline=[],
            )

        with self._mock_call_llm():
            out = StringIO()
            call_command(
                "enrich_ciaa_timeline",
                "--limit=1",
                "--dry-run",
                stdout=out,
            )

        output = out.getvalue()
        assert "Cases processed:        1" in output

    def test_cli_verbose_flag_registered(self):
        """--verbose flag is properly registered."""
        parser = MagicMock()
        cmd = Command()
        cmd.add_arguments(parser)
        parser.add_argument.assert_any_call(
            "--verbose",
            action="store_true",
            help="Enable verbose debug logging",
        )

    # ── edge cases ──────────────────────────────────────────────────────

    def test_llm_error_increments_counter(self):
        """LLM API failures are tracked in stats without crashing."""
        pr_source = self._create_source()
        case = self._create_case(
            evidence=[{"source_id": pr_source.source_id, "description": "test"}],
        )

        with self._mock_call_llm_error():
            out = StringIO()
            call_command(
                "enrich_ciaa_timeline",
                f"--case-id={case.case_id}",
                "--llm-api-key=test-key",
                "--dry-run",
                stdout=out,
            )

        output = out.getvalue()
        assert "LLM errors:             1" in output

    def test_no_content_counted(self):
        """Cases without usable source content count as 'No source content'."""
        self._create_case(
            case_id="no-source-case",
            evidence=[],
            timeline=[],
        )

        cmd = Command()
        cmd.stats = dict.fromkeys(
            [
                "cases_processed",
                "cases_enriched",
                "cases_skipped",
                "cases_no_content",
                "cases_llm_error",
                "cases_already_populated",
                "cases_ngm_used",
            ],
            0,
        )

        out = StringIO()
        call_command(
            "enrich_ciaa_timeline",
            "--case-id=no-source-case",
            "--dry-run",
            stdout=out,
        )

        output = out.getvalue()
        assert "No source content" in output

    def test_summary_printed_correctly(self):
        """The summary section displays all stat counters."""
        pr_source = self._create_source()
        case = self._create_case(
            evidence=[{"source_id": pr_source.source_id, "description": "test"}],
        )

        with self._mock_call_llm():
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
        assert "No source content:" in output
        assert "LLM errors:" in output
        assert "Already populated:" in output
        assert "NGM data used:" in output
        assert "Timeline extraction complete" in output

    def test_responds_to_missing_api_key_in_production(self):
        """Production mode without API key raises appropriate error."""
        pr_source = self._create_source()
        case = self._create_case(
            evidence=[{"source_id": pr_source.source_id, "description": "test"}],
        )

        with patch.dict("os.environ", {}, clear=True):
            with pytest.raises(CommandError) as exc_info:
                call_command(
                    "enrich_ciaa_timeline",
                    f"--case-id={case.case_id}",
                )
        assert "No LLM API key" in str(exc_info.value)

    # ── NGM structured hearing data ─────────────────────────────────────

    def test_get_ngm_data_returns_data_for_special_ref(self):
        """_get_ngm_data queries NGM when special: ref exists in court_cases."""
        case = self._create_case(
            court_cases=["special:080-CR-0111"],
        )
        mock_data = self._mock_ngm_data()

        cmd = Command()
        with patch(
            "cases.management.commands.enrich_ciaa_timeline.get_court_case_details",
            return_value=mock_data,
        ):
            result = cmd._get_ngm_data(case)

        assert result is not None
        assert result["case"]["registration_date_ad"] == "2023-09-20"
        assert len(result["hearings"]) == 2

    def test_get_ngm_data_returns_none_without_special_ref(self):
        """_get_ngm_data returns None when no special: ref in court_cases."""
        case = self._create_case(
            court_cases=["supreme:123"],
        )

        cmd = Command()
        result = cmd._get_ngm_data(case)

        assert result is None

    def test_get_ngm_data_returns_none_empty_court_cases(self):
        """_get_ngm_data returns None when court_cases is empty."""
        case = self._create_case(court_cases=[])

        cmd = Command()
        result = cmd._get_ngm_data(case)

        assert result is None

    def test_get_ngm_data_queries_database(self):
        """_get_ngm_data fetches real data from NGM when available."""
        case = self._create_case(
            court_cases=["special:080-CR-0111"],
        )
        mock_data = self._mock_ngm_data()

        cmd = Command()
        with patch(
            "cases.management.commands.enrich_ciaa_timeline.get_court_case_details",
            return_value=mock_data,
        ):
            result = cmd._get_ngm_data(case)

        assert result is not None
        assert result["case"]["registration_date_ad"] == "2023-09-20"
        assert len(result["hearings"]) == 2

    def test_format_ngm_section_with_data(self):
        """_format_ngm_section produces structured text from NGM data."""
        mock_data = self._mock_ngm_data()

        cmd = Command()
        section = cmd._format_ngm_section(mock_data)

        assert "NGM STRUCTURED HEARING DATA" in section
        assert "2023-09-20" in section
        assert "2024-03-10" in section
        assert "2023-11-15" in section
        assert "सुनुवाइ भएको" in section

    def test_format_ngm_section_empty(self):
        """_format_ngm_section returns empty string for None/empty data."""
        cmd = Command()
        assert cmd._format_ngm_section(None) == ""
        assert cmd._format_ngm_section({}) == ""

    def test_ngm_data_passed_to_extract_timeline(self):
        """NGM data is used in the extraction prompt."""
        pr_source = self._create_source()
        case = self._create_case(
            court_cases=["special:080-CR-0111"],
            evidence=[{"source_id": pr_source.source_id, "description": "test"}],
        )
        mock_ngm = self._mock_ngm_data()

        with patch(
            "cases.management.commands.enrich_ciaa_timeline.get_court_case_details",
            return_value=mock_ngm,
        ):
            with self._mock_call_llm():
                out = StringIO()
                call_command(
                    "enrich_ciaa_timeline",
                    f"--case-id={case.case_id}",
                    "--dry-run",
                    stdout=out,
                )

        output = out.getvalue()
        assert "NGM data: 2 hearing(s)" in output

    def test_ngm_counter_incremented(self):
        """cases_ngm_used stat is incremented when NGM data is available."""
        pr_source = self._create_source()
        case = self._create_case(
            court_cases=["special:080-CR-0111"],
            evidence=[{"source_id": pr_source.source_id, "description": "test"}],
        )
        mock_ngm = self._mock_ngm_data()

        with patch(
            "cases.management.commands.enrich_ciaa_timeline.get_court_case_details",
            return_value=mock_ngm,
        ):
            with self._mock_call_llm():
                out = StringIO()
                call_command(
                    "enrich_ciaa_timeline",
                    f"--case-id={case.case_id}",
                    "--dry-run",
                    stdout=out,
                )

        output = out.getvalue()
        assert "NGM data used:          1" in output

    def test_ngm_query_failure_handled_gracefully(self):
        """NGM query failures are caught and logged without crashing."""
        pr_source = self._create_source()
        case = self._create_case(
            court_cases=["special:080-CR-0111"],
            evidence=[{"source_id": pr_source.source_id, "description": "test"}],
        )

        with patch(
            "cases.management.commands.enrich_ciaa_timeline.get_court_case_details",
            side_effect=Exception("Database error"),
        ):
            with self._mock_call_llm():
                out = StringIO()
                call_command(
                    "enrich_ciaa_timeline",
                    f"--case-id={case.case_id}",
                    "--dry-run",
                    stdout=out,
                )

        output = out.getvalue()
        assert "NGM data: none" in output

    # ── --priority flag ──────────────────────────────────────────────────

    def test_cli_priority_flag_registered(self):
        """--priority flag is properly registered."""
        parser = MagicMock()
        cmd = Command()
        cmd.add_arguments(parser)
        parser.add_argument.assert_any_call(
            "--priority",
            action="store_true",
            help="Enrich only cases in the priority case list",
        )

    def test_priority_and_case_id_mutually_exclusive(self):
        """--priority and --case-id cannot be used together."""
        out = StringIO()
        with patch("sys.stderr", new_callable=StringIO) as stderr:
            call_command(
                "enrich_ciaa_timeline",
                "--priority",
                "--case-id=case-001",
                "--dry-run",
                stdout=out,
                stderr=stderr,
            )
            output = stderr.getvalue()
        assert "mutually exclusive" in output
