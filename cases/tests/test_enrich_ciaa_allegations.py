"""
Tests for enrich_ciaa_allegations management command.

Phase 2 of CIAA FY 080/081 Case Enrichment pipeline.
Covers: idempotency, CIAA case filtering, JSON response parsing,
press release content extraction, dry-run safety, and CLI flag registration.
"""

import json
from io import StringIO
from unittest.mock import MagicMock, patch

import pytest
import requests
from django.core.management import call_command

from cases.management.commands.enrich_ciaa_allegations import Command
from cases.models import Case, CaseState, CaseType, DocumentSource, SourceType


@pytest.mark.django_db
class TestEnrichCiaaAllegations:
    """Test suite for enrich_ciaa_allegations management command."""

    # ── helpers ──────────────────────────────────────────────────────────

    def _create_case(self, **overrides):
        defaults = {
            "case_type": CaseType.CORRUPTION,
            "state": CaseState.DRAFT,
            "title": "Test CIAA Case",
            "case_id": "case-test-001",
            "court_cases": ["special:test-001"],
            "key_allegations": [],
            "evidence": [],
        }
        defaults.update(overrides)
        return Case.objects.create(**defaults)

    def _create_press_release_source(self, **overrides):
        defaults = {
            "title": "CIAA Press Release",
            "description": "Press release content. " * 30,
            "url": ["https://ciaa.gov.np/pressrelease/3173"],
            "source_type": SourceType.LEGAL_PROCEDURAL,
        }
        defaults.update(overrides)
        return DocumentSource.objects.create(**defaults)

    def _mock_llm_response(self, allegations=None):
        if allegations is None:
            allegations = [
                "Test allegation one in Nepali.",
                "Test allegation two in Nepali.",
            ]
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {
            "choices": [{"message": {"content": json.dumps(allegations)}}]
        }
        return mock_response

    # ── 1. Idempotency ──────────────────────────────────────────────────

    def test_skips_cases_with_populated_key_allegations(self):
        """
        Idempotency: cases with non-empty key_allegations are skipped
        and counted as 'Already populated' in the summary.
        """
        self._create_case(
            case_id="populated-test",
            key_allegations=["Already populated allegation"],
        )

        out = StringIO()
        call_command("enrich_ciaa_allegations", "--dry-run", stdout=out)

        output = out.getvalue()
        assert "Already populated:      1" in output
        assert "Cases processed:        0" in output

    def test_processes_cases_with_empty_key_allegations(self):
        """
        Idempotency: cases with empty key_allegations are processed.
        """
        pr_source = self._create_press_release_source()
        case = self._create_case(
            key_allegations=[],
            evidence=[{"source_id": pr_source.source_id, "description": "test"}],
        )

        with patch("requests.post") as mock_post:
            mock_post.return_value = self._mock_llm_response()

            out = StringIO()
            call_command(
                "enrich_ciaa_allegations",
                f"--case-id={case.case_id}",
                "--dry-run",
                stdout=out,
            )

        output = out.getvalue()
        assert "Cases processed:        1" in output
        assert "Cases enriched:         0" in output
        assert "Already populated:      0" in output
        assert "DRY RUN" in output

    # ── 2. CIAA case filtering ──────────────────────────────────────────

    def test_includes_draft_case_without_special_prefix(self):
        """
        DRAFT cases without 'special:' in court_cases are included.
        No strict court_cases filtering — all DRAFT cases with empty
        key_allegations are candidates.
        """
        self._create_case(
            case_id="ciaa-001",
            court_cases=["special:123", "supreme:456"],
            key_allegations=[],
        )

        self._create_case(
            case_id="non-special-001",
            court_cases=["district:789"],
            key_allegations=[],
        )

        self._create_case(
            case_id="no-court-001",
            court_cases=[],
            key_allegations=[],
        )

        cmd = Command()
        cmd.stats = {
            "cases_processed": 0,
            "cases_enriched": 0,
            "cases_skipped": 0,
            "cases_no_content": 0,
            "cases_llm_error": 0,
            "cases_already_populated": 0,
        }

        cases = cmd._get_ciaa_cases()
        case_ids = [c.case_id for c in cases]
        assert "ciaa-001" in case_ids
        assert "non-special-001" in case_ids
        assert "no-court-001" in case_ids

    def test_skips_non_draft_cases(self):
        """
        Only DRAFT cases are considered for enrichment.
        """
        self._create_case(
            case_id="draft-case",
            court_cases=["special:1"],
            state=CaseState.DRAFT,
            key_allegations=[],
        )

        self._create_case(
            case_id="published-case",
            court_cases=["special:2"],
            state=CaseState.PUBLISHED,
            key_allegations=[],
        )

        cmd = Command()
        cmd.stats = {
            "cases_processed": 0,
            "cases_enriched": 0,
            "cases_skipped": 0,
            "cases_no_content": 0,
            "cases_llm_error": 0,
            "cases_already_populated": 0,
        }

        cases = cmd._get_ciaa_cases()
        case_ids = [c.case_id for c in cases]
        assert "draft-case" in case_ids
        assert "published-case" not in case_ids

    # ── 3. JSON response parsing ────────────────────────────────────────

    def test_parse_clean_json_array(self):
        """Clean JSON array of strings parses correctly."""
        cmd = Command()
        result = cmd._parse_allegations_response(
            '["Allegation one.", "Allegation two."]'
        )
        assert result == ["Allegation one.", "Allegation two."]

    def test_parse_json_with_wrapper_text(self):
        """JSON array wrapped in explanatory text is extracted."""
        cmd = Command()
        result = cmd._parse_allegations_response(
            'Here are the allegations:\n\n```json\n["First.", "Second."]\n```\n\nHope this helps.'
        )
        assert result == ["First.", "Second."]

    def test_parse_invalid_json_returns_none(self):
        """Invalid JSON returns None."""
        cmd = Command()
        result = cmd._parse_allegations_response("This is not JSON at all.")
        assert result is None

    def test_parse_dict_items_flattened_to_strings(self):
        """Response items that are dicts get flattened to strings."""
        cmd = Command()
        result = cmd._parse_allegations_response(
            json.dumps(
                [
                    {"allegation": "First dict entry."},
                    "Second string entry.",
                ]
            )
        )
        assert result == ["First dict entry.", "Second string entry."]

    def test_parse_empty_json_array(self):
        """Empty JSON array returns None (not a list of valid allegations)."""
        cmd = Command()
        result = cmd._parse_allegations_response("[]")
        assert result is None

    def test_parse_over_max_items(self):
        """More than 5 items gets truncated to the first 5."""
        cmd = Command()
        seven = [f"Allegation {i}." for i in range(1, 8)]
        result = cmd._parse_allegations_response(json.dumps(seven))
        assert len(result) == 5
        assert result == seven[:5]

    def test_parse_non_list_response(self):
        """Dict response with nested array is extracted correctly."""
        cmd = Command()
        result = cmd._parse_allegations_response(
            json.dumps({"allegations": ["One", "Two"]})
        )
        assert result == ["One", "Two"]

    # ── 4. Press release content extraction ─────────────────────────────

    def test_extract_press_release_from_description(self):
        """Press release content is extracted from DocumentSource.description."""
        pr_source = self._create_press_release_source(
            description="Detailed CIAA press release about corruption case. " * 20,
        )
        case = self._create_case(
            evidence=[{"source_id": pr_source.source_id, "description": "test"}],
        )

        cmd = Command()
        content = cmd._get_press_release_content(case)

        assert content is not None
        assert "Detailed CIAA press release" in content

    def test_skips_case_without_evidence(self):
        """Cases with no evidence entries return None for press release content."""
        case = self._create_case(evidence=[])

        cmd = Command()
        content = cmd._get_press_release_content(case)

        assert content is None

    def test_skips_case_without_press_release_source(self):
        """Non-press-release sources are skipped."""
        non_pr_source = DocumentSource.objects.create(
            title="Regular Document",
            description="Not a press release.",
            url=["https://example.com/doc.pdf"],
            source_type=SourceType.LEGAL_COURT_ORDER,
        )
        case = self._create_case(
            evidence=[{"source_id": non_pr_source.source_id, "description": "test"}],
        )

        cmd = Command()
        content = cmd._get_press_release_content(case)

        assert content is None

    def test_is_press_release_source_by_title(self):
        """Source identified by title containing 'press release'."""
        source = DocumentSource(title="CIAA Press Release — 2082-04-19")
        cmd = Command()
        assert cmd._is_press_release_source(source) is True

    def test_is_press_release_source_by_url(self):
        """Source identified by URL containing ciaa.gov.np."""
        source = DocumentSource(
            title="Unknown Document",
            url=["https://ciaa.gov.np/pressrelease/3173"],
        )
        cmd = Command()
        assert cmd._is_press_release_source(source) is True

    def test_ciaa_url_accepted_in_extraction_path(self):
        """A source with a CIAA URL is accepted for extraction when description is short."""
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
        with patch.object(
            cmd, "_convert_to_markdown", return_value="Extracted text. " * 50
        ):
            content = cmd._get_press_release_content(case)

        assert content is not None
        assert "Extracted text" in content

    # ── 5. Dry-run safety ───────────────────────────────────────────────

    def test_dry_run_no_db_writes(self):
        """Dry-run mode does not modify the database."""
        pr_source = self._create_press_release_source()
        case = self._create_case(
            key_allegations=[],
            evidence=[{"source_id": pr_source.source_id, "description": "test"}],
        )

        with patch("requests.post") as mock_post:
            mock_post.return_value = self._mock_llm_response()

            out = StringIO()
            call_command(
                "enrich_ciaa_allegations",
                f"--case-id={case.case_id}",
                "--dry-run",
                stdout=out,
            )

        case.refresh_from_db()
        assert case.key_allegations == []

    def test_production_mode_saves_allegations(self):
        """Without --dry-run, key_allegations are saved to the database."""
        pr_source = self._create_press_release_source()
        case = self._create_case(
            key_allegations=[],
            evidence=[{"source_id": pr_source.source_id, "description": "test"}],
        )

        with patch("requests.post") as mock_post:
            mock_post.return_value = self._mock_llm_response(
                allegations=["Saved allegation one.", "Saved allegation two."],
            )

            out = StringIO()
            call_command(
                "enrich_ciaa_allegations",
                f"--case-id={case.case_id}",
                "--llm-api-key=test-key",
                stdout=out,
            )

        case.refresh_from_db()
        assert len(case.key_allegations) == 2
        assert "Saved allegation one." in case.key_allegations

    # ── 6. CLI flag registration and parsing ────────────────────────────

    def test_cli_dry_run_flag_registered(self):
        """--dry-run flag is properly registered and parsed."""
        parser = MagicMock()
        cmd = Command()
        cmd.add_arguments(parser)
        parser.add_argument.assert_any_call(
            "--dry-run",
            action="store_true",
            help="Preview without saving to database",
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

    def test_cli_limit_enforced(self):
        """--limit option caps the number of cases processed."""
        for i in range(5):
            self._create_case(
                case_id=f"limit-test-{i:03d}",
                court_cases=["special:1"],
                key_allegations=[],
            )

        with patch("requests.post") as mock_post:
            mock_post.return_value = self._mock_llm_response()

            out = StringIO()
            call_command(
                "enrich_ciaa_allegations",
                "--limit=1",
                "--dry-run",
                stdout=out,
            )

        output = out.getvalue()
        assert "Cases processed:        1" in output

    # ── edge cases ──────────────────────────────────────────────────────

    def test_llm_error_increments_counter(self):
        """LLM API failures are tracked in stats without crashing."""
        pr_source = self._create_press_release_source()
        case = self._create_case(
            evidence=[{"source_id": pr_source.source_id, "description": "test"}],
        )

        with patch("requests.post") as mock_post:
            mock_post.side_effect = requests.ConnectionError("Connection refused")

            out = StringIO()
            call_command(
                "enrich_ciaa_allegations",
                f"--case-id={case.case_id}",
                "--llm-api-key=test-key",
                "--dry-run",
                stdout=out,
            )

        output = out.getvalue()
        assert "LLM errors:             1" in output

    def test_case_with_already_populated_skipped(self):
        """Cases with existing key_allegations count as already populated."""
        self._create_case(
            case_id="populated-case",
            court_cases=["special:1"],
            key_allegations=["Existing allegation."],
        )

        cmd = Command()
        cmd.stats = {
            "cases_processed": 0,
            "cases_enriched": 0,
            "cases_skipped": 0,
            "cases_no_content": 0,
            "cases_llm_error": 0,
            "cases_already_populated": 0,
        }

        cases = cmd._get_ciaa_cases()
        case_ids = [c.case_id for c in cases]
        assert "populated-case" not in case_ids
        assert cmd.stats["cases_already_populated"] == 1

    def test_summary_printed_correctly(self):
        """The summary section displays all stat counters."""
        pr_source = self._create_press_release_source()
        case = self._create_case(
            evidence=[{"source_id": pr_source.source_id, "description": "test"}],
        )

        with patch("requests.post") as mock_post:
            mock_post.return_value = self._mock_llm_response()

            out = StringIO()
            call_command(
                "enrich_ciaa_allegations",
                f"--case-id={case.case_id}",
                "--dry-run",
                stdout=out,
            )

        output = out.getvalue()
        assert "Cases processed:" in output
        assert "Cases enriched:" in output
        assert "Cases skipped:" in output
        assert "No press release content:" in output
        assert "LLM errors:" in output
        assert "Already populated:" in output
        assert "Allegation extraction complete" in output
