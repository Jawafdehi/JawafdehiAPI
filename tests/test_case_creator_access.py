"""
Tests to ensure case creators always have access to their created cases.

Feature: accountability-platform-core
Validates: Requirements 1.5, 3.1
"""

import pytest
from django.contrib.admin.sites import AdminSite

from cases.admin import CaseAdmin
from cases.models import Case, CaseState, CaseType
from tests.conftest import (
    create_case_with_entities,
    create_mock_request,
    create_user_with_role,
)


@pytest.fixture
def contributor_user(db):
    """Create a contributor user with proper permissions."""
    return create_user_with_role("testcontrib", "contrib@test.com", "Caseworker")


@pytest.fixture
def another_contributor(db):
    """Create another contributor user with proper permissions."""
    return create_user_with_role("anothercontrib", "another@test.com", "Caseworker")


@pytest.fixture
def case_admin():
    """Create a CaseAdmin instance."""
    return CaseAdmin(Case, AdminSite())


@pytest.mark.django_db
def test_creator_automatically_added_to_contributors(contributor_user, case_admin):
    """
    Test that when a contributor creates a case, they are automatically added to contributors.

    Validates: Requirements 1.5, 3.1
    """
    # Create a mock request
    request = create_mock_request(
        contributor_user, method="post", path="/django-admin/cases/case/add/"
    )

    # Create a new case
    case = create_case_with_entities(
        title="Test Case",
        case_type=CaseType.CORRUPTION,
        alleged_entities=["https://jawafdehi.org/entity/person/test-person"],
        state=CaseState.DRAFT,
    )

    # Create a mock form
    class MockForm:
        instance = case

        def save_m2m(self):
            pass  # No-op for mock

    form = MockForm()

    # Simulate form submission (change=False means new object)
    # Django admin calls both save_model and save_related
    case_admin.save_model(request, case, form, change=False)
    case_admin.save_related(request, form, [], change=False)

    # Verify the creator is in contributors
    assert case.contributors.filter(
        id=contributor_user.id
    ).exists(), "Creator should be automatically added to contributors"

    assert (
        contributor_user in case.contributors.all()
    ), "Creator should be in the contributors list"


@pytest.mark.django_db
def test_creator_has_view_permission(contributor_user, case_admin):
    """
    Test that the creator has view permission for their created case.

    Validates: Requirements 1.5, 3.1
    """

    # Create a case
    case = create_case_with_entities(
        title="Test Case",
        case_type=CaseType.CORRUPTION,
        alleged_entities=["https://jawafdehi.org/entity/person/test-person"],
        state=CaseState.DRAFT,
    )

    # Simulate the creator being added (as would happen in save_model)
    case.contributors.add(contributor_user)

    # Create a mock request
    request = create_mock_request(contributor_user)

    # Check view permission
    has_permission = case_admin.has_view_permission(request, case)

    assert has_permission, "Creator should have view permission for their created case"


@pytest.mark.django_db
def test_creator_has_change_permission(contributor_user, case_admin):
    """
    Test that the creator has change permission for their created case.

    Validates: Requirements 1.5, 3.1
    """

    # Create a case
    case = create_case_with_entities(
        title="Test Case",
        case_type=CaseType.CORRUPTION,
        alleged_entities=["https://jawafdehi.org/entity/person/test-person"],
        state=CaseState.DRAFT,
    )

    # Simulate the creator being added (as would happen in save_model)
    case.contributors.add(contributor_user)

    # Create a mock request
    request = create_mock_request(contributor_user)

    # Check change permission
    has_permission = case_admin.has_change_permission(request, case)

    assert (
        has_permission
    ), "Creator should have change permission for their created case"


@pytest.mark.django_db
def test_non_creator_contributor_cannot_access(
    contributor_user, another_contributor, case_admin
):
    """
    Test that a contributor who didn't create the case cannot access it.

    Validates: Requirements 1.5, 3.1
    """

    # Create a case by first contributor
    case = create_case_with_entities(
        title="Test Case",
        case_type=CaseType.CORRUPTION,
        alleged_entities=["https://jawafdehi.org/entity/person/test-person"],
        state=CaseState.DRAFT,
    )
    case.contributors.add(contributor_user)

    # Try to access with another contributor
    request = create_mock_request(another_contributor)

    # Check view permission — Contributors now have global read access
    has_view = case_admin.has_view_permission(request, case)
    has_change = case_admin.has_change_permission(request, case)

    assert (
        has_view
    ), "Contributor should have view permission for all cases (global read access)"
    assert not has_change, "Non-creator contributor should NOT have change permission"


@pytest.mark.django_db
def test_creator_can_see_case_in_queryset(
    contributor_user, another_contributor, case_admin
):
    """
    Test that the creator can see their case in the admin queryset.

    Validates: Requirements 1.5, 3.1
    """

    # Create cases by different contributors
    case1 = create_case_with_entities(
        title="Case by Contributor 1",
        case_type=CaseType.CORRUPTION,
        alleged_entities=["https://jawafdehi.org/entity/person/test-person"],
        state=CaseState.DRAFT,
    )
    case1.contributors.add(contributor_user)

    case2 = create_case_with_entities(
        title="Case by Another Contributor",
        case_type=CaseType.CORRUPTION,
        alleged_entities=["https://jawafdehi.org/entity/person/test-person"],
        state=CaseState.DRAFT,
    )
    case2.contributors.add(another_contributor)

    # Get queryset for first contributor
    request = create_mock_request(contributor_user)

    queryset = case_admin.get_queryset(request)

    # Should see all non-CLOSED cases (global read access)
    assert case1 in queryset, "Creator should see their own case in queryset"

    assert (
        case2 in queryset
    ), "Creator should see other contributors' cases (global read access)"

    assert (
        queryset.count() >= 2
    ), f"Creator should see at least 2 cases, but saw {queryset.count()}"


@pytest.mark.django_db
def test_multiple_cases_by_same_creator(contributor_user, case_admin):
    """
    Test that a creator can access all cases they created.

    Validates: Requirements 1.5, 3.1
    """

    # Create multiple cases
    case1 = create_case_with_entities(
        title="Case 1",
        case_type=CaseType.CORRUPTION,
        alleged_entities=["https://jawafdehi.org/entity/person/test-person"],
        state=CaseState.DRAFT,
    )
    case1.contributors.add(contributor_user)

    case2 = create_case_with_entities(
        title="Case 2",
        case_type=CaseType.CORRUPTION,
        alleged_entities=["https://jawafdehi.org/entity/person/test-person"],
        state=CaseState.DRAFT,
    )
    case2.contributors.add(contributor_user)

    case3 = create_case_with_entities(
        title="Case 3",
        case_type=CaseType.CORRUPTION,
        alleged_entities=["https://jawafdehi.org/entity/person/test-person"],
        state=CaseState.IN_REVIEW,
    )
    case3.contributors.add(contributor_user)

    # Get queryset for contributor
    request = create_mock_request(contributor_user)

    queryset = case_admin.get_queryset(request)

    # Should see all their cases
    assert (
        queryset.count() == 3
    ), f"Creator should see all 3 of their cases, but saw {queryset.count()}"

    assert case1 in queryset, "Creator should see case 1"
    assert case2 in queryset, "Creator should see case 2"
    assert case3 in queryset, "Creator should see case 3"


@pytest.mark.django_db
def test_creator_access_persists_after_state_change(contributor_user, case_admin):
    """
    Test that creator access persists even after case state changes.

    Validates: Requirements 1.5, 3.1
    """

    # Create a case in DRAFT
    case = create_case_with_entities(
        title="Test Case",
        case_type=CaseType.CORRUPTION,
        alleged_entities=["https://jawafdehi.org/entity/person/test-person"],
        key_allegations=["Test allegation"],
        description="Test description",
        state=CaseState.DRAFT,
    )
    case.contributors.add(contributor_user)

    # Create a mock request
    request = create_mock_request(contributor_user)

    # Check access in DRAFT state
    assert case_admin.has_view_permission(
        request, case
    ), "Creator should have access in DRAFT state"

    # Change to IN_REVIEW
    case.state = CaseState.IN_REVIEW
    case.save()

    # Check access still exists
    assert case_admin.has_view_permission(
        request, case
    ), "Creator should still have access in IN_REVIEW state"

    # Verify contributor is still in the list
    assert (
        contributor_user in case.contributors.all()
    ), "Creator should still be in contributors after state change"
