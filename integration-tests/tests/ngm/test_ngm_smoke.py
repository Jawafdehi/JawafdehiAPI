"""NGM API contract smoke tests — core data-lake/judicial functionality.

Monolith topology (2026-06): NGM is mounted under ``/api/ngm/`` on the single
platform host. Verified live against the running monolith (:48000):
  * trailing slash matters everywhere — Django ``APPEND_SLASH`` 301-redirects
    ``/api/ngm/courts`` to ``/api/ngm/courts/`` (httpx does not follow by default),
    and the gated POST surfaces are now ALSO slashed (``/api/ngm/query/``,
    ``/api/ngm/ingestion/cases/`` — the old no-slash convention is gone);
  * ``GET /api/ngm/courts/`` returns a BARE list ``[...]``;
  * ``GET /api/ngm/cases/`` returns a full DRF page ``{"count","next","previous",
    "results"}``;
  * ``POST /api/ngm/query/`` is OIDC-gated -> 401 unauthenticated;
  * the old ``/api/ngm/search`` 501 stub was REMOVED — unified search is
    ``GET /api/search/`` (see tests/jawafdehi for the search contract).
"""

import pytest

from fixtures.sample_data import SAMPLE_NGM_CASE_NUMBER, SAMPLE_NGM_COURT_IDENTIFIER

pytestmark = [pytest.mark.smoke, pytest.mark.live]


def test_ngm_health(clients):
    r = clients["ngm"].get("/api/ngm/health/")
    assert r.status_code == 200, r.text
    assert r.json().get("status") == "ok"


def test_ngm_courts_listing(clients):
    """``GET /api/ngm/courts/`` returns a BARE list of courts (no pagination)."""
    r = clients["ngm"].get("/api/ngm/courts/")
    assert r.status_code == 200, r.text
    body = r.json()
    assert isinstance(body, list), f"courts should be a bare list: {body!r}"


def test_ngm_cases_listing_is_drf_paginated(clients):
    """``GET /api/ngm/cases/`` returns a DRF page: ``count`` + ``results`` + ``next``."""
    r = clients["ngm"].get("/api/ngm/cases/", params={"limit": 1})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "results" in body and "next" in body and "count" in body, body
    assert isinstance(body["results"], list)


def test_ngm_case_entities_subresource(clients):
    """``/api/ngm/cases/{court}/{case}/entities/`` is the party-resolution surface."""
    r = clients["ngm"].get(
        f"/api/ngm/cases/{SAMPLE_NGM_COURT_IDENTIFIER}/{SAMPLE_NGM_CASE_NUMBER}/entities/",
        params={"limit": 1},
    )
    # 404 when the sample case isn't seeded; DRF page when it is.
    assert r.status_code in (200, 404), r.text
    if r.status_code == 200:
        assert "results" in r.json()


def test_ngm_gated_query_requires_auth(clients):
    """``POST /api/ngm/query/`` is OIDC-protected: no bearer -> 401 (before validation)."""
    r = clients["ngm"].post("/api/ngm/query/", json={"query": "SELECT 1"})
    assert r.status_code == 401, r.text


def test_ngm_gated_query_rejects_non_select(clients):
    """The gated SQL endpoint refuses an unauthenticated request at the door (401).

    Auth runs before the SELECT-only guard, so an UNauthenticated non-SELECT is
    rejected with 401 (a 400 'forbidden/SELECT-only' rejection only happens once
    authenticated).
    """
    r = clients["ngm"].post("/api/ngm/query/", json={"query": "DROP TABLE court_cases"})
    assert r.status_code in (400, 401, 403), r.text
    if r.status_code == 400:
        detail = str(r.json().get("detail", "")).lower()
        assert "select" in detail or "forbidden" in detail


def test_ngm_legacy_search_stub_is_gone(clients):
    """The NGM 501 ``/api/ngm/search`` stub was removed in the unified-search cutover.

    Platform search now lives at ``GET /api/search/`` only; there is no NGM-local
    search route anymore (404).
    """
    r = clients["ngm"].get("/api/ngm/search", params={"q": "test"})
    assert r.status_code == 404, r.text
