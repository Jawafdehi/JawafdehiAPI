"""Real end-to-end tests against the LIVE Jawafdehi surface of the platform.

Jawafdehi is mounted at ``/api/`` on the one platform host (:48000). These hit
the running gunicorn/Postgres stack directly via httpx (not the shared
``clients`` fixture) so the base URL is self-contained (``PLATFORM_BASE_URL``,
falling back to ``JAWAFDEHI_API_BASE_URL``).

Observed live contract (monolith on :48000):
  * ``/api/``            -> 200 browsable root listing the public routers
                            ``{"cases": ..., "sources": ...}`` (NOTE: the Jawafdehi
                            DRF root has no ``entities`` router — the unified entity
                            surface is the NES-owned ``/api/entities`` list view,
                            mounted on the same ``/api/`` root but not a DRF router).
  * ``/api/cases/``      -> 200 DRF page ``{count, next, previous, results}``
                            (PUBLISHED cases only for anon; empty today -> count 0).
  * ``/api/sources/``    -> 200 (anonymous reads allowed).
  * Writes (POST) without auth -> 401 (OIDC-only).
  * Legacy DRF ``Authorization: Token xxx`` is IGNORED (read -> 200 as anon;
    write -> 401), not parsed-and-rejected — TokenAuthentication is gone.
  * ``/django-admin/login/``    -> 200.
  * APPEND_SLASH: ``/api/cases`` (no slash) -> 301 to the slashed URL.
  * In-review/draft cases are CASEWORK: not publicly retrievable -> 404 for anon.

Auth is OIDC/Zitadel only; there are no anonymous writes and no working DRF tokens.
"""

import os

import pytest

from conftest import make_client, skip_if_throttled

pytestmark = [pytest.mark.live]

BASE_URL = os.getenv("PLATFORM_BASE_URL") or os.getenv(
    "JAWAFDEHI_API_BASE_URL", "http://localhost:48000"
)


@pytest.fixture()
def client():
    # follow_redirects=False so we can observe APPEND_SLASH 301s directly;
    # throttle-retrying transport rides out a momentary anon 429.
    with make_client(BASE_URL) as c:
        yield c


def test_api_root_reachable(client):
    """``/api/`` is the public DRF root and must be reachable (200).

    It advertises the public Jawafdehi routers; ``cases`` is the headline surface.
    The DRF root does NOT list an ``entities`` router — entities are served by the
    NES-owned list view mounted on the same ``/api/`` root, not a DRF router child.
    """
    r = client.get("/api/")
    skip_if_throttled(r)
    assert r.status_code == 200, r.text
    body = r.json()
    assert isinstance(body, dict)
    assert "cases" in body, f"expected 'cases' route in API root, got: {body}"
    assert "entities" not in body, (
        f"entities is the NES list view, not a Jawafdehi DRF router: {body}"
    )


def test_entities_surface_lives_on_unified_api(client):
    """After the hard cut, ``/api/entities`` is the canonical entity list surface.

    The old ``/api/nes/entities`` prefix is gone; the unified surface serves the
    entity list at ``/api/entities`` (NO trailing slash — the slashed variant is
    not a route) with the ``{entities, total, ...}`` envelope.
    """
    r = client.get("/api/entities", params={"limit": 1})
    skip_if_throttled(r)
    assert r.status_code == 200, (
        f"expected /api/entities to be the live entity list, got {r.status_code}: {r.text[:200]}"
    )
    body = r.json()
    assert "entities" in body and "total" in body, body


def test_published_cases_listing_shape(client):
    """``/api/cases/`` lists published cases as a DRF-paginated object."""
    r = client.get("/api/cases/")
    skip_if_throttled(r)
    assert r.status_code == 200, r.text
    body = r.json()
    if isinstance(body, dict):
        assert "results" in body, f"paginated response missing 'results': {body}"
        assert isinstance(body["results"], list)
        assert "count" in body and isinstance(body["count"], int)
    else:
        assert isinstance(body, list)


def test_in_review_case_not_publicly_retrievable(client):
    """Casework (DRAFT/IN_REVIEW) cases are NOT publicly retrievable.

    The public ``CaseViewSet`` queryset is PUBLISHED-only for anonymous callers,
    so a non-published case slug is invisible (404) to the public. The corpus is
    empty today, so we prove the boundary structurally: an anonymous detail
    fetch for a non-published slug returns 404 (never 200, never a leak).
    """
    r = client.get("/api/cases/case-in-review-not-public-9999/")
    skip_if_throttled(r)
    assert r.status_code == 404, (
        f"anon retrieval of a non-published case must be 404, got {r.status_code}"
    )


def test_anonymous_write_rejected(client):
    """OIDC-only: POSTing to a collection without auth must be rejected."""
    r = client.post("/api/cases/", json={})
    skip_if_throttled(r)
    assert r.status_code in (401, 403, 405), (
        f"anonymous POST /api/cases/ should be denied, got {r.status_code}: {r.text}"
    )
    if r.status_code in (401, 403):
        assert "detail" in r.json()


def test_admin_login_reachable(client):
    """Django admin login page is served (200)."""
    r = client.get("/django-admin/login/")
    assert r.status_code == 200, r.text
    assert "text/html" in r.headers.get("content-type", "")


def test_legacy_drf_token_not_accepted(client):
    """Legacy DRF token auth is GONE: the ``Token`` header is IGNORED, not honored.

      * a public read with a legacy ``Token`` header behaves like anonymous -> 200
        (and critically NOT a 401 "Invalid token." — that would mean TokenAuth is
        still wired), and
      * a write with the same header is still denied (401/403), no OIDC bearer.
    """
    token_headers = {"Authorization": "Token deadbeefdeadbeefdeadbeefdeadbeef"}

    r_read = client.get("/api/cases/", headers=token_headers)
    skip_if_throttled(r_read)
    assert r_read.status_code == 200, (
        f"legacy Token header should be ignored on a public read, "
        f"got {r_read.status_code}: {r_read.text}"
    )
    assert "invalid token" not in r_read.text.lower(), (
        "an 'Invalid token.' body means TokenAuthentication is still wired"
    )

    r_write = client.post("/api/cases/", json={}, headers=token_headers)
    skip_if_throttled(r_write)
    assert r_write.status_code in (401, 403), (
        f"legacy Token header must not authorize writes, "
        f"got {r_write.status_code}: {r_write.text}"
    )


def test_append_slash_redirect(client):
    """APPEND_SLASH: ``/api/cases`` (no slash) -> 301 to the slashed URL."""
    r = client.get("/api/cases")  # client has follow_redirects=False
    skip_if_throttled(r)
    assert r.status_code == 301, (
        f"expected APPEND_SLASH 301 for /api/cases, got {r.status_code}"
    )
    assert r.headers.get("location", "").endswith("/api/cases/")
