"""End-to-end contract tests against the LIVE court-case surface of the platform.

Court data lives on the unified ``/api/`` surface (HARD CUT 2026-07-01: the old
``/api/ngm/`` prefix is GONE). These hit the running gunicorn/Postgres stack
directly via ``httpx`` (not through the shared ``clients`` fixture) so the base
URL is self-contained — it comes from ``PLATFORM_BASE_URL`` (falling back to the
legacy ``NGM_API_BASE_URL``), both defaulting to the platform on :48000.

Contract notes verified live against the platform:

* Court routes are trailing-slashed: reads use ``/api/courts/`` /
  ``/api/courtcases/`` and the gated POSTs are ALSO slashed (``/api/query/``,
  ``/api/ingestion/cases/``). No-slash variants 301-redirect (httpx does NOT
  follow redirects by default).
* ``GET /api/courts/`` returns a BARE list ``[...]``.
* ``GET /api/courtcases/`` returns a full DRF page ``{"count","next","previous",
  "results"}``.
* ``POST /api/query/`` + ``POST /api/ingestion/*/`` are OIDC-gated. A missing
  bearer -> 401 "Authentication credentials were not provided."; a forged bearer
  -> 401 "Invalid token..."; a malformed ``Bearer`` header -> 401.
* Health is the ONE canonical ``GET /api/health`` (the per-plane NGM health was
  dropped in the unified cutover).
* The old ``GET /api/ngm/search`` 501 stub is gone with the ``/api/ngm`` prefix —
  unified search is ``GET /api/search/`` (covered in tests/jawafdehi).
"""

import os

import pytest

from conftest import make_client, skip_if_throttled

pytestmark = [pytest.mark.live]

BASE_URL = os.getenv("PLATFORM_BASE_URL") or os.getenv(
    "NGM_API_BASE_URL", "http://localhost:48000"
)


@pytest.fixture(scope="module")
def ngm():
    """A throttle-retrying httpx client for the live monolith (no auth header).

    follow_redirects=False so APPEND_SLASH 301s surface rather than being
    silently followed — paths below already carry the required trailing slash.
    """
    with make_client(BASE_URL) as client:
        yield client


# ---------------------------------------------------------------------------
# 1. Health
# ---------------------------------------------------------------------------
def test_health_ok(ngm):
    # ONE canonical platform health (slashless). The per-plane NGM health was
    # dropped in the unified cutover; the surviving endpoint reports service
    # "nes-api" (the entities app owns the canonical /api/health route).
    r = ngm.get("/api/health")
    skip_if_throttled(r)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("status") == "ok", body


# ---------------------------------------------------------------------------
# 2. Courts listing — BARE list [...]
# ---------------------------------------------------------------------------
def test_courts_listing_shape(ngm):
    r = ngm.get("/api/courts/")
    skip_if_throttled(r)
    assert r.status_code == 200, r.text
    body = r.json()
    assert isinstance(body, list), f"courts should be a bare list: {body!r}"


# ---------------------------------------------------------------------------
# 3. Cases listing — DRF page {"count","results","next","previous"}
# ---------------------------------------------------------------------------
def test_cases_listing_is_drf_page(ngm):
    r = ngm.get("/api/courtcases/", params={"limit": 1})
    skip_if_throttled(r)
    assert r.status_code == 200, r.text
    body = r.json()
    assert isinstance(body, dict), body
    assert "results" in body and "next" in body and "count" in body, body
    assert isinstance(body["results"], list)


# ---------------------------------------------------------------------------
# 4. Single case, nonexistent -> 404
# ---------------------------------------------------------------------------
def test_nonexistent_case_404(ngm):
    r = ngm.get("/api/courtcases/nonexistent-court/no-such-case-9999/")
    skip_if_throttled(r)
    assert r.status_code == 404, r.text
    assert "detail" in r.json()


# ---------------------------------------------------------------------------
# 5. Gated query, unauthenticated -> 401 (DRF auth fires before validation)
# ---------------------------------------------------------------------------
def test_query_unauthenticated_401(ngm):
    r = ngm.post("/api/query/", json={"query": "SELECT 1"})
    skip_if_throttled(r)
    assert r.status_code == 401, r.text
    assert "authentication credentials" in str(r.json().get("detail", "")).lower()


# ---------------------------------------------------------------------------
# 6. Gated query with a bogus / empty bearer token.
#
# NGM does REAL token verification: a syntactically-valid-but-fake token fails
# verification -> 401 ("Invalid token..."), so it never executes the SELECT.
# ---------------------------------------------------------------------------
def test_query_invalid_bearer_rejected(ngm):
    """A non-empty but invalid/forged bearer is REJECTED by token auth (401/403)."""
    r = ngm.post(
        "/api/query/",
        json={"query": "SELECT 1"},
        headers={"Authorization": "Bearer not-a-real-token"},
    )
    skip_if_throttled(r)
    assert r.status_code in (401, 403), (
        f"a forged bearer must be rejected, got {r.status_code}: {r.text}"
    )
    assert "invalid token" in str(r.json().get("detail", "")).lower()


def test_query_empty_bearer_rejected_401(ngm):
    """A malformed ``Bearer`` (no token value) is rejected by DRF auth (401)."""
    r = ngm.post(
        "/api/query/",
        json={"query": "SELECT 1"},
        headers={"Authorization": "Bearer"},
    )
    skip_if_throttled(r)
    assert r.status_code == 401, r.text
    detail = str(r.json().get("detail", "")).lower()
    # Either "authentication credentials..." or "invalid authorization header".
    assert "credentials" in detail or "authorization header" in detail, detail


# ---------------------------------------------------------------------------
# 7. The NGM 501 search stub is GONE — unified search moved to /api/search/.
# ---------------------------------------------------------------------------
def test_legacy_ngm_search_stub_removed(ngm):
    r = ngm.get("/api/ngm/search", params={"q": "x"})
    skip_if_throttled(r)
    assert r.status_code == 404, r.text


# ---------------------------------------------------------------------------
# 8. Ingestion write plane is OIDC-gated -> 401 unauthenticated (slashed paths).
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "path",
    [
        "/api/ingestion/cases/",
        "/api/ingestion/entities/resolve/",
        "/api/ingestion/documents/",
    ],
)
def test_ingestion_write_unauthenticated_401(ngm, path):
    r = ngm.post(path, json={})
    skip_if_throttled(r)
    assert r.status_code == 401, r.text
    assert "authentication credentials" in str(r.json().get("detail", "")).lower()
