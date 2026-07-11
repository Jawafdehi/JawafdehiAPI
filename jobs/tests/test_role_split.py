"""Role-split regression tests for the job-queue API (/api/jobs/*).

The queue draws a read-vs-drive line (jobs/permissions.py):

  * ``CanObserveJobs`` gates the read-only dashboard (GET /api/jobs). It admits
    superuser, the Caseworker content role (via ``has_role``), the JobPoller
    service account, and the org-wide ReadOnly role. No-role and anonymous
    callers are excluded.
  * ``CanConsumeJobs`` gates every MUTATING op (POST enqueue, claim, stage,
    result). It admits the same set MINUS ReadOnly — a read-only role may watch
    the queue but never drive it.

These tests pin that boundary: which role can GET the dashboard, POST-enqueue,
and claim. Auth mirrors jobs/tests/test_api.py exactly — Django auth Groups +
``force_authenticate`` (the permissions read Django groups, not OIDC scopes).
"""

import pytest
from django.contrib.auth.models import Group, User
from rest_framework.test import APIClient

from jobs import registry


def _user_in_group(username, group_name):
    """Create a user and (if named) add them to a Django auth Group."""
    user = User.objects.create_user(username, password="x")
    if group_name:
        Group.objects.get_or_create(name=group_name)
        user.groups.add(Group.objects.get(name=group_name))
    return user


def _client_for(username, group_name):
    c = APIClient()
    c.force_authenticate(user=_user_in_group(username, group_name))
    return c


@pytest.fixture
def _demo_kind():
    """A hook-free throwaway kind so tests don't need the casework stack."""
    saved = dict(registry._REGISTRY)
    registry.register(registry.KindSpec(kind="demo"))
    yield
    registry._REGISTRY.clear()
    registry._REGISTRY.update(saved)


# --------------------------------------------------------------------------
# GET dashboard — CanObserveJobs (content roles + ReadOnly, NOT Public/anon)
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_dashboard_anon_denied():
    assert APIClient().get("/api/jobs/").status_code in (401, 403)


@pytest.mark.django_db
def test_dashboard_no_role_denied():
    """An authenticated user with no group has no queue access at all."""
    assert _client_for("norole-observer", None).get("/api/jobs/").status_code == 403


@pytest.mark.django_db
@pytest.mark.parametrize("group", ["Caseworker", "ReadOnly", "JobPoller"])
def test_dashboard_observers_allowed(group):
    """The Caseworker content role, the JobPoller service account, AND the
    org-wide ReadOnly role may observe."""
    assert (
        _client_for(f"obs-{group.lower()}", group).get("/api/jobs/").status_code == 200
    )


# --------------------------------------------------------------------------
# POST enqueue — CanConsumeJobs (content roles, NOT ReadOnly/Public/anon)
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_enqueue_readonly_denied(_demo_kind):
    """ReadOnly can watch (GET 200) but must NOT be able to enqueue work."""
    c = _client_for("ro-enqueue", "ReadOnly")
    assert c.get("/api/jobs/").status_code == 200  # observe: allowed
    r = c.post("/api/jobs/", {"kind": "demo", "payload": {"n": 1}}, format="json")
    assert r.status_code == 403  # drive: denied


@pytest.mark.django_db
def test_enqueue_no_role_denied(_demo_kind):
    r = _client_for("norole-enqueue", None).post(
        "/api/jobs/", {"kind": "demo"}, format="json"
    )
    assert r.status_code == 403


@pytest.mark.django_db
@pytest.mark.parametrize("group", ["Caseworker", "JobPoller"])
def test_enqueue_drivers_allowed(_demo_kind, group):
    """The Caseworker content role + the JobPoller service account may enqueue."""
    r = _client_for(f"enq-{group.lower()}", group).post(
        "/api/jobs/", {"kind": "demo", "payload": {"n": 1}}, format="json"
    )
    assert r.status_code == 201


# --------------------------------------------------------------------------
# POST claim — CanConsumeJobs (same drive boundary)
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_claim_readonly_denied(_demo_kind):
    """ReadOnly is a pure observer — it cannot claim (drive) a job."""
    r = _client_for("ro-claim", "ReadOnly").post(
        "/api/jobs/claim/", {"kinds": ["demo"]}, format="json"
    )
    assert r.status_code == 403


@pytest.mark.django_db
def test_claim_caseworker_allowed(_demo_kind):
    """A driver with nothing to claim gets 204 (not a permission error)."""
    r = _client_for("cw-claim", "Caseworker").post(
        "/api/jobs/claim/", {"kinds": ["demo"]}, format="json"
    )
    assert r.status_code == 204
