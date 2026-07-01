"""End-to-end tests against the LIVE entities surface of the platform.

Entities are mounted at ``/api/entities`` on the one platform host — the old
``/api/nes/`` prefix was removed in the 2026-07-01 hard cut. These assert the
*contract* (response shapes, the canonical entity ``@id`` IRI rule) rather than
a fixed entity set, since seeded data may change. Where data is asserted, it is
gated on ``total > 0``.

Clean-slate IRI contract (2026-06): entities are schema.org JSON-LD keyed by a
canonical ``@id`` IRI ``https://jawafdehi.org/entity/<prefix>/<slug>`` — there
is NO legacy ``entity:<prefix>/<slug>`` form. The detail route accepts either a
url-encoded IRI or a bare ``<prefix>/<slug>`` path.

Verified live against the platform (:48000):
  * health ``GET /api/health`` (NO trailing slash — slashless route; the
    ``/api/health/`` variant 404s) -> ``{"status":"ok","service":"nes-api"}``;
  * list/search shape ``{"entities": [...], "total", "limit", "offset"}``;
  * search param is ``query`` (not ``q``);
  * ``/api/entity_prefixes`` -> ``{"prefixes": [...]}``;
  * the entities store is EMPTY today (total 0) — data-dependent assertions are gated.
"""

import re

import pytest

pytestmark = [pytest.mark.live]

# Mirrors shared.jawafdehi_shared.entities.ids (entity @id IRI grammar):
#   https?://<host>/entity/<prefix>/<slug>
# prefix: 1-4 slash-joined [a-z0-9_] segments; slug: [a-z0-9][a-z0-9-]*.
ENTITY_IRI_RE = re.compile(
    r"^https?://[^/]+/entity/[a-z0-9_]+(?:/[a-z0-9_]+){0,3}/[a-z0-9][a-z0-9-]*$"
)

# Known pilot entities, by language of the query that should surface them.
ENGLISH_PROBE = "Renu Dahal"
DEVANAGARI_PROBE = "बालेन्द्र"  # Balendra Shah


def _entity_id(entity: dict) -> str:
    """The canonical id field is the schema.org ``@id`` IRI (``id`` fallback)."""
    return entity.get("@id") or entity.get("id") or ""


def _assert_entity_iri_shape(iri: str) -> None:
    assert isinstance(iri, str) and ENTITY_IRI_RE.match(iri), f"bad entity IRI: {iri!r}"


def _assert_list_shape(body: dict) -> None:
    """{entities: list, total: int>=0, limit, offset}."""
    assert isinstance(body.get("entities"), list), f"entities not a list: {body!r}"
    assert isinstance(body.get("total"), int) and body["total"] >= 0
    assert isinstance(body.get("limit"), int)
    assert isinstance(body.get("offset"), int)


# --- 1. health ---------------------------------------------------------------


def test_health_ok(clients):
    r = clients["nes"].get("/api/health")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("status") == "ok", body
    assert body.get("service") == "nes-api", body


# --- 2. list response shape --------------------------------------------------


def test_entities_list_response_shape(clients):
    r = clients["nes"].get("/api/entities", params={"limit": 5})
    assert r.status_code == 200, r.text
    _assert_list_shape(r.json())


# --- 3. bilingual search -----------------------------------------------------


def _names_blob(entity: dict) -> str:
    """Flatten any en/ne name fields of a JSON-LD entity into one search string."""
    out: list[str] = []
    name = entity.get("name")
    if isinstance(name, dict):
        out.extend(str(v) for v in name.values() if v)
    elif isinstance(name, str):
        out.append(name)
    for n in entity.get("names") or []:
        if isinstance(n, dict):
            for lang in ("en", "ne"):
                block = n.get(lang) or {}
                if isinstance(block, dict):
                    out.extend(str(v) for v in block.values() if v)
                elif block:
                    out.append(str(block))
    return " ".join(out)


@pytest.mark.parametrize(
    "query, needle",
    [(ENGLISH_PROBE, "Renu"), (DEVANAGARI_PROBE, "बालेन्द्र")],
    ids=["english", "devanagari"],
)
def test_bilingual_search_returns_list(clients, query, needle):
    """Both an English and a Devanagari query return the list shape.

    Shape is asserted unconditionally; the needle check is gated on data being
    present (the corpus is empty today).
    """
    r = clients["nes"].get("/api/entities", params={"query": query, "limit": 10})
    assert r.status_code == 200, r.text
    body = r.json()
    _assert_list_shape(body)
    if body["total"] > 0:
        blobs = " ".join(_names_blob(e) for e in body["entities"])
        assert needle in blobs, (
            f"query {query!r} returned {body['total']} hits but none matched "
            f"{needle!r}; ids={[_entity_id(e) for e in body['entities']]}"
        )


# --- 4. entity-by-id round trip ----------------------------------------------


def test_entity_by_id_round_trip(clients):
    """List one entity, fetch it by its @id IRI, assert the IRI contract.

    PENDING-DATA: the NES store is empty today, so this skips until an entity is
    seeded. The detail route accepts the url-encoded IRI verbatim.
    """
    listing = clients["nes"].get("/api/entities", params={"limit": 1})
    assert listing.status_code == 200, listing.text
    body = listing.json()
    _assert_list_shape(body)
    if not body["entities"]:
        pytest.skip("PENDING-DATA: NES entity store empty — nothing to round-trip.")

    listed = body["entities"][0]
    iri = _entity_id(listed)
    _assert_entity_iri_shape(iri)

    # The detail route accepts a bare <prefix>/<slug> path (after stripping the
    # IRI base) or the url-encoded IRI; use the bare path form which is robust.
    prefix_slug = iri.split("/entity/", 1)[1]
    fetched = clients["nes"].get(f"/api/entities/{prefix_slug}")
    assert fetched.status_code == 200, fetched.text
    detail = fetched.json()
    assert _entity_id(detail) == iri, f"round-trip id mismatch: {detail!r} != {iri!r}"


# --- 5. entity prefixes ------------------------------------------------------


def test_entity_prefixes_response_shape(clients):
    """``/api/entity_prefixes`` -> ``{"prefixes": [...]}``.

    PENDING-DATA: the prefix list is derived from seeded entities, so it is empty
    today. Once persons/organizations land it includes those top-level prefixes.
    """
    r = clients["nes"].get("/api/entity_prefixes")
    assert r.status_code == 200, r.text
    prefixes = r.json().get("prefixes")
    assert isinstance(prefixes, list), f"prefixes not a list: {r.text}"
    if prefixes:
        assert "person" in prefixes or "organization" in prefixes, prefixes


# --- 6. pagination -----------------------------------------------------------


def test_pagination_limit_respected(clients):
    r = clients["nes"].get("/api/entities", params={"limit": 1})
    assert r.status_code == 200, r.text
    body = r.json()
    _assert_list_shape(body)
    assert body["limit"] == 1, body
    assert len(body["entities"]) <= 1, body
    if body["total"] > 1:
        assert len(body["entities"]) == 1, body
