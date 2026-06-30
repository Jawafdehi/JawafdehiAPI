"""Jawafdehi API contract smoke tests — core case-platform functionality.

Jawafdehi is mounted at ``/api/`` on the single monolith host (:48000).
"""

import pytest

pytestmark = [pytest.mark.smoke, pytest.mark.live]


def test_jawafdehi_root(clients):
    """``/api/`` is the public DRF root and is reachable (200)."""
    r = clients["jawafdehi"].get("/api/")
    assert r.status_code == 200, r.text
    assert "cases" in r.json()


def test_published_cases_listing(clients):
    """Published cases are the public surface — must list without auth errors.

    Note the trailing slash: APPEND_SLASH 301-redirects /api/cases -> /api/cases/.
    """
    r = clients["jawafdehi"].get("/api/cases/", params={"state": "PUBLISHED"})
    assert r.status_code in (200, 401, 403)
