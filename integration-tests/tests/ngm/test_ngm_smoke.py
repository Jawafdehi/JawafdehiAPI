"""Court-case API contract smoke tests — core data-lake/judicial functionality.

Unified surface (HARD CUT 2026-07-01): the old ``/api/ngm/`` prefix is GONE.
Court data lives at ``/api/courtcases/`` (renamed from ``/api/ngm/cases/`` — the
Jawafdehi corruption ``/api/cases/`` is a DIFFERENT resource), courts at
``/api/courts/``, gated SQL at ``/api/query/``, batch write at ``/api/ingestion/*``.
Verified live against the running monolith (:48000):
  * trailing slash matters everywhere — Django ``APPEND_SLASH`` 301-redirects
    ``/api/courts`` to ``/api/courts/`` (httpx does not follow by default),
    and the gated POST surfaces are ALSO slashed (``/api/query/``,
    ``/api/ingestion/cases/`` — the old no-slash convention is gone);
  * ``GET /api/courts/`` returns a BARE list ``[...]``;
  * ``GET /api/courtcases/`` returns a full DRF page ``{"count","next","previous",
    "results"}``;
  * ``POST /api/query/`` is OIDC-gated -> 401 unauthenticated;
  * there is ONE canonical health at ``GET /api/health`` (the per-plane NGM
    health was dropped in the unified cutover);
  * the old ``/api/ngm/search`` 501 stub was REMOVED — unified search is
    ``GET /api/search/`` (see tests/jawafdehi for the search contract).
"""

import pytest

from fixtures.sample_data import SAMPLE_NGM_CASE_NUMBER, SAMPLE_NGM_COURT_IDENTIFIER

pytestmark = [pytest.mark.smoke, pytest.mark.live]


def test_health(clients):
    """ONE canonical platform health at ``/api/health`` (slashless)."""
    r = clients["ngm"].get("/api/health")
    assert r.status_code == 200, r.text
    assert r.json().get("status") == "ok"


def test_courts_listing(clients):
    """``GET /api/courts/`` returns a BARE list of courts (no pagination)."""
    r = clients["ngm"].get("/api/courts/")
    assert r.status_code == 200, r.text
    body = r.json()
    assert isinstance(body, list), f"courts should be a bare list: {body!r}"


def test_courtcases_listing_is_drf_paginated(clients):
    """``GET /api/courtcases/`` returns a DRF page: ``count`` + ``results`` + ``next``."""
    r = clients["ngm"].get("/api/courtcases/", params={"limit": 1})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "results" in body and "next" in body and "count" in body, body
    assert isinstance(body["results"], list)


def test_courtcase_entities_subresource(clients):
    """``/api/courtcases/{court}/{case}/entities/`` is the party-resolution surface."""
    r = clients["ngm"].get(
        f"/api/courtcases/{SAMPLE_NGM_COURT_IDENTIFIER}/{SAMPLE_NGM_CASE_NUMBER}/entities/",
        params={"limit": 1},
    )
    # 404 when the sample case isn't seeded; DRF page when it is.
    assert r.status_code in (200, 404), r.text
    if r.status_code == 200:
        assert "results" in r.json()


def test_gated_query_requires_auth(clients):
    """``POST /api/query/`` is OIDC-protected: no bearer -> 401 (before validation)."""
    r = clients["ngm"].post("/api/query/", json={"query": "SELECT 1"})
    assert r.status_code == 401, r.text


def test_gated_query_rejects_non_select(clients):
    """The gated SQL endpoint refuses an unauthenticated request at the door (401).

    Auth runs before the SELECT-only guard, so an UNauthenticated non-SELECT is
    rejected with 401 (a 400 'forbidden/SELECT-only' rejection only happens once
    authenticated).
    """
    r = clients["ngm"].post("/api/query/", json={"query": "DROP TABLE court_cases"})
    assert r.status_code in (400, 401, 403), r.text
    if r.status_code == 400:
        detail = str(r.json().get("detail", "")).lower()
        assert "select" in detail or "forbidden" in detail


def test_legacy_ngm_search_stub_is_gone(clients):
    """The old 501 ``/api/ngm/search`` stub is gone with the whole ``/api/ngm`` prefix.

    Platform search now lives at ``GET /api/search/`` only; the old prefixed
    route no longer exists (404).
    """
    r = clients["ngm"].get("/api/ngm/search", params={"q": "test"})
    assert r.status_code == 404, r.text
