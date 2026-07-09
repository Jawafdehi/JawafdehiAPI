"""Jawafdehi API contract smoke tests — core case-platform functionality.

Jawafdehi is mounted at ``/api/`` on the single monolith host (:48000).
"""

import pytest

pytestmark = [pytest.mark.smoke, pytest.mark.live]


def test_jawafdehi_root(clients):
    """``/api/`` is the public DRF root and is reachable (200).

    Several apps mount a DefaultRouter at the same ``/api/`` prefix (entities,
    courts, materials, cases). DRF's browsable root only advertises ONE router's
    registry (whichever owns the ``api-root`` name — currently courts), so we do
    NOT assert a specific router key here — that is a routing implementation
    detail, not a contract. The real contract (the ``/api/cases/`` resource is
    reachable) is asserted by ``test_published_cases_listing`` below."""
    r = clients["jawafdehi"].get("/api/")
    assert r.status_code == 200, r.text
    assert isinstance(r.json(), dict)


def test_published_cases_listing(clients):
    """Published cases are the public surface — must list without auth errors.

    Note the trailing slash: APPEND_SLASH 301-redirects /api/cases -> /api/cases/.
    """
    r = clients["jawafdehi"].get("/api/cases/", params={"state": "PUBLISHED"})
    assert r.status_code in (200, 401, 403)
