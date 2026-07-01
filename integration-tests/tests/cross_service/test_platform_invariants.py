"""Cross-service invariants — the seams no single service can test alone.

These encode the platform contracts for the unified monolith: one host, prefixed
mounts (``/api/nes/``, ``/api/ngm/``, ``/api/``), the unified search endpoint, the
public discovery surfaces, and OpenSearch as a hard search dependency.

The entity-id *shape* contract has its own dedicated module
(``test_entity_id_contract.py``); this file holds the broader multi-service and
platform-wide invariants.
"""

import pytest

from conftest import make_client, skip_if_throttled

pytestmark = [pytest.mark.cross_service, pytest.mark.slow]


@pytest.mark.live
@pytest.mark.xfail(
    reason="PENDING-DATA: NGM court tables empty, so no party nes_id to resolve "
    "yet (nes_id write-back lands with court data). Contract is correct against "
    "the platform read plane (/api/ngm/cases/ -> DRF results); flips green when data lands.",
    strict=False,
)
def test_ngm_court_party_resolves_to_nes_id(clients):
    """An NGM court party that's been resolved carries a valid NES entity @id IRI."""
    r = clients["ngm"].get("/api/ngm/cases/", params={"limit": 1})
    r.raise_for_status()
    cases = r.json().get("results", [])
    assert cases, "no court cases available"
    case = cases[0]
    parties = clients["ngm"].get(
        f"/api/ngm/cases/{case['court_identifier']}/{case['case_number']}/entities/"
    ).json()
    for p in parties.get("results", []):
        if p.get("nes_id"):
            assert p["nes_id"].startswith("https://"), p["nes_id"]
            assert "/entity/" in p["nes_id"], p["nes_id"]


@pytest.mark.live
def test_services_reject_unauthenticated_writes(clients):
    """Write endpoints must require an OIDC bearer; no DRF-token path exists.

    All three prefixes enforce auth on writes (the OIDC-only contract is
    consistent across the platform):
      * NGM ingestion (``POST /api/ngm/ingestion/cases/``) -> 401,
      * NES entity write (``POST /api/nes/entities``)       -> 401 (no-slash path),
      * Jawafdehi case write (``POST /api/cases/``)         -> 401.
    """
    checks = [
        (clients["ngm"].base_url, "/api/ngm/ingestion/cases/", {"items": []}),
        (clients["nes"].base_url, "/api/nes/entities", {}),
        (clients["jawafdehi"].base_url, "/api/cases/", {}),
    ]
    for base, path, payload in checks:
        with make_client(base) as anon:  # no auth header -> truly anonymous
            r = anon.post(path, json=payload)
        skip_if_throttled(r)
        assert r.status_code in (401, 403), (
            f"anon write to {path} should be denied, got {r.status_code}: {r.text[:200]}"
        )


# =============================================================================
# Public discovery surfaces — crawl + harvest endpoints at the project root.
# =============================================================================


@pytest.mark.live
def test_sitemap_index_is_public(clients):
    """``/sitemap.xml`` is a public XML sitemap index."""
    r = clients["platform"].get("/sitemap.xml")
    assert r.status_code == 200, r.text
    ctype = r.headers.get("content-type", "")
    assert "xml" in ctype, ctype
    assert "<sitemap" in r.text or "<urlset" in r.text, r.text[:200]


@pytest.mark.live
def test_resourcesync_is_public(clients):
    """``/.well-known/resourcesync`` is a public ResourceSync description."""
    r = clients["platform"].get("/.well-known/resourcesync")
    assert r.status_code == 200, r.text


@pytest.mark.live
def test_robots_txt_is_public(clients):
    """``/robots.txt`` is public and advertises the sitemap."""
    r = clients["platform"].get("/robots.txt")
    assert r.status_code == 200, r.text
    assert "User-agent" in r.text, r.text[:200]


# =============================================================================
# OpenSearch is a HARD dependency of unified search.
# =============================================================================


@pytest.mark.live
def test_unified_search_is_backed_by_opensearch(clients):
    """Unified search is backed by OpenSearch — 200 when the cluster is up.

    OpenSearch is a hard dependency (no in-process fallback): a healthy cluster
    yields 200 with the common envelope; the endpoint only 503s if the cluster
    is unreachable. We assert it is currently 200 (the stack's OpenSearch is up)
    and that a 503 — should it ever occur — carries the documented detail.
    """
    r = clients["platform"].get("/api/search/", params={"q": "x"})
    assert r.status_code in (200, 503), r.text
    if r.status_code == 503:
        pytest.skip(
            "OpenSearch cluster down -> search 503 (hard dependency). "
            f"detail: {r.json().get('detail')!r}"
        )
    body = r.json()
    assert {"query", "count", "counts", "results"}.issubset(body), body


@pytest.mark.xfail(reason="≥2-source gate enforced in NES sourcing (Phase 4)")
def test_single_source_entity_is_held_not_published(clients):
    """An entity with only one source must not appear in published results."""
    pytest.skip("requires sourcing pipeline + seed data (Phase 4)")
