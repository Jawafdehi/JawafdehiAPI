import pytest
from cases.models import Case, CaseState, CaseType
from tests.conftest import create_case_with_entities

@pytest.mark.django_db
def test_in_review_case_generates_slug():
    """
    Verify that a case in IN_REVIEW state auto-generates a slug on save.
    """
    # Create a draft case (should have no slug)
    case = create_case_with_entities(
        title="Test Case for In Review Slug",
        alleged_entities=["entity:person/test-person-3"],
        key_allegations=["Yet another allegation"],
        case_type=CaseType.CORRUPTION,
        description="Test description 3",
        state=CaseState.DRAFT,
    )
    
    assert case.slug is None, "Draft case should not have a slug"
    
    # Transition to IN_REVIEW
    case.state = CaseState.IN_REVIEW
    case.save()
    
    assert case.slug is not None, "In-review case should have a generated slug"
    assert case.slug.startswith("test-case-for-in-review-slug"), "Slug should be based on title"
