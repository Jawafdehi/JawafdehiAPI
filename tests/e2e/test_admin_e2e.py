"""
End-to-End tests for Django Admin workflows.

Feature: accountability-platform-core
Tests complete admin workflows including case management, permissions, and versioning
Validates: Requirements 1.1, 1.2, 1.3, 1.4, 2.1, 2.2, 2.3, 2.4, 3.1, 3.2, 3.3, 5.1, 5.2, 5.3, 7.1, 7.3
"""

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import Client

from cases.admin import CaseAdmin
from cases.models import (
    Case,
    CaseEntityRelationship,
    CaseState,
    CaseType,
    RelationshipType,
)
from tests.byline import credit_author
from tests.conftest import (
    create_case_with_entities,
    create_entities_from_ids,
    create_user_with_role,
)

User = get_user_model()


# ============================================================================
# E2E Test Class
# ============================================================================


@pytest.mark.django_db
class TestDjangoAdminWorkflows:
    """
    End-to-end tests for Django Admin workflows.

    These tests simulate complete user journeys through the Django Admin,
    testing the integration of case management, permissions, and versioning.
    """

    def setup_method(self):
        """Set up test data for each test."""
        # Create users with different roles
        self.admin = create_user_with_role("admin", "admin@example.com", "Admin")
        self.moderator = create_user_with_role(
            "moderator", "moderator@example.com", "Moderator"
        )
        self.contributor1 = create_user_with_role(
            "contributor1", "contributor1@example.com", "Caseworker"
        )
        self.contributor2 = create_user_with_role(
            "contributor2", "contributor2@example.com", "Caseworker"
        )

        # Create Django test client
        self.client = Client()

    def test_create_draft_edit_submit_review_publish_workflow(self):
        """
        E2E Test: Django admin is read-only; validates that no role can add or
        change cases through this surface.

        The SPA `/admin` panel is the sole write surface for case lifecycle
        (create/edit/submit/publish). This test verifies the Django admin
        read-only contract: has_add_permission and has_change_permission both
        return False for all roles, while has_view_permission remains functional.

        Validates: Requirements 1.1, 1.2, 1.3, 2.1, 2.2
        """
        # Create a case in various states to verify view access
        case = create_case_with_entities(
            title="New Corruption Case",
            alleged_entities=["https://jawafdehi.org/entity/person/test-official"],
            key_allegations=["Initial allegation"],
            case_type=CaseType.CORRUPTION,
            description="Initial draft description",
            state=CaseState.DRAFT,
        )

        admin_instance = CaseAdmin(Case, None)
        from django.test import RequestFactory

        factory = RequestFactory()

        # Verify contributor CANNOT add or change via Django admin
        request_contrib = factory.get("/")
        request_contrib.user = self.contributor1

        assert not admin_instance.has_add_permission(
            request_contrib
        ), "Django admin is read-only; contributor cannot add cases"
        assert not admin_instance.has_change_permission(
            request_contrib, case
        ), "Django admin is read-only; contributor cannot change cases"
        assert admin_instance.has_view_permission(
            request_contrib, case
        ), "Contributor should have view permission for assigned case"

        # Verify moderator CANNOT change via Django admin either
        request_mod = factory.get("/")
        request_mod.user = self.moderator

        assert not admin_instance.has_add_permission(
            request_mod
        ), "Django admin is read-only; moderator cannot add cases"
        assert not admin_instance.has_change_permission(
            request_mod, case
        ), "Django admin is read-only; moderator cannot change cases"
        assert admin_instance.has_view_permission(
            request_mod, case
        ), "Moderator should have view permission for all cases"

        # Verify admin CANNOT change via Django admin either
        request_admin = factory.get("/")
        request_admin.user = self.admin

        assert not admin_instance.has_add_permission(
            request_admin
        ), "Django admin is read-only; admin cannot add cases"
        assert not admin_instance.has_change_permission(
            request_admin, case
        ), "Django admin is read-only; admin cannot change cases"
        assert admin_instance.has_view_permission(
            request_admin, case
        ), "Admin should have view permission for all cases"

    def test_caseworker_global_view_access(self):
        """
        E2E Test: v3 authz — object-level case assignment is retired. Every
        Caseworker has global VIEW access to all cases through Django admin,
        and no role can CHANGE cases here (Django admin is read-only).

        Workflow:
        1. Create a case (no assignment concept)
        2. Both caseworkers can VIEW the case (queryset + view permission)
        3. Neither caseworker can CHANGE via Django admin (read-only)

        Validates: Requirements 3.1, 3.2, 5.2
        """
        # Step 1: Create a case — there is no per-case assignment in v3.
        case = create_case_with_entities(
            title="Any Case",
            alleged_entities=["https://jawafdehi.org/entity/person/test"],
            key_allegations=["Test allegation"],
            case_type=CaseType.CORRUPTION,
            description="Test description",
            state=CaseState.DRAFT,
        )

        admin_instance = CaseAdmin(Case, None)
        from django.test import RequestFactory

        factory = RequestFactory()

        # Step 2: Both caseworkers see and can view the case (global read access).
        for user in (self.contributor1, self.contributor2):
            request = factory.get("/")
            request.user = user

            queryset = admin_instance.get_queryset(request)
            assert (
                case in queryset
            ), "Every caseworker should see any case in queryset (global read access)"

            assert admin_instance.has_view_permission(
                request, case
            ), "Every caseworker should have view permission for any case"

            # Step 3: Django admin is read-only — no caseworker can change cases.
            assert not admin_instance.has_change_permission(
                request, case
            ), "Django admin is read-only; caseworker cannot change cases"

    def test_contributor_can_see_own_created_case_in_list(self):
        """
        E2E Test: Django admin is read-only; validates that a caseworker can
        VIEW any case in the list and detail views (global read access in v3),
        and no caseworker has change permission.

        Validates: Requirements 3.1, 3.2
        """
        admin_instance = CaseAdmin(Case, None)
        from django.test import RequestFactory

        factory = RequestFactory()

        request_contrib1 = factory.get("/")
        request_contrib1.user = self.contributor1

        # Create a case — v3 has no per-case assignment; any caseworker can view.
        case = create_case_with_entities(
            title="Contributor's New Case",
            alleged_entities=["https://jawafdehi.org/entity/person/test-official"],
            key_allegations=["Test allegation"],
            case_type=CaseType.CORRUPTION,
            description="Case created by contributor1",
            state=CaseState.DRAFT,
        )

        # Contributor performs list query: case appears in queryset
        queryset = admin_instance.get_queryset(request_contrib1)
        assert (
            case in queryset
        ), "Contributor should see their own case in list view (Requirement 3.1)"

        # Contributor has VIEW permission
        has_view_permission = admin_instance.has_view_permission(request_contrib1, case)
        assert (
            has_view_permission
        ), "Contributor should have view permission for their own case"

        # Django admin is read-only: NO change permission
        has_change_permission = admin_instance.has_change_permission(
            request_contrib1, case
        )
        assert (
            not has_change_permission
        ), "Django admin is read-only; contributor cannot change cases"

        # has_add_permission is also False
        assert not admin_instance.has_add_permission(
            request_contrib1
        ), "Django admin is read-only; contributor cannot add cases"

        # Another contributor can view but not change the case
        request_contrib2 = factory.get("/")
        request_contrib2.user = self.contributor2

        queryset2 = admin_instance.get_queryset(request_contrib2)
        assert (
            case in queryset2
        ), "Other contributors should see unassigned cases in list view (global read access)"

        has_view_permission2 = admin_instance.has_view_permission(
            request_contrib2, case
        )
        assert (
            has_view_permission2
        ), "Other contributors should have view permission for unassigned cases"

        has_change_permission2 = admin_instance.has_change_permission(
            request_contrib2, case
        )
        assert (
            not has_change_permission2
        ), "Django admin is read-only; other contributor cannot change cases"

    def test_state_transitions_with_validation(self):
        """
        E2E Test: Verify state transitions are validated correctly.

        Workflow:
        1. Create a draft case with minimal data
        2. Attempt to transition to IN_REVIEW without required fields (should fail)
        3. Add required fields
        4. Successfully transition to IN_REVIEW
        5. Caseworker publishes (allowed in v3 — single content-staff role)

        Validates: Requirements 1.2, 1.5, 2.1
        """
        # Step 1: Create a draft case with minimal data
        case = create_case_with_entities(
            title="Minimal Draft",
            alleged_entities=["https://jawafdehi.org/entity/person/test"],
            case_type=CaseType.CORRUPTION,
            state=CaseState.DRAFT,
        )

        # Step 2: Attempt to transition to IN_REVIEW without required fields
        case.state = CaseState.IN_REVIEW

        with pytest.raises(ValidationError) as exc_info:
            case.validate()

        # Verify validation error mentions missing fields
        error_dict = exc_info.value.message_dict
        assert (
            "key_allegations" in error_dict or "description" in error_dict
        ), "Validation should fail for IN_REVIEW without required fields (Requirement 1.2)"

        # Reset state
        case.state = CaseState.DRAFT
        case.save()

        # Step 3: Add required fields
        case.key_allegations = ["Complete allegation statement"]
        case.description = "Complete description with sufficient detail"
        case.save()
        credit_author(case)

        # Step 4: Successfully transition to IN_REVIEW
        case.submit()
        case.refresh_from_db()
        assert (
            case.state == CaseState.IN_REVIEW
        ), "Case should transition to IN_REVIEW with complete data"

        # Step 5: Caseworker publishes — allowed in v3 (the single content-staff
        # role can transition to any state, including PUBLISHED).
        from django.test import RequestFactory

        from cases.admin import CaseAdminForm

        factory = RequestFactory()
        request_contrib = factory.post("/")
        request_contrib.user = self.contributor1

        form_data = {
            "slug": case.slug,
            "title": case.title,
            "case_type": case.case_type,
            "state": CaseState.PUBLISHED,
            "key_allegations": case.key_allegations,
            "description": case.description,
        }
        form = CaseAdminForm(data=form_data, instance=case, request=request_contrib)
        assert (
            form.is_valid()
        ), f"Form should be valid for caseworker publishing: {form.errors}"
        form.save()

        case.refresh_from_db()
        assert (
            case.state == CaseState.PUBLISHED
        ), "Caseworker should be able to publish the case (Requirement 2.1)"

    def test_in_place_editing_of_published_cases(self):
        """
        E2E Test: Verify editing a published case updates it in-place.

        Workflow:
        1. Create and publish a case
        2. Edit the published case in-place
        3. Verify changes are saved
        4. Verify only one row exists per case_id

        Validates: Requirements 1.4
        """
        # Step 1: Create and publish a case
        case = create_case_with_entities(
            title="Original Case Title",
            alleged_entities=["https://jawafdehi.org/entity/person/original"],
            key_allegations=["Original allegation"],
            case_type=CaseType.CORRUPTION,
            description="Original description",
            state=CaseState.PUBLISHED,
        )

        original_slug = case.slug
        case_db_id = case.id

        # Step 2: Edit the case in-place
        case.title = "Updated Case Title"
        case.key_allegations = ["Original allegation", "New allegation"]
        case.description = "Updated description with new information"
        case.save()

        # Step 3: Verify changes are saved
        case.refresh_from_db()
        assert case.title == "Updated Case Title"
        assert len(case.key_allegations) == 2
        assert case.id == case_db_id, "Should be the same database record"
        assert case.slug == original_slug, "slug should be unchanged"

        # Step 4: Verify only one row exists for this case_id
        row_count = Case.objects.filter(slug=original_slug).count()
        assert row_count == 1, "There should be exactly one row per slug"

    def test_soft_deletion(self):
        """
        E2E Test: Verify soft deletion sets state to CLOSED and preserves data.

        Workflow:
        1. Create a published case
        2. Soft delete the case (using delete() method)
        3. Verify state is set to CLOSED
        4. Verify case still exists in database
        5. Verify versionInfo records the deletion
        6. Verify case is not visible in public API

        Validates: Requirements 7.3
        """
        # Step 1: Create a published case
        case = create_case_with_entities(
            title="Case to be Deleted",
            alleged_entities=["https://jawafdehi.org/entity/person/test"],
            key_allegations=["Test allegation"],
            case_type=CaseType.CORRUPTION,
            description="Test description",
            state=CaseState.PUBLISHED,
        )

        case_id = case.id

        # Step 2: Soft delete the case
        result = case.delete()

        # Verify delete() returns expected tuple
        assert result == (
            0,
            {"cases.Case": 0},
        ), "Soft delete should report 0 actual deletions"

        # Step 3: Verify state is set to CLOSED
        case.refresh_from_db()
        assert (
            case.state == CaseState.CLOSED
        ), "Soft delete should set state to CLOSED (Requirement 7.3)"

        # Step 4: Verify case still exists in database
        deleted_case = Case.objects.get(id=case_id)
        assert (
            deleted_case is not None
        ), "Case should still exist in database after soft delete"
        assert (
            deleted_case.title == "Case to be Deleted"
        ), "Case data should be preserved"

        # Step 5: Verify versionInfo records the deletion
        assert deleted_case.versionInfo is not None
        assert (
            deleted_case.versionInfo.get("action") == "deleted"
        ), "versionInfo should record the deletion action"
        assert (
            "datetime" in deleted_case.versionInfo
        ), "versionInfo should include deletion timestamp"

        # Step 6: Verify case is not visible in public API
        # (This would be tested by checking the API queryset filters)
        # For now, we verify the state is CLOSED which the API filters out
        assert deleted_case.state == CaseState.CLOSED

        # Verify we can query all cases including closed ones
        all_cases = Case.objects.all()
        assert deleted_case in all_cases, "Closed case should be queryable with all()"

        # Verify filtering by state works
        closed_cases = Case.objects.filter(state=CaseState.CLOSED)
        assert deleted_case in closed_cases, "Should be able to filter for closed cases"

    def test_admin_full_access_workflow(self):
        """
        E2E Test: Django admin is read-only; validates that Admin can VIEW all
        cases across all states and contributors, but cannot CHANGE cases
        through this surface. Admin retains user management capability.

        Validates: Requirements 5.1
        """
        # Create cases assigned to different contributors in various states
        case1 = create_case_with_entities(
            title="Case for Contributor 1",
            alleged_entities=["https://jawafdehi.org/entity/person/test1"],
            key_allegations=["Allegation 1"],
            case_type=CaseType.CORRUPTION,
            description="Description 1",
            state=CaseState.DRAFT,
        )

        case2 = create_case_with_entities(
            title="Case for Contributor 2",
            alleged_entities=["https://jawafdehi.org/entity/person/test2"],
            key_allegations=["Allegation 2"],
            case_type=CaseType.CORRUPTION,
            description="Description 2",
            state=CaseState.IN_REVIEW,
        )

        admin_instance = CaseAdmin(Case, None)
        from django.test import RequestFactory

        factory = RequestFactory()

        request_admin = factory.get("/")
        request_admin.user = self.admin

        # Admin can SEE all cases in queryset
        queryset = admin_instance.get_queryset(request_admin)
        assert case1 in queryset, "Admin should see case assigned to contributor1"
        assert case2 in queryset, "Admin should see case assigned to contributor2"

        # Admin has VIEW permission for all cases
        assert admin_instance.has_view_permission(
            request_admin, case1
        ), "Admin should have view permission for any case"
        assert admin_instance.has_view_permission(
            request_admin, case2
        ), "Admin should have view permission for any case"

        # Django admin is read-only: Admin CANNOT change cases
        assert not admin_instance.has_change_permission(
            request_admin, case1
        ), "Django admin is read-only; admin cannot change cases"
        assert not admin_instance.has_change_permission(
            request_admin, case2
        ), "Django admin is read-only; admin cannot change cases"
        assert not admin_instance.has_add_permission(
            request_admin
        ), "Django admin is read-only; admin cannot add cases"
        assert not admin_instance.has_delete_permission(
            request_admin, case1
        ), "Django admin is read-only; admin cannot delete cases"

        # Admin is a superuser and can still manage users
        assert self.admin.is_superuser, "Admin should be a superuser"

        from cases.admin import CustomUserAdmin

        user_admin = CustomUserAdmin(User, None)

        user_queryset = user_admin.get_queryset(request_admin)
        assert (
            self.moderator in user_queryset
        ), "Admin should be able to see moderator users"

        assert user_admin.has_change_permission(
            request_admin, self.moderator
        ), "Admin should be able to change moderator users"

    def test_user_management_is_superuser_only(self):
        """
        E2E Test: v3 authz — user management is SUPERUSER-ONLY.

        The old "moderators manage users but not other moderators" asymmetry is
        retired. A non-superuser content-staff user (Caseworker) can neither see
        nor change users; only a superuser can.

        Workflow:
        1. A Caseworker sees NO users in the user queryset
        2. A Caseworker cannot change or delete any user
        3. A superuser sees all users and can change/delete them

        Validates: Requirements 5.3
        """
        from django.test import RequestFactory

        from cases.admin import CustomUserAdmin

        user_admin = CustomUserAdmin(User, None)
        factory = RequestFactory()

        # Step 1: A Caseworker (self.moderator maps to Caseworker) sees no users.
        request_caseworker = factory.get("/")
        request_caseworker.user = self.moderator

        caseworker_queryset = user_admin.get_queryset(request_caseworker)
        assert (
            caseworker_queryset.count() == 0
        ), "Non-superuser (Caseworker) should see no users (user mgmt is superuser-only)"

        # Step 2: A Caseworker cannot change or delete any user.
        assert not user_admin.has_change_permission(
            request_caseworker
        ), "Caseworker should NOT have change permission on users"
        assert not user_admin.has_change_permission(
            request_caseworker, self.contributor1
        ), "Caseworker should NOT be able to change another user"
        assert not user_admin.has_delete_permission(
            request_caseworker, self.contributor1
        ), "Caseworker should NOT be able to delete another user"

        # Step 3: A superuser can see and manage all users.
        request_admin = factory.get("/")
        request_admin.user = self.admin

        admin_queryset = user_admin.get_queryset(request_admin)
        assert (
            self.moderator in admin_queryset
        ), "Superuser should see all users in queryset"
        assert (
            self.contributor1 in admin_queryset
        ), "Superuser should see all users in queryset"

        assert user_admin.has_change_permission(
            request_admin, self.moderator
        ), "Superuser should be able to change any user"
        assert user_admin.has_delete_permission(
            request_admin, self.moderator
        ), "Superuser should be able to delete any user"

    def test_complete_edit_publish_workflow(self):
        """
        E2E Test: Complete workflow with in-place editing and state transitions.

        Workflow:
        1. Contributor creates draft
        2. Contributor submits for review
        3. Moderator publishes
        4. Edit the published case in-place (move back to draft, re-publish)
        5. Verify versionInfo is updated on publish

        Validates: Requirements 1.1, 1.3, 2.1, 2.2, 7.2
        """
        # Step 1: Contributor creates draft
        case = create_case_with_entities(
            title="Edit Publish Case",
            alleged_entities=["https://jawafdehi.org/entity/person/test"],
            key_allegations=["Initial allegation"],
            case_type=CaseType.CORRUPTION,
            description="Initial version description",
            state=CaseState.DRAFT,
        )

        assert case.state == CaseState.DRAFT
        credit_author(case)

        case_slug = case.slug

        # Step 2: Contributor submits for review
        case.submit()
        case.refresh_from_db()

        assert case.state == CaseState.IN_REVIEW
        assert case.versionInfo.get("action") == "submitted"

        # Step 3: Moderator publishes
        case.publish()
        case.refresh_from_db()

        assert case.state == CaseState.PUBLISHED
        assert case.versionInfo.get("action") == "published"

        # Step 4: Edit the published case in-place
        case.title = "Edit Publish Case - Updated"
        case.key_allegations = ["Initial allegation", "Updated allegation"]
        case.description = "Updated version with new information"
        case.state = CaseState.DRAFT
        case.save()

        # Re-submit and re-publish
        case.submit()
        case.publish()
        case.refresh_from_db()

        assert case.state == CaseState.PUBLISHED
        assert case.versionInfo.get("action") == "published"

        # Step 5: Verify only one DB row per case_id
        assert Case.objects.filter(slug=case_slug).count() == 1

        # Verify versionInfo is complete
        assert case.versionInfo is not None
        assert "datetime" in case.versionInfo

    def test_caseworker_can_transition_to_any_state(self):
        """
        E2E Test: v3 authz — the single content-staff role (Caseworker) can
        transition a case to ANY state, including PUBLISHED and CLOSED. The old
        Caseworker-confined-to-{DRAFT, IN_REVIEW} boundary is retired.

        Workflow:
        1. Caseworker creates a draft
        2. Caseworker transitions DRAFT → IN_REVIEW (allowed)
        3. Caseworker transitions IN_REVIEW → PUBLISHED (allowed in v3)
        4. Caseworker transitions PUBLISHED → CLOSED (allowed in v3)

        Validates: Requirements 1.5
        """
        # Step 1: Caseworker creates a draft
        case = create_case_with_entities(
            title="State Transition Test",
            alleged_entities=["https://jawafdehi.org/entity/person/test"],
            key_allegations=["Test allegation"],
            case_type=CaseType.CORRUPTION,
            description="Test description",
            state=CaseState.DRAFT,
        )

        from django.test import RequestFactory

        from cases.admin import CaseAdminForm

        factory = RequestFactory()
        request_contrib = factory.post("/")
        request_contrib.user = self.contributor1

        # Step 2: Caseworker transitions DRAFT → IN_REVIEW (allowed)
        form_data = {
            "slug": case.slug,
            "title": case.title,
            "case_type": case.case_type,
            "state": CaseState.IN_REVIEW,
            "key_allegations": case.key_allegations,
            "description": case.description,
        }
        form = CaseAdminForm(data=form_data, instance=case, request=request_contrib)
        assert (
            form.is_valid()
        ), f"Form should be valid for DRAFT → IN_REVIEW: {form.errors}"
        form.save()

        case.refresh_from_db()
        assert (
            case.state == CaseState.IN_REVIEW
        ), "Caseworker should be able to transition DRAFT → IN_REVIEW"

        # Step 3: Caseworker transitions IN_REVIEW → PUBLISHED (allowed in v3)
        form_data["state"] = CaseState.PUBLISHED
        form = CaseAdminForm(data=form_data, instance=case, request=request_contrib)
        assert (
            form.is_valid()
        ), f"Form should be valid for IN_REVIEW → PUBLISHED: {form.errors}"
        form.save()

        case.refresh_from_db()
        assert (
            case.state == CaseState.PUBLISHED
        ), "Caseworker should be able to transition to PUBLISHED (Requirement 1.5)"

        # Step 4: Caseworker transitions PUBLISHED → CLOSED (allowed in v3)
        form_data["state"] = CaseState.CLOSED
        form = CaseAdminForm(data=form_data, instance=case, request=request_contrib)
        assert (
            form.is_valid()
        ), f"Form should be valid for PUBLISHED → CLOSED: {form.errors}"
        form.save()

        case.refresh_from_db()
        assert (
            case.state == CaseState.CLOSED
        ), "Caseworker should be able to transition to CLOSED"

    def test_contributor_login_create_minimal_case_and_view_workflow(self):
        """
        E2E Test: Django admin is read-only; validates that a contributor can
        log in to Django admin and VIEW their assigned cases, but cannot add
        new cases through this interface. Case creation happens via the SPA.

        Validates: Requirements 1.1, 3.1, 3.2
        """
        # Step 1: Contributor logs into Django Admin
        login_success = self.client.login(
            username="contributor1", password="testpass123"
        )
        assert login_success, "Contributor should be able to log in"

        # Verify contributor can access admin
        response = self.client.get("/django-admin/")
        assert (
            response.status_code == 200
        ), "Contributor should be able to access admin interface"

        # Step 2: Verify has_add_permission is False (cannot create via Django admin)
        admin_instance = CaseAdmin(Case, None)
        from django.test import RequestFactory

        factory = RequestFactory()

        request_contrib = factory.get("/django-admin/cases/case/")
        request_contrib.user = self.contributor1

        assert not admin_instance.has_add_permission(
            request_contrib
        ), "Django admin is read-only; contributor cannot add cases"

        # Step 3: Create a case (as if via SPA) and assign contributor
        case = create_case_with_entities(
            title="Minimal Case - Quick Start",
            case_type=CaseType.CORRUPTION,
            alleged_entities=["https://jawafdehi.org/entity/person/placeholder"],
            state=CaseState.DRAFT,
        )

        # Step 4: Contributor can VIEW the case in their queryset
        queryset = admin_instance.get_queryset(request_contrib)
        assert (
            case in queryset
        ), "Contributor should see their case in list (Requirement 3.1)"

        # Verify view permission
        has_view_permission = admin_instance.has_view_permission(request_contrib, case)
        assert (
            has_view_permission
        ), "Contributor should have view permission for their own case"

        # Django admin is read-only: NO change permission
        has_change_permission = admin_instance.has_change_permission(
            request_contrib, case
        )
        assert (
            not has_change_permission
        ), "Django admin is read-only; contributor cannot change cases"

        # Step 5: Other contributor can view but not change this case
        request_other = factory.get("/django-admin/cases/case/")
        request_other.user = self.contributor2

        queryset_other = admin_instance.get_queryset(request_other)
        assert (
            case in queryset_other
        ), "Other contributors should see unassigned cases (global read access)"

        has_view_permission_other = admin_instance.has_view_permission(
            request_other, case
        )
        assert (
            has_view_permission_other
        ), "Other contributors should have view permission for unassigned cases"

        has_change_permission_other = admin_instance.has_change_permission(
            request_other, case
        )
        assert (
            not has_change_permission_other
        ), "Django admin is read-only; other contributor cannot change cases"

    def test_new_case_must_be_draft_state(self):
        """
        E2E Test: Verify that new cases can only be created in DRAFT state.

        Workflow:
        1. Attempt to create a new case with state=PUBLISHED (should fail)
        2. Attempt to create a new case with state=IN_REVIEW (should fail)
        3. Attempt to create a new case with state=CLOSED (should fail)
        4. Create a new case with state=DRAFT (should succeed)
        5. Verify case is created successfully in DRAFT state

        Validates: Requirements 1.1
        """
        from django.test import RequestFactory

        from cases.admin import CaseAdminForm

        factory = RequestFactory()
        request_contrib = factory.post("/django-admin/cases/case/add/")
        request_contrib.user = self.contributor1

        entities = create_entities_from_ids(
            ["https://jawafdehi.org/entity/person/test"]
        )

        # Step 1: Attempt to create a new case with state=PUBLISHED (should fail)
        form_data = {
            "title": "New Case - Published State",
            "case_type": CaseType.CORRUPTION,
            "state": CaseState.PUBLISHED,
            "alleged_entities": list(entities),
            "key_allegations": ["Test allegation"],
            "description": "Test description",
        }
        form = CaseAdminForm(data=form_data, request=request_contrib)
        assert (
            not form.is_valid()
        ), "Form should not be valid for PUBLISHED state on new case"
        assert "state" in form.errors, "Should have state error"
        assert "New cases must be created in DRAFT state" in str(
            form.errors["state"]
        ), "Should not allow creating new case with PUBLISHED state (Requirement 1.1)"

        # Step 2: Attempt to create a new case with state=IN_REVIEW (should fail)
        form_data["state"] = CaseState.IN_REVIEW
        form_data["title"] = "New Case - In Review State"
        form = CaseAdminForm(data=form_data, request=request_contrib)
        assert (
            not form.is_valid()
        ), "Form should not be valid for IN_REVIEW state on new case"
        assert "state" in form.errors, "Should have state error"
        assert "New cases must be created in DRAFT state" in str(
            form.errors["state"]
        ), "Should not allow creating new case with IN_REVIEW state"

        # Step 3: Attempt to create a new case with state=CLOSED (should fail)
        form_data["state"] = CaseState.CLOSED
        form_data["title"] = "New Case - Closed State"
        form = CaseAdminForm(data=form_data, request=request_contrib)
        assert (
            not form.is_valid()
        ), "Form should not be valid for CLOSED state on new case"
        assert "state" in form.errors, "Should have state error"
        assert "New cases must be created in DRAFT state" in str(
            form.errors["state"]
        ), "Should not allow creating new case with CLOSED state"

        # Step 4: Create a new case with state=DRAFT (should succeed)
        case_draft = Case(
            title="New Case - Draft State",
            case_type=CaseType.CORRUPTION,
            state=CaseState.DRAFT,
        )
        case_draft.save()
        for nes_id in entities:
            CaseEntityRelationship.objects.create(
                case=case_draft,
                nes_id=nes_id,
                relationship_type=RelationshipType.ALLEGED,
            )

        # Step 5: Verify case is created successfully in DRAFT state
        assert case_draft.id is not None, "Case should be saved to database"
        assert (
            case_draft.state == CaseState.DRAFT
        ), "New case should be in DRAFT state (Requirement 1.1)"

    def test_admin_entity_id_validation_on_create(self):
        """
        E2E Test: Verify entity ID validation in admin panel when creating a case.

        Tests that the admin form properly validates entity IDs using the
        MultiEntityIDField widget which validates the canonical entity @id IRI.

        Workflow:
        1. Test invalid entity ID format (not an @id IRI)
        2. Test invalid entity type (unsupported type)
        3. Test invalid slug format
        4. Test empty entity ID
        5. Test valid entity IDs (person, organization, location)
        6. Test mixed valid and invalid entity IDs

        Validates: Entity ID validation in admin panel
        """
        from cases.widgets import MultiEntityIDField

        # Test the field directly to isolate entity ID validation
        field = MultiEntityIDField(required=True)

        # Step 1: Test invalid entity ID format (not an @id IRI)
        with pytest.raises(ValidationError) as exc_info:
            field.clean('["invalid-format"]')

        error_message = str(exc_info.value)
        assert (
            "Invalid entity @id IRI" in error_message
        ), f"Error should mention invalid IRI. Got: {error_message}"

        # Step 2: Test legacy entity: form is no longer accepted
        with pytest.raises(ValidationError) as exc_info:
            field.clean('["entity:person/test-slug"]')

        error_message = str(exc_info.value)
        assert (
            "Invalid entity @id IRI" in error_message
        ), f"Error should reject legacy entity: form. Got: {error_message}"

        # Step 3: Test invalid slug format (contains invalid characters)
        with pytest.raises(ValidationError) as exc_info:
            field.clean(
                '["https://jawafdehi.org/entity/person/Invalid Slug With Spaces"]'
            )

        error_message = str(exc_info.value)
        assert (
            "Invalid entity @id IRI" in error_message
        ), f"Error should mention invalid IRI. Got: {error_message}"

        # Step 4: Test empty entity ID
        with pytest.raises(ValidationError) as exc_info:
            field.clean('[""]')

        error_message = str(exc_info.value)
        assert (
            "Invalid entity @id IRI" in error_message
            or "empty" in error_message.lower()
        ), f"Error should mention empty entity ID. Got: {error_message}"

        # Step 5: Test valid entity @id IRIs
        # Valid person entity
        result = field.clean('["https://jawafdehi.org/entity/person/john-doe"]')
        assert result == [
            "https://jawafdehi.org/entity/person/john-doe"
        ], "Should accept valid person entity IRI"

        # Valid organization entity
        result = field.clean('["https://jawafdehi.org/entity/organization/test-org"]')
        assert result == [
            "https://jawafdehi.org/entity/organization/test-org"
        ], "Should accept valid organization entity IRI"

        # Valid location entity
        result = field.clean('["https://jawafdehi.org/entity/location/kathmandu"]')
        assert result == [
            "https://jawafdehi.org/entity/location/kathmandu"
        ], "Should accept valid location entity IRI"

        # Multiple valid entity IRIs
        result = field.clean(
            '["https://jawafdehi.org/entity/person/jane-doe", '
            '"https://jawafdehi.org/entity/organization/ministry", '
            '"https://jawafdehi.org/entity/location/district"]'
        )
        assert len(result) == 3, "Should accept multiple valid entity IRIs"
        assert "https://jawafdehi.org/entity/person/jane-doe" in result
        assert "https://jawafdehi.org/entity/organization/ministry" in result
        assert "https://jawafdehi.org/entity/location/district" in result

        # Step 6: Test mixed valid and invalid entity IDs
        with pytest.raises(ValidationError) as exc_info:
            field.clean(
                '["https://jawafdehi.org/entity/person/valid-person", '
                '"invalid-format"]'
            )

        error_message = str(exc_info.value)
        assert (
            "Invalid entity @id IRI" in error_message
        ), "Should reject when mixing valid and invalid entity IDs"

    def test_admin_entity_id_validation_on_update(self):
        """
        E2E Test: Verify entity ID validation when updating an existing case.

        Workflow:
        1. Create a case with valid entity IDs
        2. Attempt to update with invalid entity ID via model
        3. Verify validation error is raised
        4. Update with valid entity IDs
        5. Verify update succeeds

        Validates: Entity ID validation on case updates
        """
        # Step 1: Create a case with valid entity IDs
        case = create_case_with_entities(
            title="Original Case",
            case_type=CaseType.CORRUPTION,
            alleged_entities=["https://jawafdehi.org/entity/person/original-person"],
            state=CaseState.DRAFT,
        )

        original_id = case.id

        # Step 2: Attempt to bind an invalid entity ID. Validation now happens at
        # the CaseEntityRelationship bind level (the nes_id is the join key).
        with pytest.raises(ValidationError) as exc_info:
            CaseEntityRelationship(
                case=case,
                nes_id="not-valid-format",
                relationship_type=RelationshipType.ALLEGED,
            ).save()

        error_dict = exc_info.value.message_dict
        assert (
            "nes_id" in error_dict
        ), f"Error should be associated with nes_id field. Got: {error_dict}"

        # Step 4: Update with valid entity IDs
        new_entities = create_entities_from_ids(
            [
                "https://jawafdehi.org/entity/person/updated-person",
                "https://jawafdehi.org/entity/organization/new-org",
            ]
        )
        case.entity_relationships.filter(
            relationship_type=RelationshipType.ALLEGED
        ).delete()
        for nes_id in new_entities:
            CaseEntityRelationship.objects.create(
                case=case,
                nes_id=nes_id,
                relationship_type=RelationshipType.ALLEGED,
            )
        case.full_clean()  # Should not raise
        case.save()

        # Step 5: Verify update succeeds
        case.refresh_from_db()

        assert case.id == original_id, "Should be the same case instance"
        assert (
            case.entity_relationships.filter(
                relationship_type=RelationshipType.ALLEGED
            ).count()
            == 2
        ), "Case should have 2 alleged relationships after update"
        entity_nes_ids = list(
            case.entity_relationships.filter(
                relationship_type=RelationshipType.ALLEGED
            ).values_list("nes_id", flat=True)
        )
        assert (
            "https://jawafdehi.org/entity/person/updated-person" in entity_nes_ids
        ), "Updated entity ID should be saved"
        assert (
            "https://jawafdehi.org/entity/organization/new-org" in entity_nes_ids
        ), "New entity ID should be saved"

    def test_admin_related_entities_validation(self):
        """
        E2E Test: Verify entity ID validation for related_entities and locations fields.

        Workflow:
        1. Test invalid entity IDs in related_entities field
        2. Test invalid entity IDs in locations field
        3. Test valid entity IDs in all entity fields
        4. Verify all fields are properly validated

        Validates: Entity ID validation across all entity fields
        """
        from cases.widgets import MultiEntityIDField

        field = MultiEntityIDField(required=False)

        # Step 1: Test invalid entity IDs in related_entities field
        with pytest.raises(ValidationError) as exc_info:
            field.clean('["invalid-related"]')

        error_message = str(exc_info.value)
        assert (
            "Invalid entity @id IRI" in error_message
        ), f"Error should mention invalid IRI. Got: {error_message}"

        # Step 2: Test invalid entity IDs in locations field
        with pytest.raises(ValidationError) as exc_info:
            field.clean('["invalid-location"]')

        error_message = str(exc_info.value)
        assert (
            "Invalid entity @id IRI" in error_message
        ), f"Error should mention invalid IRI. Got: {error_message}"

        # Step 3: Test valid entity @id IRIs in all entity fields
        result = field.clean(
            '["https://jawafdehi.org/entity/person/witness", '
            '"https://jawafdehi.org/entity/organization/related-org"]'
        )
        assert len(result) == 2, "Should accept multiple valid entity IRIs"

        result = field.clean(
            '["https://jawafdehi.org/entity/location/kathmandu", '
            '"https://jawafdehi.org/entity/location/pokhara"]'
        )
        assert len(result) == 2, "Should accept multiple valid location entity IRIs"

        # Step 4: Test empty list is valid for optional fields
        result = field.clean("[]")
        assert result == [], "Empty list should be valid for optional entity fields"

    def test_alleged_entities_optional_for_draft_required_for_review(self):
        """
        E2E Test: Verify alleged_entities is optional for DRAFT but required for IN_REVIEW.

        Workflow:
        1. Create a draft case without alleged_entities (should succeed)
        2. Attempt to submit for review without alleged_entities (should fail)
        3. Add alleged_entities and submit (should succeed)
        4. Verify case transitions to IN_REVIEW

        Validates: alleged_entities validation based on state
        """
        # Step 1: Create a draft case without alleged_entities
        case = create_case_with_entities(
            title="Draft Without Entities",
            case_type=CaseType.CORRUPTION,
            alleged_entities=[],  # Empty list
            state=CaseState.DRAFT,
        )

        # Verify case was created successfully
        assert (
            case.id is not None
        ), "Draft case should be created without alleged_entities"
        assert case.state == CaseState.DRAFT
        assert case.entity_relationships.count() == 0

        # Step 2: Attempt to submit for review without alleged_entities
        case.state = CaseState.IN_REVIEW

        with pytest.raises(ValidationError) as exc_info:
            case.validate()

        error_dict = exc_info.value.message_dict
        assert (
            "entities" in error_dict
        ), f"Should require alleged entities for IN_REVIEW. Got errors: {error_dict}"
        error_message = str(error_dict["entities"])
        assert (
            "IN_REVIEW or PUBLISHED" in error_message
        ), f"Error message should mention IN_REVIEW/PUBLISHED requirement. Got: {error_message}"

        # Reset state
        case.state = CaseState.DRAFT
        case.save()

        # Step 3: Add alleged_entities and required fields for submission
        alleged_entity = create_entities_from_ids(
            ["https://jawafdehi.org/entity/person/corrupt-official"]
        )[0]
        case.entity_relationships.create(
            nes_id=alleged_entity,
            relationship_type=RelationshipType.ACCUSED,
        )
        case.key_allegations = ["Test allegation"]
        case.description = "Test description"
        case.save()
        credit_author(case)

        # Now submit should work
        case.submit()

        # Step 4: Verify case transitions to IN_REVIEW
        case.refresh_from_db()
        assert (
            case.state == CaseState.IN_REVIEW
        ), "Case should transition to IN_REVIEW with alleged_entities"
        assert (
            case.entity_relationships.filter(
                relationship_type=RelationshipType.ACCUSED
            ).count()
            == 1
        )
        assert case.versionInfo.get("action") == "submitted"

    def test_alleged_entities_required_for_published(self):
        """
        E2E Test: Verify alleged_entities is required for PUBLISHED state.

        Workflow:
        1. Create a draft case with alleged_entities
        2. Remove alleged_entities and attempt to publish (should fail)
        3. Add alleged_entities back and publish (should succeed)

        Validates: alleged_entities validation for PUBLISHED state
        """
        # Step 1: Create a draft case with alleged_entities
        case = create_case_with_entities(
            title="Case for Publishing",
            case_type=CaseType.CORRUPTION,
            alleged_entities=["https://jawafdehi.org/entity/person/test-official"],
            key_allegations=["Test allegation"],
            description="Test description",
            state=CaseState.DRAFT,
        )

        # Step 2: Remove alleged_entities and attempt to publish
        case.entity_relationships.filter(
            relationship_type=RelationshipType.ACCUSED
        ).delete()
        case.state = CaseState.PUBLISHED

        with pytest.raises(ValidationError) as exc_info:
            case.validate()

        error_dict = exc_info.value.message_dict
        assert (
            "entities" in error_dict
        ), f"Should require alleged entities for PUBLISHED. Got errors: {error_dict}"

        # Reset state
        case.state = CaseState.DRAFT
        case.save()

        # Step 3: Add alleged_entities back and publish
        alleged_entity = create_entities_from_ids(
            ["https://jawafdehi.org/entity/person/test-official"]
        )[0]
        case.entity_relationships.create(
            nes_id=alleged_entity,
            relationship_type=RelationshipType.ACCUSED,
        )
        case.save()
        credit_author(case)

        case.publish()

        case.refresh_from_db()
        assert (
            case.state == CaseState.PUBLISHED
        ), "Case should be published with alleged_entities"
        assert (
            case.entity_relationships.filter(
                relationship_type=RelationshipType.ACCUSED
            ).count()
            == 1
        )
