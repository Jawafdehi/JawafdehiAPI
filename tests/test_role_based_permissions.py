"""
Property-based tests for role-based permissions.

Feature: accountability-platform-core
Tests Properties 5, 12, 13, 14
Validates: Requirements 1.5, 3.1, 3.2, 3.3, 5.1, 5.2, 5.3
"""

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from hypothesis import given, settings
from hypothesis import strategies as st

from cases.admin import CaseAdmin
from cases.models import Case, CaseState, CaseType
from tests.conftest import (
    create_case_with_entities,
    create_mock_request,
    create_user_with_role,
)
from tests.strategies import (
    simple_complete_case_data as complete_case_data,
)
from tests.strategies import (
    user_with_role,
)

User = get_user_model()


# ============================================================================
# Property 5: Contributors can only transition between Draft and In Review
# ============================================================================


@pytest.mark.django_db
@settings(max_examples=20, deadline=None)
@given(
    case_data=complete_case_data(),
    contributor_data=user_with_role("Caseworker"),
    target_state=st.sampled_from([CaseState.DRAFT, CaseState.IN_REVIEW]),
)
def test_contributors_cannot_change_cases_via_django_admin(
    case_data, contributor_data, target_state
):
    """
    Feature: accountability-platform-core, Property 5: Django admin is read-only.

    Django admin is read-only; validates that contributors cannot change cases
    (including state transitions) through this surface. State transitions happen
    via the SPA `/admin` panel.
    Validates: Requirements 1.5
    """
    # Create contributor user
    contributor = create_user_with_role(
        contributor_data["username"],
        contributor_data["email"],
        contributor_data["role"],
    )

    # Create a case (v3: no object-level assignment)
    case = create_case_with_entities(**case_data)
    case.save()

    # Set initial state (opposite of target)
    if target_state == CaseState.DRAFT:
        case.state = CaseState.IN_REVIEW
    else:
        case.state = CaseState.DRAFT
    case.save()

    # Create mock request
    request = create_mock_request(contributor)

    # Create admin instance
    admin = CaseAdmin(Case, None)

    # Django admin is read-only: has_change_permission always returns False
    has_change = admin.has_change_permission(request, case)
    assert (
        not has_change
    ), "Django admin is read-only; contributor should NOT have change permission"

    # Contributor can still VIEW the case
    has_view = admin.has_view_permission(request, case)
    assert has_view, "Contributor should have view permission for assigned case"

    # has_add_permission is also False
    has_add = admin.has_add_permission(request)
    assert not has_add, "Django admin does not allow adding cases"


@pytest.mark.django_db
@settings(max_examples=20, deadline=None)
@given(
    case_data=complete_case_data(),
    contributor_data=user_with_role("Caseworker"),
    target_state=st.sampled_from([CaseState.PUBLISHED, CaseState.CLOSED]),
)
def test_caseworker_can_transition_to_published_or_closed(
    case_data, contributor_data, target_state
):
    """
    v3 authz model: the single content-staff role (Caseworker) has the full
    powers the old Moderator had — it CAN transition a case to PUBLISHED or
    CLOSED. (Inverted from the obsolete "contributors confined to DRAFT/IN_REVIEW"
    boundary.) The admin form's ``clean()`` gate must therefore NOT raise a
    state error for a Caseworker.
    Validates: Requirements 2.1
    """
    # Create caseworker user
    caseworker = create_user_with_role(
        contributor_data["username"],
        contributor_data["email"],
        contributor_data["role"],
    )

    # Create a case in IN_REVIEW state (v3: no object-level assignment)
    case = create_case_with_entities(**case_data)
    case.state = CaseState.IN_REVIEW
    case.save()

    # Create mock request
    request = create_mock_request(caseworker)

    # Use the admin form to test the transition-gate validation
    from cases.admin import CaseAdminForm

    form_data = {
        "slug": case.slug,
        "title": case.title,
        "case_type": case.case_type,
        "state": target_state,
        "key_allegations": case.key_allegations,
        "description": case.description,
    }

    form = CaseAdminForm(data=form_data, instance=case, request=request)

    # The transition gate must accept the Caseworker: no state error.
    form.is_valid()
    assert (
        "state" not in form.errors
    ), f"Caseworker should be allowed to transition to {target_state}, got: {form.errors.get('state')}"


# ============================================================================
# Property 12: Admin role-based permissions in Django Admin
# ============================================================================


@pytest.mark.django_db
@settings(max_examples=20, deadline=None)
@given(case_data=complete_case_data(), admin_data=user_with_role("Admin"))
def test_admin_has_full_access_to_all_cases(case_data, admin_data):
    """
    Feature: accountability-platform-core, Property 12: Admin role-based permissions in Django Admin

    Django admin is read-only; validates that Admins can VIEW all cases
    regardless of assignment (queryset is unfiltered), but cannot change or add
    cases through this surface.
    Validates: Requirements 5.1
    """
    # Create admin user
    admin_user = create_user_with_role(
        admin_data["username"], admin_data["email"], admin_data["role"]
    )

    # Create a case (not assigned to admin)
    case = create_case_with_entities(**case_data)

    # Create mock request
    request = create_mock_request(admin_user)

    # Create admin instance
    admin = CaseAdmin(Case, None)

    # Django admin is read-only: has_change_permission always returns False
    has_change = admin.has_change_permission(request, case)
    assert (
        not has_change
    ), "Django admin is read-only; even Admin should NOT have change permission"

    # Admin can VIEW all cases
    has_view = admin.has_view_permission(request, case)
    assert has_view, "Admin should have view permission for all cases"

    # Check that admin can see the case in queryset
    queryset = admin.get_queryset(request)
    assert case in queryset, "Admin should see all cases in queryset"


@pytest.mark.django_db
@settings(max_examples=20, deadline=None)
@given(
    case_data=complete_case_data(),
    admin_data=user_with_role("Admin"),
    target_state=st.sampled_from(
        [CaseState.DRAFT, CaseState.IN_REVIEW, CaseState.PUBLISHED, CaseState.CLOSED]
    ),
)
def test_admin_can_transition_to_any_state(case_data, admin_data, target_state):
    """
    Feature: accountability-platform-core, Property 12: Admin role-based permissions in Django Admin

    For any user with Admin role, they should be able to transition cases to
    any state.
    Validates: Requirements 5.1
    """
    # Create admin user
    admin_user = create_user_with_role(
        admin_data["username"], admin_data["email"], admin_data["role"]
    )

    # Create a case in IN_REVIEW state
    case = create_case_with_entities(**case_data)
    case.state = CaseState.IN_REVIEW
    case.save()

    # Create mock request
    request = create_mock_request(admin_user)

    # Create admin instance
    admin = CaseAdmin(Case, None)

    # Attempt to transition to target state
    case.state = target_state

    # This should succeed without raising ValidationError
    try:
        admin.save_model(request, case, None, change=True)
        success = True
    except ValidationError:
        success = False

    assert success, f"Admin should be able to transition case to {target_state}"


# ============================================================================
# Property 13: Contributor assignment restricts access in Django Admin
# ============================================================================


@pytest.mark.django_db
@settings(max_examples=20, deadline=None)
@given(case_data=complete_case_data(), contributor_data=user_with_role("Caseworker"))
def test_contributor_can_only_access_assigned_cases(case_data, contributor_data):
    """
    Feature: accountability-platform-core, Property 13: Contributor assignment restricts access in Django Admin

    Django admin is read-only; validates that contributors can VIEW all
    non-CLOSED cases (global read access) but cannot CHANGE any case through
    this surface.
    Validates: Requirements 5.2, 3.1, 3.2
    """
    # Create contributor user
    contributor = create_user_with_role(
        contributor_data["username"],
        contributor_data["email"],
        contributor_data["role"],
    )

    # Create two cases (v3: no object-level assignment — both are visible to
    # any Caseworker via global read access).
    assigned_case = create_case_with_entities(**case_data)

    # Create a second case with different title to avoid conflicts
    unassigned_case_data = case_data.copy()
    unassigned_case_data["title"] = f"{case_data['title']}_unassigned"
    unassigned_case = create_case_with_entities(**unassigned_case_data)

    # Create mock request
    request = create_mock_request(contributor)

    # Create admin instance
    admin = CaseAdmin(Case, None)

    # Django admin is read-only: has_change_permission always returns False
    has_change_assigned = admin.has_change_permission(request, assigned_case)
    assert (
        not has_change_assigned
    ), "Django admin is read-only; contributor should NOT have change permission even for assigned case"

    has_change_unassigned = admin.has_change_permission(request, unassigned_case)
    assert (
        not has_change_unassigned
    ), "Django admin is read-only; contributor should NOT have change permission for unassigned case"

    # View permission is role-scoped: contributor can view assigned cases
    has_view_assigned = admin.has_view_permission(request, assigned_case)
    assert (
        has_view_assigned
    ), "Contributor should have view permission for assigned case"

    has_view_unassigned = admin.has_view_permission(request, unassigned_case)
    assert (
        has_view_unassigned
    ), "Contributor should have view permission for unassigned cases (global read access)"

    # Check queryset includes all non-CLOSED cases (global read access)
    queryset = admin.get_queryset(request)
    assert assigned_case in queryset, "Contributor should see assigned case in queryset"
    assert (
        unassigned_case in queryset
    ), "Contributor should see unassigned case in queryset (global read access)"


@pytest.mark.django_db
@settings(max_examples=20, deadline=None)
@given(case_data=complete_case_data(), contributor_data=user_with_role("Caseworker"))
def test_contributor_cannot_modify_unassigned_cases(case_data, contributor_data):
    """
    Feature: accountability-platform-core, Property 13: Contributor assignment restricts access in Django Admin

    For any case not assigned to a Contributor, that Contributor should not be
    able to modify it.
    Validates: Requirements 3.2
    """
    # Create contributor user
    contributor = create_user_with_role(
        contributor_data["username"],
        contributor_data["email"],
        contributor_data["role"],
    )

    # Create a case (not assigned to contributor)
    case = create_case_with_entities(**case_data)

    # Create mock request
    request = create_mock_request(contributor)

    # Create admin instance
    admin = CaseAdmin(Case, None)

    # Check that contributor does NOT have change permission
    has_permission = admin.has_change_permission(request, case)
    assert (
        not has_permission
    ), "Contributor should NOT have change permission for unassigned case"


# ============================================================================
# Property 14: User management is superuser-only in Django Admin
# ============================================================================


@pytest.mark.django_db
@settings(max_examples=10, deadline=None)
@given(
    caseworker_data=user_with_role("Caseworker"),
    other_data=user_with_role("Caseworker"),
)
def test_user_management_is_superuser_only(caseworker_data, other_data):
    """
    v3 authz model: user management is SUPERUSER-ONLY. The old
    "moderators can manage users except other moderators" asymmetry is retired —
    ``can_manage_user`` now returns True only for superusers. A Caseworker (a
    non-superuser) cannot manage any user; a superuser can.
    Validates: Requirements 5.3
    """
    from cases.rules.predicates import can_manage_user

    # A Caseworker (non-superuser content staff).
    caseworker = create_user_with_role(
        caseworker_data["username"], caseworker_data["email"], caseworker_data["role"]
    )

    # Another target user.
    other_user = create_user_with_role(
        other_data["username"], other_data["email"], other_data["role"]
    )

    # A superuser (v3 "admin").
    superuser = create_user_with_role(
        "prop14-admin", "prop14-admin@example.com", "Admin"
    )

    # The Caseworker is not a superuser and cannot manage users.
    assert not caseworker.is_superuser, "Caseworker should not be a superuser"
    assert not can_manage_user(
        caseworker, other_user
    ), "Non-superuser (Caseworker) must NOT be able to manage users"
    assert not can_manage_user(
        caseworker, caseworker
    ), "Non-superuser must NOT be able to manage even itself"

    # The superuser can manage any user.
    assert can_manage_user(
        superuser, other_user
    ), "Superuser should be able to manage users"


@pytest.mark.django_db
@settings(max_examples=20, deadline=None)
@given(case_data=complete_case_data(), moderator_data=user_with_role("Moderator"))
def test_moderators_can_access_all_cases(case_data, moderator_data):
    """
    Feature: accountability-platform-core, Property 14: Moderators cannot manage other Moderators in Django Admin

    Django admin is read-only; validates that Moderators can VIEW all cases
    (queryset is unfiltered) but cannot CHANGE cases through this surface.
    Validates: Requirements 3.3
    """
    # Create moderator user
    moderator = create_user_with_role(
        moderator_data["username"], moderator_data["email"], moderator_data["role"]
    )

    # Create a case (not assigned to moderator)
    case = create_case_with_entities(**case_data)

    # Create mock request
    request = create_mock_request(moderator)

    # Create admin instance
    admin = CaseAdmin(Case, None)

    # Django admin is read-only: has_change_permission always returns False
    has_change = admin.has_change_permission(request, case)
    assert (
        not has_change
    ), "Django admin is read-only; moderator should NOT have change permission"

    # Moderator CAN view all cases
    has_view = admin.has_view_permission(request, case)
    assert has_view, "Moderator should have view permission for all cases"

    # Check that moderator can see the case in queryset
    queryset = admin.get_queryset(request)
    assert case in queryset, "Moderator should see all cases in queryset"


# ============================================================================
# Edge Cases and Additional Tests
# ============================================================================


@pytest.mark.django_db
def test_user_without_role_has_no_access():
    """
    Edge case: Users without any role should have no access to cases.
    """
    # Create user without any role
    user = User.objects.create_user(
        username="norole", email="norole@example.com", password="testpass123"
    )
    user.save()

    # Create a case
    case = create_case_with_entities(
        title="Test Case",
        alleged_entities=["https://jawafdehi.org/entity/person/test-person"],
        key_allegations=["Test allegation"],
        case_type=CaseType.CORRUPTION,
        description="Test description",
    )

    # Create mock request
    request = create_mock_request(user)

    # Create admin instance
    admin = CaseAdmin(Case, None)

    # Check that user has no permission
    has_permission = admin.has_change_permission(request, case)
    assert not has_permission, "User without role should NOT have change permission"

    # Check that queryset is empty
    queryset = admin.get_queryset(request)
    assert case not in queryset, "User without role should not see any cases"


@pytest.mark.django_db
def test_caseworker_can_access_multiple_cases():
    """
    Edge case: a Caseworker has global read access to all cases (v3: no
    object-level assignment).
    """
    # Create caseworker
    caseworker = create_user_with_role("contrib", "contrib@example.com", "Caseworker")

    # Create multiple cases
    case1 = create_case_with_entities(
        title="Case 1",
        alleged_entities=["https://jawafdehi.org/entity/person/person1"],
        key_allegations=["Allegation 1"],
        case_type=CaseType.CORRUPTION,
        description="Description 1",
    )

    case2 = create_case_with_entities(
        title="Case 2",
        alleged_entities=["https://jawafdehi.org/entity/person/person2"],
        key_allegations=["Allegation 2"],
        case_type=CaseType.CORRUPTION,
        description="Description 2",
    )

    # Create mock request
    request = create_mock_request(caseworker)

    # Create admin instance
    admin = CaseAdmin(Case, None)

    # Check queryset includes both cases (global read access)
    queryset = admin.get_queryset(request)
    assert case1 in queryset, "Caseworker should see first case"
    assert case2 in queryset, "Caseworker should see second case"
    assert queryset.count() == 2, "Caseworker should see all 2 cases"
