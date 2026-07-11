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
def test_admin_cannot_add_cases(contributor_user, case_admin):
    """
    Django admin is read-only: the SPA `/admin` panel is the sole case write
    surface. No role can add a case through Django admin.

    Validates: SPA-is-sole-write-surface (Django-admin case decommission).
    """
    request = create_mock_request(
        contributor_user, method="post", path="/django-admin/cases/case/add/"
    )
    assert not case_admin.has_add_permission(
        request
    ), "Django admin must not allow adding cases (SPA is the sole write surface)"


@pytest.mark.django_db
def test_caseworker_has_view_permission(contributor_user, case_admin):
    """
    v3 authz model: object-level assignment is retired — the content-staff
    (Caseworker) role has global read access to any case.

    Validates: Requirements 1.5, 3.1
    """

    # Create a case
    case = create_case_with_entities(
        title="Test Case",
        case_type=CaseType.CORRUPTION,
        alleged_entities=["https://jawafdehi.org/entity/person/test-person"],
        state=CaseState.DRAFT,
    )

    # Create a mock request
    request = create_mock_request(contributor_user)

    # Check view permission
    has_permission = case_admin.has_view_permission(request, case)

    assert has_permission, "Caseworker should have view permission for any case"


@pytest.mark.django_db
def test_admin_is_read_only_even_for_creator(contributor_user, case_admin):
    """
    Django admin is uniformly read-only: even the creator cannot change a case
    through this surface. Write access is the SPA `/admin` panel's domain.

    Validates: SPA-is-sole-write-surface (Django-admin case decommission).
    """

    # Create a case
    case = create_case_with_entities(
        title="Test Case",
        case_type=CaseType.CORRUPTION,
        alleged_entities=["https://jawafdehi.org/entity/person/test-person"],
        state=CaseState.DRAFT,
    )

    request = create_mock_request(contributor_user)

    assert not case_admin.has_change_permission(
        request, case
    ), "Django admin must not allow editing cases (SPA is the sole write surface)"
    assert not case_admin.has_delete_permission(
        request, case
    ), "Django admin must not allow deleting cases"


@pytest.mark.django_db
def test_other_caseworker_has_global_read_but_no_admin_change(
    contributor_user, another_contributor, case_admin
):
    """
    v3 authz model: any Caseworker has global read access to any case, but
    Django admin is uniformly read-only (change is the SPA's domain).

    Validates: Requirements 1.5, 3.1
    """

    # Create a case (no object-level assignment in v3)
    case = create_case_with_entities(
        title="Test Case",
        case_type=CaseType.CORRUPTION,
        alleged_entities=["https://jawafdehi.org/entity/person/test-person"],
        state=CaseState.DRAFT,
    )

    # Access with another caseworker
    request = create_mock_request(another_contributor)

    # Check view permission — Caseworkers have global read access
    has_view = case_admin.has_view_permission(request, case)
    has_change = case_admin.has_change_permission(request, case)

    assert (
        has_view
    ), "Caseworker should have view permission for all cases (global read access)"
    assert not has_change, "Django admin is read-only; no change permission"


@pytest.mark.django_db
def test_caseworker_sees_all_cases_in_queryset(
    contributor_user, another_contributor, case_admin
):
    """
    v3 authz model: a Caseworker sees ALL cases in the admin queryset (global
    read access), regardless of who created them.

    Validates: Requirements 1.5, 3.1
    """

    # Create cases (no object-level assignment in v3)
    case1 = create_case_with_entities(
        title="Case by Contributor 1",
        case_type=CaseType.CORRUPTION,
        alleged_entities=["https://jawafdehi.org/entity/person/test-person"],
        state=CaseState.DRAFT,
    )

    case2 = create_case_with_entities(
        title="Case by Another Contributor",
        case_type=CaseType.CORRUPTION,
        alleged_entities=["https://jawafdehi.org/entity/person/test-person"],
        state=CaseState.DRAFT,
    )

    # Get queryset for a caseworker
    request = create_mock_request(contributor_user)

    queryset = case_admin.get_queryset(request)

    # Should see all non-CLOSED cases (global read access)
    assert case1 in queryset, "Caseworker should see case1 in queryset"

    assert case2 in queryset, "Caseworker should see all cases (global read access)"

    assert (
        queryset.count() >= 2
    ), f"Caseworker should see at least 2 cases, but saw {queryset.count()}"


@pytest.mark.django_db
def test_caseworker_sees_multiple_cases(contributor_user, case_admin):
    """
    v3 authz model: a Caseworker sees all non-CLOSED cases in the queryset.

    Validates: Requirements 1.5, 3.1
    """

    # Create multiple cases
    case1 = create_case_with_entities(
        title="Case 1",
        case_type=CaseType.CORRUPTION,
        alleged_entities=["https://jawafdehi.org/entity/person/test-person"],
        state=CaseState.DRAFT,
    )

    case2 = create_case_with_entities(
        title="Case 2",
        case_type=CaseType.CORRUPTION,
        alleged_entities=["https://jawafdehi.org/entity/person/test-person"],
        state=CaseState.DRAFT,
    )

    case3 = create_case_with_entities(
        title="Case 3",
        case_type=CaseType.CORRUPTION,
        alleged_entities=["https://jawafdehi.org/entity/person/test-person"],
        state=CaseState.IN_REVIEW,
    )

    # Get queryset for the caseworker
    request = create_mock_request(contributor_user)

    queryset = case_admin.get_queryset(request)

    # Should see all cases
    assert (
        queryset.count() == 3
    ), f"Caseworker should see all 3 cases, but saw {queryset.count()}"

    assert case1 in queryset, "Caseworker should see case 1"
    assert case2 in queryset, "Caseworker should see case 2"
    assert case3 in queryset, "Caseworker should see case 3"


@pytest.mark.django_db
def test_caseworker_access_persists_after_state_change(contributor_user, case_admin):
    """
    v3 authz model: a Caseworker's global read access is unaffected by case
    state changes.

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

    # Create a mock request
    request = create_mock_request(contributor_user)

    # Check access in DRAFT state
    assert case_admin.has_view_permission(
        request, case
    ), "Caseworker should have access in DRAFT state"

    # Change to IN_REVIEW
    case.state = CaseState.IN_REVIEW
    case.save()

    # Check access still exists
    assert case_admin.has_view_permission(
        request, case
    ), "Caseworker should still have access in IN_REVIEW state"
