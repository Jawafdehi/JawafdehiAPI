"""NES API contract smoke tests — core entity-service functionality.

Monolith topology (2026-06): NES is mounted under ``/api/nes/`` on the single
platform host. Verified live against the running monolith (:48000):
  * health is ``GET /api/nes/health`` (NO trailing slash — NES registers it
    slashless; the ``/api/nes/health/`` variant 404s) ->
    ``{"status": "ok", "service": "nes-api"}``;
  * search param is ``query`` (``q`` is silently ignored), not ``q``;
  * list shape is ``{"entities": [...], "total", "limit", "offset"}``
    (the entity id field is the schema.org ``@id`` IRI), not ``{"results": [...]}``;
  * prefixes come back as ``{"prefixes": [...]}``.
"""

import pytest

pytestmark = [pytest.mark.smoke, pytest.mark.live]


def test_nes_health(clients):
    r = clients["nes"].get("/api/nes/health")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("status") == "ok", body
    assert body.get("service") == "nes-api", body


def test_nes_entity_search_responds(clients):
    """Entity search returns a total-bearing ``entities`` array."""
    r = clients["nes"].get("/api/nes/entities", params={"query": "a", "limit": 1})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "total" in body
    assert isinstance(body.get("entities"), list)


def test_nes_entity_prefixes(clients):
    """The entity taxonomy endpoint the MCP/consumers depend on."""
    r = clients["nes"].get("/api/nes/entity_prefixes")
    assert r.status_code == 200, r.text
    body = r.json()
    assert isinstance(body.get("prefixes"), list)
