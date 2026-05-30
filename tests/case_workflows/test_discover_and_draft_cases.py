"""
Tests for the discover_and_draft_cases management command.

Covers:
- Creates DRAFT CORRUPTION cases for each CIAA case number
- Title format is canonical ("CIAA Special Court Case 081-CR-XXXX")
- Skips cases that already have the case number in their title
- Fully idempotent across multiple runs
- Partial pre-population creates only the missing cases
- stdout output: [CREATED] / [SKIP] lines and summary counts
"""

from io import StringIO

import pytest
from django.core.management import call_command

from case_workflows.workflows.ciaa_caseworker.constants import CIAA_CASE_NUMBERS
from cases.models import Case, CaseState, CaseType

UNIQUE_CIAA_CASE_NUMBERS = sorted(set(CIAA_CASE_NUMBERS))
DUPLICATE_CIAA_CASE_COUNT = len(CIAA_CASE_NUMBERS) - len(UNIQUE_CIAA_CASE_NUMBERS)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(**kwargs) -> str:
    """Run the command and return captured stdout."""
    out = StringIO()
    call_command("discover_and_draft_cases", stdout=out, **kwargs)
    return out.getvalue()


def _make_case(
    title: str, case_type=CaseType.CORRUPTION, state=CaseState.DRAFT
) -> Case:
    return Case.objects.create(title=title, case_type=case_type, state=state)


# ---------------------------------------------------------------------------
# Creation behaviour
# ---------------------------------------------------------------------------


class TestDiscoverAndDraftCasesCreation:
    @pytest.mark.django_db
    def test_creates_draft_for_single_case_number(self):
        """A single case number produces exactly one DRAFT CORRUPTION case."""
        _run()
        case = Case.objects.get(title__icontains="081-CR-0022")
        assert case.state == CaseState.DRAFT
        assert case.case_type == CaseType.CORRUPTION
        assert case.case_id.startswith("case-")

    @pytest.mark.django_db
    def test_all_unique_cases_created_from_empty_db(self):
        """Starting from an empty DB, one case is created per unique case number."""
        _run()
        assert Case.objects.count() == len(UNIQUE_CIAA_CASE_NUMBERS)
        for number in UNIQUE_CIAA_CASE_NUMBERS:
            assert Case.objects.filter(
                title__icontains=number
            ).exists(), f"Expected a case with '{number}' in title"

    @pytest.mark.django_db
    def test_title_format(self):
        """Title follows the canonical format."""
        _run()
        case = Case.objects.get(title__icontains="081-CR-0022")
        assert case.title == "CIAA Special Court Case 081-CR-0022"

    @pytest.mark.django_db
    def test_all_created_cases_are_corruption_draft(self):
        """Every created case has case_type=CORRUPTION and state=DRAFT."""
        _run()
        for case in Case.objects.all():
            assert case.case_type == CaseType.CORRUPTION
            assert case.state == CaseState.DRAFT


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


class TestDiscoverAndDraftCasesIdempotency:
    @pytest.mark.django_db
    def test_skips_case_with_case_number_in_title(self):
        """A pre-existing case containing the case number is not duplicated."""
        _make_case("CIAA Special Court Case 081-CR-0097")
        _run()
        assert Case.objects.filter(title__icontains="081-CR-0097").count() == 1

    @pytest.mark.django_db
    def test_skips_case_with_manually_titled_entry(self):
        """A manually-titled case that contains the number is still skipped."""
        _make_case("Embezzlement 081-CR-0097 investigation")
        _run()
        assert Case.objects.filter(title__icontains="081-CR-0097").count() == 1

    @pytest.mark.django_db
    def test_idempotent_reruns(self):
        """Running the command twice produces the same number of cases."""
        _run()
        count_after_first = Case.objects.count()
        _run()
        assert (
            Case.objects.count() == count_after_first == len(UNIQUE_CIAA_CASE_NUMBERS)
        )

    @pytest.mark.django_db
    def test_partial_existing_creates_only_missing(self):
        """Pre-creating 3 cases means the command creates only missing unique cases."""
        for number in ["081-CR-0022", "081-CR-0087", "081-CR-0097"]:
            _make_case(f"CIAA Special Court Case {number}")
        _run()
        assert Case.objects.count() == len(UNIQUE_CIAA_CASE_NUMBERS)


# ---------------------------------------------------------------------------
# stdout output
# ---------------------------------------------------------------------------


class TestDiscoverAndDraftCasesOutput:
    @pytest.mark.django_db
    def test_summary_shows_all_created(self):
        """First run from empty DB: summary reflects unique creations and duplicate skips."""
        output = _run()
        assert f"{len(UNIQUE_CIAA_CASE_NUMBERS)} created" in output
        assert f"{DUPLICATE_CIAA_CASE_COUNT} skipped" in output

    @pytest.mark.django_db
    def test_summary_shows_all_skipped_on_rerun(self):
        """Second run: all configured case numbers are skipped."""
        _run()
        output = _run()
        assert "0 created" in output
        assert f"{len(CIAA_CASE_NUMBERS)} skipped" in output

    @pytest.mark.django_db
    def test_created_lines_logged(self):
        """Each new case produces a [CREATED] line."""
        output = _run()
        assert output.count("[CREATED]") == len(UNIQUE_CIAA_CASE_NUMBERS)

    @pytest.mark.django_db
    def test_skip_lines_logged(self):
        """Pre-existing case produces a [SKIP] line with its case number."""
        _make_case("CIAA Special Court Case 081-CR-0097")
        output = _run()
        assert "[SKIP]" in output
        assert "081-CR-0097" in output

    @pytest.mark.django_db
    def test_dry_run_does_not_create_cases(self):
        """--dry-run must not write any cases to the database."""
        _run(dry_run=True)
        assert Case.objects.count() == 0

    @pytest.mark.django_db
    def test_dry_run_output_shows_would_create(self):
        """--dry-run output shows [DRY-RUN] lines and correct summary."""
        output = _run(dry_run=True)
        assert "[DRY-RUN]" in output
        assert "would be created" in output
        assert output.count("[DRY-RUN] Would create:") == len(CIAA_CASE_NUMBERS)
