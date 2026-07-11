"""Tests for GET/PUT /api/casework/config/ — the global review-config gate.

The review config holds GLOBAL scoring thresholds (pass/revise) + LLM sampling
that affect every review's disposition. The endpoint deliberately splits its
permissions: any casework READ role may GET the config, but only content staff
(superuser or Caseworker) may PUT it. That write gate is enforced by an INLINE
``IsAdminOrModerator().has_permission(...)`` check inside the view (not the
``permission_classes``); in the v3 model the Moderator role is folded into
Caseworker, so a Caseworker legitimately CAN rewrite the thresholds while the
org-wide ReadOnly role cannot. These tests pin the matrix so a regression can't
slip through.
"""

import pytest
from rest_framework.test import APIClient

from tests.conftest import create_user_with_role

URL = "/api/casework/config/"


def _client(role):
    user = create_user_with_role(
        f"cfg-{role.lower()}", f"cfg-{role.lower()}@example.com", role
    )
    client = APIClient()
    client.force_authenticate(user=user)
    return client


@pytest.mark.django_db
def test_config_get_requires_authentication():
    assert APIClient().get(URL).status_code == 401


@pytest.mark.django_db
def test_config_put_requires_authentication():
    assert (
        APIClient().put(URL, {"pass_threshold": 90}, format="json").status_code == 401
    )


@pytest.mark.django_db
@pytest.mark.parametrize("role", ["Caseworker", "Moderator", "Admin"])
def test_read_roles_can_get_config(role):
    resp = _client(role).get(URL)
    assert resp.status_code == 200
    # The config exposes the global thresholds.
    assert "pass_threshold" in resp.data


@pytest.mark.django_db
def test_unauthenticated_cannot_get_config():
    """An anonymous (no-token) caller cannot read the config."""
    assert APIClient().get(URL).status_code == 401


@pytest.mark.django_db
def test_caseworker_can_put_config():
    """v3: the Caseworker role carries the old Moderator powers, so a Caseworker
    MAY rewrite the global thresholds (PUT 200)."""
    resp = _client("Caseworker").put(URL, {"pass_threshold": 99}, format="json")
    assert resp.status_code == 200
    assert resp.data["pass_threshold"] == 99


@pytest.mark.django_db
def test_readonly_cannot_put_config():
    resp = _client("ReadOnly").put(URL, {"pass_threshold": 99}, format="json")
    # ReadOnly lacks even the read role for casework config → 403.
    assert resp.status_code in (403,)


@pytest.mark.django_db
@pytest.mark.parametrize("role", ["Moderator", "Admin"])
def test_admin_and_moderator_can_put_config(role):
    resp = _client(role).put(URL, {"pass_threshold": 88}, format="json")
    assert resp.status_code == 200
    assert resp.data["pass_threshold"] == 88
