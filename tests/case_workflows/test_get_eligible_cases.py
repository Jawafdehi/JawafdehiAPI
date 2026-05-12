"""
Tests for CIAACaseworkerWorkflow.get_eligible_cases().

Covers:
- Returns DRAFT and IN_REVIEW CORRUPTION cases whose titles contain a
  known CIAA case number
- Excludes CLOSED cases (explicit user requirement)
- Excludes PUBLISHED cases
- Excludes non-CIAA CORRUPTION drafts (title contains no case number)
- Excludes cases with a completed CaseWorkflowRun
- Still includes cases with a failed (but not completed) CaseWorkflowRun
"""

import pytest

from case_workflows.models import CaseWorkflowRun
from case_workflows.workflows.ciaa_caseworker.constants import CIAA_CASE_NUMBERS
from case_workflows.workflows.ciaa_caseworker.workflow import CIAACaseworkerWorkflow
from cases.models import Case, CaseState, CaseType

# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def workflow():
    return CIAACaseworkerWorkflow()

def make_ciaa_case(
    case_number: str = "081-CR-0097", state: str = CaseState.DRAFT
) -> Case:
    """Create a Case with the canonical CIAA title for the given case number."""
    return Case.objects.create(
        title=f"CIAA Special Court Case {case_number}",
        case_type=CaseType.CORRUPTION,
        state=state,
    )

# ---------------------------------------------------------------------------
# Inclusion
# ---------------------------------------------------------------------------

class TestGetEligibleCasesInclusion:
    @pytest.mark.django_db
    def test_returns_draft_ciaa_case(self, workflow):
        """A DRAFT CIAA case is included."""
        case = make_ciaa_case("081-CR-0097", CaseState.DRAFT)
        assert case.case_id in workflow.get_eligible_cases()

    @pytest.mark.django_db
    def test_returns_in_review_ciaa_case(self, workflow):
        """An IN_REVIEW CIAA case is included."""
        case = make_ciaa_case("081-CR-0091", CaseState.IN_REVIEW)
        assert case.case_id in workflow.get_eligible_cases()

    @pytest.mark.django_db
    def test_returns_all_cases_from_canonical_list(self, workflow):
        """All 19 canonical CIAA cases are returned when all are DRAFT."""
        for number in CIAA_CASE_NUMBERS:
            make_ciaa_case(number, CaseState.DRAFT)
        eligible = workflow.get_eligible_cases()
        assert len(eligible) == len(CIAA_CASE_NUMBERS)

# ---------------------------------------------------------------------------
# Exclusion
# ---------------------------------------------------------------------------

class TestGetEligibleCasesExclusion:
    @pytest.mark.django_db
    def test_excludes_closed_cases(self, workflow):
        """CLOSED cases must never be returned (explicit requirement)."""
        case = make_ciaa_case("081-CR-0097", CaseState.CLOSED)
        assert case.case_id not in workflow.get_eligible_cases()

    @pytest.mark.django_db
    def test_excludes_published_cases(self, workflow):
        """PUBLISHED cases are not eligible for the workflow."""
        case = make_ciaa_case("081-CR-0097", CaseState.PUBLISHED)
        assert case.case_id not in workflow.get_eligible_cases()

    @pytest.mark.django_db
    def test_excludes_non_ciaa_corruption_draft(self, workflow):
        """A CORRUPTION DRAFT whose title has no CIAA case number is excluded."""
        case = Case.objects.create(
            title="Embezzlement case in Kailali",
            case_type=CaseType.CORRUPTION,
            state=CaseState.DRAFT,
        )
        assert case.case_id not in workflow.get_eligible_cases()

    @pytest.mark.django_db
    def test_excludes_completed_workflow_runs(self, workflow):
        """A case with a completed CaseWorkflowRun is not eligible."""
        case = make_ciaa_case("081-CR-0097", CaseState.DRAFT)
        CaseWorkflowRun.objects.create(
            case_id=case.case_id,
            workflow_id=workflow.workflow_id,
            is_complete=True,
        )
        assert case.case_id not in workflow.get_eligible_cases()

    @pytest.mark.django_db
    def test_includes_case_with_failed_workflow_run(self, workflow):
        """A case whose run has failed but is not complete is still eligible."""
        case = make_ciaa_case("081-CR-0097", CaseState.DRAFT)
        CaseWorkflowRun.objects.create(
            case_id=case.case_id,
            workflow_id=workflow.workflow_id,
            is_complete=False,
            has_failed=True,
        )
        assert case.case_id in workflow.get_eligible_cases()
