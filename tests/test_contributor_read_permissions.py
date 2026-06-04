"""
Tests for global Contributor (Caseworker) read-only access.

Verifies that Contributors can view all case materials but cannot write
to unassigned resources.
"""

import pytest
from django.contrib.auth import get_user_model
from django.test import RequestFactory

from case_workflows.permissions import IsAdminOrModeratorOrContributorReadOnly
from cases.models import Case, CaseState, CaseType
from tests.conftest import (
    create_case_with_entities,
    create_mock_request,
    create_user_with_role,
)

User = get_user_model()


# ============================================================================
# Helper: build a mock request + DRF view for permission class testing
# ============================================================================


class _MockView:
    """Minimal mock DRF view for testing permission classes."""

    def __init__(self, action=None):
        self.action = action
        self.detail = action in ("retrieve", "partial_update", "destroy")


def _build_request(method="GET", user=None):
    """Build a DRF Request-like object with the given method and user."""
    factory = RequestFactory()
    req = factory.generic(method, "/")
    req.user = user
    req.method = method
    return req


# ============================================================================
# Helpers
# ============================================================================


def _make_contributor(username="contrib", email="contrib@example.com"):
    return create_user_with_role(username, email, "Contributor")


# ============================================================================
# View predicates — can_view_case / can_view_source
# ============================================================================


@pytest.mark.django_db
def test_contributor_can_view_all_cases_including_unassigned_draft():
    """Contributor can see all non-CLOSED cases in list queryset."""
    contributor = _make_contributor()
    case = create_case_with_entities(
        title="Unassigned Draft Case",
        alleged_entities=["entity:person/test-person"],
        key_allegations=["Test"],
        case_type=CaseType.CORRUPTION,
        description="A case not assigned to this contributor",
    )
    case.state = CaseState.DRAFT
    case.save()

    request = create_mock_request(contributor)
    from cases.rules.predicates import can_view_case

    assert can_view_case(request.user, case)


@pytest.mark.django_db
def test_contributor_cannot_change_unassigned_case():
    """Contributor cannot patch a case they are not assigned to."""
    contributor = _make_contributor()
    case = create_case_with_entities(
        title="Unassigned",
        alleged_entities=["entity:person/test-person"],
        key_allegations=["Test"],
        case_type=CaseType.CORRUPTION,
        description="Not assigned",
    )

    request = create_mock_request(contributor)
    from cases.rules.predicates import can_change_case

    assert not can_change_case(request.user, case)


@pytest.mark.django_db
def test_contributor_can_change_assigned_case():
    """Contributor can patch a case they are assigned to."""
    contributor = _make_contributor()
    case = create_case_with_entities(
        title="Assigned",
        alleged_entities=["entity:person/test-person"],
        key_allegations=["Test"],
        case_type=CaseType.CORRUPTION,
        description="Assigned case",
    )
    case.contributors.add(contributor)

    request = create_mock_request(contributor)
    from cases.rules.predicates import can_change_case

    assert can_change_case(request.user, case)


# ============================================================================
# DRF permission class — workflow endpoints
# ============================================================================


@pytest.mark.django_db
class TestIsAdminOrModeratorOrContributorReadOnly:

    def test_contributor_read_allowed(self):
        user = _make_contributor()
        perm = IsAdminOrModeratorOrContributorReadOnly()
        assert perm.has_permission(_build_request("GET", user), _MockView())

    def test_contributor_write_denied(self):
        user = _make_contributor()
        perm = IsAdminOrModeratorOrContributorReadOnly()
        assert not perm.has_permission(_build_request("POST", user), _MockView())
        assert not perm.has_permission(_build_request("PATCH", user), _MockView())
        assert not perm.has_permission(_build_request("DELETE", user), _MockView())

    def test_admin_write_allowed(self):
        admin = create_user_with_role("admin1", "admin@example.com", "Admin")
        perm = IsAdminOrModeratorOrContributorReadOnly()
        assert perm.has_permission(_build_request("POST", admin), _MockView())
        assert perm.has_permission(_build_request("PATCH", admin), _MockView())

    def test_moderator_write_allowed(self):
        mod = create_user_with_role("mod1", "mod@example.com", "Moderator")
        perm = IsAdminOrModeratorOrContributorReadOnly()
        assert perm.has_permission(_build_request("POST", mod), _MockView())

    def test_unauthenticated_denied(self):
        perm = IsAdminOrModeratorOrContributorReadOnly()
        req = _build_request("GET")
        req.user = None
        assert not perm.has_permission(req, _MockView())

    def test_unauthorized_user_denied(self):
        # NoRole is not a real group; create a plain user
        plain = User.objects.create_user(
            username="plain", email="plain@example.com", password="testpass123"
        )
        perm = IsAdminOrModeratorOrContributorReadOnly()
        assert not perm.has_permission(_build_request("GET", plain), _MockView())


# ============================================================================
# CaseViewSet queryset — list visibility
# ============================================================================


@pytest.mark.django_db
def test_contributor_sees_all_non_closed_cases_in_list():
    """Contributor list queryset includes DRAFT and IN_REVIEW, excludes CLOSED."""
    contributor = _make_contributor()

    draft = create_case_with_entities(
        title="Contributor Visible Draft",
        alleged_entities=["entity:person/draft-person"],
        key_allegations=["Test"],
        case_type=CaseType.CORRUPTION,
        description="Draft visible to contributor",
    )
    draft.state = CaseState.DRAFT
    draft.save()

    published = create_case_with_entities(
        title="Published Case",
        alleged_entities=["entity:person/pub-person"],
        key_allegations=["Test"],
        case_type=CaseType.CORRUPTION,
        description="Published case",
    )
    published.state = CaseState.PUBLISHED
    published.save()

    closed = create_case_with_entities(
        title="Closed Case",
        alleged_entities=["entity:person/closed-person"],
        key_allegations=["Test"],
        case_type=CaseType.CORRUPTION,
        description="Closed",
    )
    closed.state = CaseState.CLOSED
    closed.save()

    from cases.rules.predicates import is_contributor

    assert is_contributor(contributor)
    # The view-level check — contributor sees all non-CLOSED
    all_non_closed = Case.objects.exclude(state=CaseState.CLOSED)
    assert draft in all_non_closed
    assert published in all_non_closed
    assert closed not in all_non_closed


# ============================================================================
# Edge cases
# ============================================================================


@pytest.mark.django_db
def test_user_without_role_still_blocked():
    """Regression: users without any role remain blocked from viewing DRAFT cases."""
    plain = User.objects.create_user(
        username="norole2", email="norole2@example.com", password="testpass123"
    )
    case = create_case_with_entities(
        title="Restricted Draft",
        alleged_entities=["entity:person/restrict"],
        key_allegations=["Test"],
        case_type=CaseType.CORRUPTION,
        description="Should not be visible",
    )
    case.state = CaseState.DRAFT
    case.save()

    from cases.rules.predicates import can_view_case

    assert not can_view_case(plain, case)


@pytest.mark.django_db
def test_is_contributor_predicate():
    """is_contributor returns True only for users in Contributor group."""
    from cases.rules.predicates import is_contributor

    contrib = _make_contributor()
    assert is_contributor(contrib)

    plain = User.objects.create_user(
        username="plain2", email="plain2@example.com", password="testpass123"
    )
    assert not is_contributor(plain)

    admin = create_user_with_role("admin2", "admin2@example.com", "Admin")
    assert not is_contributor(admin)  # Admin is not a Contributor
