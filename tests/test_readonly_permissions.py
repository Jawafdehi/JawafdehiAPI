"""
Tests for the org-wide ReadOnly role.

Verifies that a ReadOnly user can read everything (all non-CLOSED cases, the
casework review system) but has no write path anywhere: not casework mutations,
not the review config, and not case PATCH (even for unassigned cases).
"""

import pytest
from django.contrib.auth import get_user_model
from django.test import RequestFactory

from cases.models import CaseState, CaseType
from cases.rules.predicates import (
    can_change_case,
    can_view_case,
    has_role,
    is_contributor,
    is_readonly,
)
from review.permissions import CanReadReview, HasContributorRole, IsAdminOrModerator
from tests.conftest import create_case_with_entities, create_user_with_role

User = get_user_model()


# ============================================================================
# Helpers (mirror tests/test_contributor_read_permissions.py)
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


def _make_readonly(username="readonly", email="readonly@example.com"):
    return create_user_with_role(username, email, "ReadOnly")


def _make_case(title, state=CaseState.DRAFT):
    case = create_case_with_entities(
        title=title,
        alleged_entities=["entity:person/test-person"],
        key_allegations=["Test"],
        case_type=CaseType.CORRUPTION,
        description=title,
    )
    case.state = state
    case.save()
    return case


# ============================================================================
# Predicates
# ============================================================================


@pytest.mark.django_db
def test_is_readonly_predicate():
    """is_readonly is True only for ReadOnly group members."""
    readonly = _make_readonly()
    assert is_readonly(readonly)

    contrib = create_user_with_role(
        "contrib_ro", "contrib_ro@example.com", "Contributor"
    )
    assert not is_readonly(contrib)

    plain = User.objects.create_user(username="plain_ro", email="plain_ro@example.com")
    assert not is_readonly(plain)


@pytest.mark.django_db
def test_readonly_is_not_a_content_role():
    """ReadOnly must not satisfy has_role (which gates content writes)."""
    readonly = _make_readonly()
    assert not has_role(readonly)
    assert not is_contributor(readonly)


@pytest.mark.django_db
def test_readonly_can_view_unassigned_draft_case():
    """can_view_case admits ReadOnly so retrieve() exposes DRAFT details."""
    readonly = _make_readonly()
    case = _make_case("Unassigned Draft", state=CaseState.DRAFT)
    assert can_view_case(readonly, case)


@pytest.mark.django_db
def test_readonly_cannot_change_unassigned_case():
    """Critical no-write assertion: ReadOnly cannot PATCH a case."""
    readonly = _make_readonly()
    case = _make_case("Unassigned", state=CaseState.DRAFT)
    assert not can_change_case(readonly, case)


# ============================================================================
# Casework read permission class — CanReadReview
# ============================================================================


@pytest.mark.django_db
class TestCanReadReview:
    def test_readonly_allowed(self):
        user = _make_readonly()
        assert CanReadReview().has_permission(_build_request("GET", user), _MockView())

    def test_contributor_allowed(self):
        user = create_user_with_role("c_rr", "c_rr@example.com", "Contributor")
        assert CanReadReview().has_permission(_build_request("GET", user), _MockView())

    def test_moderator_allowed(self):
        user = create_user_with_role("m_rr", "m_rr@example.com", "Moderator")
        assert CanReadReview().has_permission(_build_request("GET", user), _MockView())

    def test_admin_allowed(self):
        user = create_user_with_role("a_rr", "a_rr@example.com", "Admin")
        assert CanReadReview().has_permission(_build_request("GET", user), _MockView())

    def test_review_assistant_allowed(self):
        user = create_user_with_role("ra_rr", "ra_rr@example.com", "ReviewAssistant")
        assert CanReadReview().has_permission(_build_request("GET", user), _MockView())

    def test_unauthenticated_denied(self):
        req = _build_request("GET")
        req.user = None
        assert not CanReadReview().has_permission(req, _MockView())

    def test_no_role_user_denied(self):
        plain = User.objects.create_user(
            username="norole_rr", email="norole_rr@example.com"
        )
        assert not CanReadReview().has_permission(
            _build_request("GET", plain), _MockView()
        )


# ============================================================================
# Casework write/admin gates — ReadOnly must be denied
# ============================================================================


@pytest.mark.django_db
def test_readonly_denied_casework_mutations():
    """HasContributorRole (guards submit/claim/stage/result/regrade) excludes ReadOnly."""
    readonly = _make_readonly()
    perm = HasContributorRole()
    assert not perm.has_permission(_build_request("POST", readonly), _MockView())


@pytest.mark.django_db
def test_readonly_denied_config_put():
    """The inline config PUT guard (IsAdminOrModerator) rejects ReadOnly."""
    readonly = _make_readonly()
    assert not IsAdminOrModerator().has_permission(
        _build_request("PUT", readonly), None
    )


# ============================================================================
# Case list visibility
# ============================================================================


@pytest.mark.django_db
def test_readonly_list_queryset_includes_draft_excludes_closed():
    """Exercise the role-gated CaseViewSet.get_queryset() (not a bare ORM filter).

    Drives the real list branch with a ReadOnly request via the DRF viewset and
    asserts DRAFT is included while CLOSED is excluded. End-to-end endpoint
    coverage lives in tests/api/test_readonly_read_visibility.py.
    """
    from rest_framework.request import Request
    from rest_framework.test import APIRequestFactory

    from cases.api_views import CaseViewSet

    readonly = _make_readonly()
    draft = _make_case("RO Visible Draft", state=CaseState.DRAFT)
    published = _make_case("RO Published", state=CaseState.PUBLISHED)
    closed = _make_case("RO Closed", state=CaseState.CLOSED)

    # Wrap in a DRF Request so get_queryset()'s query_params access works.
    request = Request(APIRequestFactory().get("/api/cases/"))
    request.user = readonly
    view = CaseViewSet()
    view.action = "list"
    view.request = request

    case_ids = set(view.get_queryset().values_list("id", flat=True))
    assert draft.id in case_ids
    assert published.id in case_ids
    assert closed.id not in case_ids
