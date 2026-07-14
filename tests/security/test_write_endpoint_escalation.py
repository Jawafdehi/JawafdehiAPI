"""Adversarial: vertical privilege escalation across the write plane.

Threat: an anonymous caller or a low-privilege principal (public / readonly) tries
to reach a mutating endpoint they must not. The contract across every write plane
is the SAME: no credentials → 401 (the OIDC authenticator sets WWW-Authenticate);
authenticated but without the required role → 403. This asserts that contract
uniformly so a regression on any one plane is caught.

The auth/role gate fires BEFORE body validation, so these use minimal/empty
payloads — an unauthorized caller never reaches the serializer.

Run with: ``uv run pytest -m security``.
"""

from __future__ import annotations

import pytest
from rest_framework.test import APIClient

from tests.conftest import create_user_with_role

pytestmark = [pytest.mark.security, pytest.mark.django_db]


# (method, url, json-body) for each role-gated write endpoint.
#
# NOTE: ``/api/query/`` is deliberately NOT here — it is a SELECT-only READ
# surface, so the org-wide ReadOnly role is admitted to it (see
# ``test_query_plane_is_read_not_write`` below), unlike these write planes.
WRITE_ENDPOINTS = [
    ("post", "/api/entities", {"@type": "Person", "@id": "x"}),
    ("post", "/api/courtcases/", {}),
    ("post", "/api/ingestion/cases/", {}),
    ("post", "/api/admin/reindex", {}),
]


def _call(client, method, url, body):
    return getattr(client, method)(url, body, format="json")


@pytest.mark.parametrize("method,url,body", WRITE_ENDPOINTS)
def test_anonymous_write_is_401(method, url, body):
    resp = _call(APIClient(), method, url, body)
    assert resp.status_code == 401, f"{url} gave {resp.status_code}, expected 401"


@pytest.mark.parametrize("method,url,body", WRITE_ENDPOINTS)
@pytest.mark.parametrize("role", ["Public", "ReadOnly"])
def test_low_privilege_write_is_403(method, url, body, role):
    user = create_user_with_role(f"esc-{role}", f"esc-{role}@example.com", role)
    client = APIClient()
    client.force_authenticate(user=user)
    resp = _call(client, method, url, body)
    # A low-privilege authenticated caller is forbidden (403) — NOT 401 (they ARE
    # authenticated) and NOT 2xx (they lack the write/admin role).
    assert resp.status_code == 403, (
        f"{role} on {url} gave {resp.status_code}, expected 403 "
        f"(escalation guard). Body: {getattr(resp, 'content', b'')[:200]}"
    )


def test_readonly_can_still_read_but_not_write_cases():
    """The readonly role is org-wide READ: it may GET the case list but must not
    create — the classic read-vs-write escalation line."""
    user = create_user_with_role("esc-ro-cases", "esc-ro-cases@example.com", "ReadOnly")
    client = APIClient()
    client.force_authenticate(user=user)

    assert client.get("/api/cases/").status_code == 200
    # DjangoModelPermissions gates POST on cases.add_case, which ReadOnly lacks.
    resp = client.post("/api/cases/", {"case_type": "CORRUPTION", "title": "x"}, format="json")
    assert resp.status_code == 403


@pytest.mark.django_db(databases="__all__")
def test_query_plane_is_read_not_write():
    """``/api/query/`` is a SELECT-only READ surface, so its role contract differs
    from the write planes above: anon → 401, a lower-privilege authenticated role
    (Public) → 403, but the org-wide ReadOnly read role is ADMITTED (passes the
    auth gate). Writes still can't ride through it — the SELECT-only guard is
    covered by courts/tests/test_query_guard_security.py."""
    body = {"query": "SELECT 1"}

    # Anonymous: 401 (authenticator sets WWW-Authenticate).
    assert APIClient().post("/api/query/", body, format="json").status_code == 401

    # Public (authenticated, no query role): still forbidden.
    public = create_user_with_role("esc-pub-q", "esc-pub-q@example.com", "Public")
    pc = APIClient()
    pc.force_authenticate(user=public)
    assert pc.post("/api/query/", body, format="json").status_code == 403

    # ReadOnly: admitted — it must clear the auth gate (NOT 401/403); the response
    # is then a normal query outcome (200, or a guard/DB 400), never a rejection.
    readonly = create_user_with_role("esc-ro-q", "esc-ro-q@example.com", "ReadOnly")
    rc = APIClient()
    rc.force_authenticate(user=readonly)
    ro_status = rc.post("/api/query/", body, format="json").status_code
    assert ro_status not in (401, 403), (
        f"ReadOnly must clear the /api/query/ auth gate, got {ro_status}"
    )
