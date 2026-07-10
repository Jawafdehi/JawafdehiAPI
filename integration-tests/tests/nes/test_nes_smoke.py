"""Entities API contract smoke tests — core entity-service functionality.

Unified surface (HARD CUT 2026-07-01): entities live at ``/api/entities`` on the
single platform host — the old ``/api/nes/`` prefix is GONE. Verified live
against the running monolith (:48000):
  * health is ``GET /api/health`` (NO trailing slash — registered slashless; the
    ``/api/health/`` variant 404s) -> ``{"status": "ok", "service": "jawafdehi-api"}``;
  * search param is ``query`` (``q`` is silently ignored), not ``q``;
  * list shape is ``{"entities": [...], "total", "limit", "offset"}``
    (the entity id field is the schema.org ``@id`` IRI), not ``{"results": [...]}``;
  * prefixes come back as ``{"prefixes": [...]}``.
"""

import pytest

pytestmark = [pytest.mark.smoke, pytest.mark.live]


def test_nes_health(clients):
    r = clients["nes"].get("/api/health")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("status") == "ok", body
    assert body.get("service") == "jawafdehi-api", body


def test_nes_entity_search_responds(clients):
    """Entity search returns a total-bearing ``entities`` array."""
    r = clients["nes"].get("/api/entities", params={"query": "a", "limit": 1})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "total" in body
    assert isinstance(body.get("entities"), list)


def test_nes_entity_prefixes(clients):
    """The entity taxonomy endpoint the MCP/consumers depend on."""
    r = clients["nes"].get("/api/entity_prefixes")
    assert r.status_code == 200, r.text
    body = r.json()
    assert isinstance(body.get("prefixes"), list)
