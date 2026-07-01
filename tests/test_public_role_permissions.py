"""
Tests for the role-model v2 ``Public`` role and its contrast with ``ReadOnly``.

The locked role model defines:
  - readonly = system-wide read INCLUDING casework (can view draft/in-review).
  - public   = read EXCLUDING casework (public surface only; cannot view
               draft/in-review casework or the review queue).

These tests pin the casework VIEW boundary: a ReadOnly user can view an
unassigned DRAFT case, a Public user cannot; and the casework review read gate
(CanReadReview) admits ReadOnly but denies Public. Neither role has any write
path (mirrors tests/test_readonly_permissions.py for the no-write side).
"""

import pytest
from django.contrib.auth import get_user_model
from django.test import RequestFactory

from cases.models import CaseState, CaseType
from cases.rules.predicates import (
    can_change_case,
    can_view_case,
    has_role,
    is_caseworker,
    is_public,
    is_readonly,
)
from review.permissions import CanReadReview, HasContributorRole
from tests.conftest import create_case_with_entities, create_user_with_role

User = get_user_model()


class _MockView:
    def __init__(self, action=None):
        self.action = action
        self.detail = action in ("retrieve", "partial_update", "destroy")


def _build_request(method="GET", user=None):
    factory = RequestFactory()
    req = factory.generic(method, "/")
    req.user = user
    req.method = method
    return req


def _make_public(username="pub", email="pub@example.com"):
    return create_user_with_role(username, email, "Public")


def _make_readonly(username="ro", email="ro@example.com"):
    return create_user_with_role(username, email, "ReadOnly")


def _make_draft(title="Unassigned Draft"):
    case = create_case_with_entities(
        title=title,
        alleged_entities=["https://jawafdehi.org/entity/person/test-person"],
        key_allegations=["Test"],
        case_type=CaseType.CORRUPTION,
        description=title,
    )
    case.state = CaseState.DRAFT
    case.save()
    return case


# ---------------------------------------------------------------------------
# Predicate identity
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_is_public_predicate():
    """is_public is True only for Public group members."""
    public = _make_public()
    assert is_public(public)
    assert not is_readonly(public)
    assert not is_caseworker(public)

    readonly = _make_readonly()
    assert not is_public(readonly)


@pytest.mark.django_db
def test_public_is_not_a_content_role():
    """Public must not satisfy has_role (the content-write gate)."""
    public = _make_public()
    assert not has_role(public)


# ---------------------------------------------------------------------------
# Casework VIEW boundary: readonly sees draft/in-review, public does not
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_readonly_can_view_draft_but_public_cannot():
    case = _make_draft()
    assert can_view_case(_make_readonly(), case) is True
    assert can_view_case(_make_public(), case) is False


# ---------------------------------------------------------------------------
# Casework review read gate: readonly admitted, public denied
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_canreadreview_admits_readonly_denies_public():
    perm = CanReadReview()
    assert perm.has_permission(_build_request("GET", _make_readonly()), _MockView())
    assert not perm.has_permission(_build_request("GET", _make_public()), _MockView())


# ---------------------------------------------------------------------------
# No write path for either read role
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_public_cannot_change_case():
    public = _make_public()
    assert not can_change_case(public, _make_draft())


@pytest.mark.django_db
def test_public_denied_casework_mutations():
    """HasContributorRole (caseworker write gate) excludes Public."""
    public = _make_public()
    assert not HasContributorRole().has_permission(
        _build_request("POST", public), _MockView()
    )
