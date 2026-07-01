"""Live cross-service end-to-end tests against the running monolith.

These run against the REAL platform (no mocks): NES, NGM and Jawafdehi all live
in ONE monolith process behind a single host (``PLATFORM_BASE_URL``, default
:48000), mounted under distinct path prefixes (``/api/nes/``, ``/api/ngm/``,
``/api/``). The shared ``clients`` fixture's per-service clients all point at the
same host and differ only by the path prefix the test passes.

The goal is to prove the **unification contracts** that thread the former
services together, against live data where it exists and structurally
(vacuously, with a clear reason) where the data hasn't landed yet:

1. Canonical entity-id contract holds across services (THE join key — an @id IRI).
2. NGM ``nes_id`` references obey the same @id IRI contract.
3. The unified search endpoint (``/api/search/``) replaces the old per-service
   search and returns the common envelope.
4. The auth plane is consistent (anonymous writes rejected on all three).
5. Liveness / topology: one host, every prefix's health-equivalent reachable.
"""

import os
import re

import httpx
import pytest

from conftest import make_client, skip_if_throttled

pytestmark = [pytest.mark.live, pytest.mark.cross_service]


# --- The canonical entity-id @id IRI shape contract --------------------------
# https?://<host>/entity/<prefix>/<slug>; prefix 1..4 [a-z0-9_] segs, slug
# [a-z0-9][a-z0-9-]*. Mirrors shared.jawafdehi_shared.entities.ids.ENTITY_IRI_RE.
ENTITY_IRI_RE = re.compile(
    r"^https?://[^/]+/entity/[a-z0-9_]+(?:/[a-z0-9_]+){0,3}/[a-z0-9][a-z0-9-]*$"
)


def assert_entity_iri_shape(iri: str) -> None:
    assert isinstance(iri, str), f"not a string: {iri!r}"
    assert ENTITY_IRI_RE.match(iri), f"id fails canonical entity-IRI grammar: {iri!r}"


def _entity_id(entity: dict) -> str:
    return entity.get("@id") or entity.get("id") or ""


def _anon_client(base_url) -> httpx.Client:
    """A client with NO Authorization header, to test anonymous-write rejection."""
    return make_client(base_url)


# =============================================================================
# 5. Liveness / topology — one host, every mounted prefix is reachable.
#    (Listed first so a red here explains failures below.)
# =============================================================================


def test_topology_single_host_all_prefixes_live(clients):
    """One monolith host serves NES, NGM and Jawafdehi under distinct prefixes.

    PROVEN-LIVE: ``/api/nes/health`` 200 (NES health is slashless),
    ``/api/ngm/health/`` 200, and the Jawafdehi ``/api/`` root 200 — same host.
    """
    nes = clients["nes"].get("/api/nes/health")
    skip_if_throttled(nes)
    assert nes.status_code == 200, f"NES health: {nes.status_code}"
    assert nes.json().get("status") == "ok", nes.text

    ngm = clients["ngm"].get("/api/ngm/health/")
    skip_if_throttled(ngm)
    assert ngm.status_code == 200, f"NGM health: {ngm.status_code}"
    assert ngm.json().get("status") == "ok", ngm.text

    jaw = clients["jawafdehi"].get("/api/")
    skip_if_throttled(jaw)
    assert jaw.status_code == 200, f"Jawafdehi /api/: {jaw.status_code}"
    root = jaw.json()
    # The DRF API root advertises the public Jawafdehi collections. Entities are
    # no longer here (NES-owned), so only cases/sources are guaranteed.
    assert "cases" in root, f"Jawafdehi root missing 'cases': {root}"

    # Same host for every prefix — the defining property of the platform.
    assert clients["nes"].base_url == clients["ngm"].base_url == clients["jawafdehi"].base_url


# =============================================================================
# 1. Canonical entity-id contract holds across services (THE join key).
# =============================================================================


def test_nes_canonical_entity_id_contract(clients):
    """Pull entities from NES and assert every @id is a valid join key.

    PROVEN-LIVE (structurally): NES is the canonical owner; the list endpoint is
    up and returns the ``{entities, total, ...}`` envelope. Every @id it returns
    MUST satisfy the canonical IRI contract.
    PENDING-DATA: the NES store is empty today (total 0), so the id-shape gate
    holds vacuously.
    """
    r = clients["nes"].get("/api/nes/entities", params={"limit": 100})
    skip_if_throttled(r)
    assert r.status_code == 200, f"NES list: {r.status_code} {r.text[:200]}"
    body = r.json()
    assert "entities" in body and "total" in body, body
    entities = body.get("entities") or []

    for e in entities:
        assert_entity_iri_shape(_entity_id(e))

    if not entities:
        pytest.skip(
            "PENDING-DATA: NES entity store empty (no pilot persons seeded). "
            "Envelope + @id-shape contract hold vacuously."
        )


def test_nes_query_endpoint_entity_id_contract(clients):
    """The query path returns the same canonical @ids as the list path.

    PROVEN-LIVE (structurally): the query path is up and returns the envelope.
    PENDING-DATA: store empty, so 'renu' matches nothing today — skip until
    seeded. Any hit returned MUST be a contract-valid join key.
    """
    r = clients["nes"].get("/api/nes/entities", params={"query": "renu", "limit": 5})
    skip_if_throttled(r)
    assert r.status_code == 200, f"NES query: {r.status_code}"
    entities = r.json().get("entities") or []
    if not entities:
        pytest.skip("PENDING-DATA: NES store empty — query 'renu' returns nothing yet.")
    hit = entities[0]
    assert_entity_iri_shape(_entity_id(hit))
    assert "/entity/person/" in _entity_id(hit), _entity_id(hit)


# =============================================================================
# 2. NGM nes_id references obey the same @id IRI contract.
# =============================================================================


def test_ngm_nes_id_references_obey_entity_contract(clients):
    """Any ``nes_id`` NGM exposes MUST be a valid canonical entity @id IRI.

    PROVEN-LIVE (structurally): NGM read plane is up and returns the DRF page
    shape (``results``); the nes_id shape gate is asserted on anything present.
    PENDING-DATA: no court parties exist yet, so the join is not yet exercised
    end-to-end (the tables are empty by design at this phase).
    """
    cases = clients["ngm"].get("/api/ngm/cases/", params={"limit": 50})
    skip_if_throttled(cases)
    assert cases.status_code == 200, f"NGM /api/ngm/cases/: {cases.status_code}"
    case_items = cases.json().get("results", [])

    # NGM also exposes a flat /api/ngm/entities/ (court-case entities) — sweep it.
    ents = clients["ngm"].get("/api/ngm/entities/", params={"limit": 50})
    skip_if_throttled(ents)
    assert ents.status_code == 200, f"NGM /api/ngm/entities/: {ents.status_code}"
    entity_items = ents.json().get("results", [])

    nes_ids: list[str] = []
    for case in case_items:
        court = case.get("court_identifier") or case.get("court")
        num = case.get("case_number")
        if not (court and num):
            continue
        parties = clients["ngm"].get(f"/api/ngm/cases/{court}/{num}/entities/")
        if parties.status_code == 200:
            for p in parties.json().get("results", []):
                if p.get("nes_id"):
                    nes_ids.append(p["nes_id"])
    for p in entity_items:
        if p.get("nes_id"):
            nes_ids.append(p["nes_id"])

    if not nes_ids:
        pytest.skip(
            "PENDING-DATA: NGM court tables empty — no nes_id to join yet. "
            "Contract holds vacuously; ready for when court data lands."
        )

    for nes_id in nes_ids:
        assert_entity_iri_shape(nes_id)


# =============================================================================
# 3. Unified search — replaces the old per-service search surfaces.
# =============================================================================


def test_unified_search_envelope_and_replaces_old_surfaces(clients):
    """``GET /api/search/`` is the ONE platform search endpoint.

    It replaces the old cases-scoped ``/api/search`` UnifiedSearchView and the
    NGM 501 ``/api/ngm/search`` stub (both GONE). It is a public read returning
    the common envelope ``{query, lang, page, page_size, count, counts, results}``.

    PROVEN-LIVE: the endpoint answers 200 with the documented envelope.
    PENDING-DATA: the index is empty today, so ``count`` is 0 and ``results`` is
    empty — we assert the SHAPE + 200, not specific hits.
    """
    r = clients["platform"].get("/api/search/", params={"q": "nepal"})
    skip_if_throttled(r)
    assert r.status_code == 200, f"unified search: {r.status_code} {r.text[:200]}"
    body = r.json()
    for key in ("query", "lang", "page", "page_size", "count", "counts", "results"):
        assert key in body, f"search envelope missing {key!r}: {body}"
    assert body["query"] == "nepal", body
    assert isinstance(body["count"], int) and body["count"] >= 0
    assert isinstance(body["counts"], dict)
    assert isinstance(body["results"], list)
    # Empty corpus today: count 0, no results. (Shape proven; data PENDING.)


def test_unified_search_requires_q(clients):
    """``q`` is mandatory -> 400 when omitted (DRF serializer validation)."""
    r = clients["platform"].get("/api/search/")
    skip_if_throttled(r)
    assert r.status_code == 400, r.text
    assert "q" in r.json(), r.text


def test_old_per_service_search_surfaces_removed(clients):
    """The pre-unification search surfaces are gone.

    * ``GET /api/ngm/search`` (the 501 stub) -> 404.
    * ``GET /api/search`` (no slash) -> 301 to the slashed unified endpoint
      (APPEND_SLASH); the unified view owns the slashed path only.
    """
    ngm_old = clients["ngm"].get("/api/ngm/search", params={"q": "x"})
    skip_if_throttled(ngm_old)
    assert ngm_old.status_code == 404, ngm_old.text

    no_slash = clients["platform"].get("/api/search", params={"q": "x"})
    skip_if_throttled(no_slash)
    assert no_slash.status_code == 301, no_slash.text


# =============================================================================
# 4. Auth plane consistency — anonymous writes rejected everywhere.
# =============================================================================


def test_ngm_rejects_anonymous_ingestion_write(clients):
    """NGM ingestion is OIDC-gated and rejects an anonymous write (slashed path)."""
    with _anon_client(clients["ngm"].base_url) as anon:
        r = anon.post("/api/ngm/ingestion/cases/", json={"items": []})
    skip_if_throttled(r)
    assert r.status_code in (401, 403), (
        f"NGM ingestion should reject anon write, got {r.status_code}: {r.text[:200]}"
    )


def test_jawafdehi_rejects_anonymous_case_write(clients):
    """Jawafdehi rejects an anonymous POST to /api/cases/."""
    with _anon_client(clients["jawafdehi"].base_url) as anon:
        r = anon.post("/api/cases/", json={})
    skip_if_throttled(r)
    assert r.status_code in (401, 403), (
        f"Jawafdehi case write should reject anon, got {r.status_code}: {r.text[:200]}"
    )


def test_nes_rejects_anonymous_entity_write(clients):
    """NES rejects an anonymous write — auth plane consistent across all prefixes.

    NES's write surface is the NO-slash path (``/api/nes/entities``);
    ``/api/nes/entities/`` (trailing slash) is a 404, so do not add a slash here.
    """
    with _anon_client(clients["nes"].base_url) as anon:
        r = anon.post("/api/nes/entities", json={})
    skip_if_throttled(r)
    assert r.status_code in (401, 403), (
        f"expected NES to reject anon write with 401/403, got {r.status_code}"
    )


def test_shared_oidc_issuer_is_reachable(clients):
    """The platform shares ONE OIDC issuer (Zitadel); it must be reachable.

    PROVEN-LIVE: a single shared issuer exposes a valid OpenID discovery document
    advertising a JWKS + token endpoint — the shared root the wired auth planes
    validate tokens against.
    """
    issuer = os.environ.get("OIDC_ISSUER", "http://localhost:48080")
    try:
        with httpx.Client(timeout=10) as c:
            r = c.get(f"{issuer}/.well-known/openid-configuration")
    except httpx.HTTPError as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"OIDC issuer {issuer} unreachable: {exc}")
    assert r.status_code == 200, f"OIDC discovery: {r.status_code}"
    disc = r.json()
    assert disc.get("issuer"), disc
    assert disc.get("jwks_uri"), disc
    assert disc.get("token_endpoint"), disc
