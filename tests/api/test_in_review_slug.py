import pytest
from cases.models import CaseState, CaseType
from tests.conftest import create_case_with_entities


@pytest.mark.django_db
def test_case_auto_generates_slug_on_save():
    """
    Slug-only API contract: every case has a slug after save, regardless of
    state. The slug is derived from the title.
    """
    case = create_case_with_entities(
        title="Test Case for Slug Generation",
        alleged_entities=["entity:person/test-person-3"],
        key_allegations=["Yet another allegation"],
        case_type=CaseType.CORRUPTION,
        description="Test description 3",
        state=CaseState.DRAFT,
    )

    assert case.slug, "Case should have an auto-generated slug after save"
    assert case.slug.startswith(
        "test-case-for-slug-generation"
    ), "Slug should be derived from the title"
