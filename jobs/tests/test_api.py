"""HTTP-surface tests for the job-queue API (/api/jobs/*).

Covers the request/response layer the pure-function queue tests don't: auth
gating, enqueue/claim/stage/result status codes, the stale-finalize 409, and
the safe ``?limit=`` parse (a comment-flagged 500 risk).
"""

import pytest
from django.contrib.auth.models import Group, User
from rest_framework.test import APIClient

from jobs import registry


@pytest.fixture
def caseworker(db):
    Group.objects.get_or_create(name="Caseworker")
    user = User.objects.create_user("qworker", password="x")
    user.groups.add(Group.objects.get(name="Caseworker"))
    return user


@pytest.fixture
def client(caseworker):
    c = APIClient()
    c.force_authenticate(user=caseworker)
    return c


@pytest.fixture
def _demo_kind():
    """A hook-free throwaway kind so API tests don't need the casework stack."""
    saved = dict(registry._REGISTRY)
    registry.register(registry.KindSpec(kind="demo"))
    yield
    registry._REGISTRY.clear()
    registry._REGISTRY.update(saved)


@pytest.mark.django_db
def test_anon_cannot_claim():
    resp = APIClient().post("/api/jobs/claim/", {"kinds": ["demo"]}, format="json")
    assert resp.status_code in (401, 403)


@pytest.mark.django_db
def test_enqueue_claim_stage_result_flow(client, _demo_kind):
    # enqueue
    r = client.post(
        "/api/jobs/", {"kind": "demo", "payload": {"n": 1}}, format="json"
    )
    assert r.status_code == 201
    jid = r.json()["id"]

    # claim
    r = client.post("/api/jobs/claim/", {"kinds": ["demo"]}, format="json")
    assert r.status_code == 200
    assert r.json()["id"] == jid
    assert r.json()["status"] == "running"

    # stage heartbeat
    r = client.post(f"/api/jobs/{jid}/stage/", {"stage": "working"}, format="json")
    assert r.status_code == 200

    # result -> done
    r = client.post(
        f"/api/jobs/{jid}/result/",
        {"status": "done", "result": {"v": 1}},
        format="json",
    )
    assert r.status_code == 200
    assert r.json()["status"] == "done"


@pytest.mark.django_db
def test_empty_claim_returns_204(client, _demo_kind):
    r = client.post("/api/jobs/claim/", {"kinds": ["demo"]}, format="json")
    assert r.status_code == 204


@pytest.mark.django_db
def test_stale_finalize_returns_409(client, _demo_kind):
    client.post("/api/jobs/", {"kind": "demo"}, format="json")
    jid = client.post(
        "/api/jobs/claim/", {"kinds": ["demo"]}, format="json"
    ).json()["id"]
    # First finalize succeeds.
    ok = client.post(
        f"/api/jobs/{jid}/result/",
        {"status": "done", "result": {"v": 1}},
        format="json",
    )
    assert ok.status_code == 200
    # Second (stale) finalize is rejected — job no longer RUNNING.
    stale = client.post(
        f"/api/jobs/{jid}/result/",
        {"status": "done", "result": {"v": 2}},
        format="json",
    )
    assert stale.status_code == 409


@pytest.mark.django_db
@pytest.mark.parametrize("bad_limit", ["abc", "-5", ""])
def test_dashboard_bad_limit_does_not_500(client, _demo_kind, bad_limit):
    """A non-integer or negative ?limit= must not 500 (safe-parse -> 200)."""
    r = client.get(f"/api/jobs/?limit={bad_limit}")
    assert r.status_code == 200


@pytest.mark.django_db
def test_dashboard_filters_by_kind_and_status(client, _demo_kind):
    client.post("/api/jobs/", {"kind": "demo", "payload": {"n": 1}}, format="json")
    r = client.get("/api/jobs/?kind=demo&status=queued")
    assert r.status_code == 200
    assert all(j["kind"] == "demo" and j["status"] == "queued" for j in r.json())
