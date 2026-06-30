"""The entity-id join contract: the canonical schema.org ``@id`` IRI.

This is the single key that threads NES (canonical owner), NGM
(``court_case_entities.nes_id``), and Jawafdehi (``JawafEntity.nes_id``)
together. We assert the *shape* against any data present today, and xfail the
actual NGM->NES resolution join until ``nes_id`` population lands.

Clean-slate IRI contract (``shared/jawafdehi_shared/entities/ids.py``):
  * entity @id: ``https://jawafdehi.org/entity/<prefix>/<slug>``
  * prefix: 1..4 slash-joined ``[a-z0-9_]`` segments; slug: ``[a-z0-9][a-z0-9-]*``.
  * There is NO legacy ``entity:<prefix>/<slug>`` form.
"""

import re

import pytest

from fixtures.sample_data import (
    SAMPLE_ENTITY_IRI,
    SAMPLE_NES_ENTITY,
    SAMPLE_NGM_PARTY,
)

pytestmark = [pytest.mark.cross_service]

# Mirrors ids.ENTITY_IRI_RE.
ENTITY_IRI_RE = re.compile(
    r"^https?://[^/]+/entity/[a-z0-9_]+(?:/[a-z0-9_]+){0,3}/[a-z0-9][a-z0-9-]*$"
)


def assert_entity_iri_shape(iri: str) -> None:
    """The canonical-IRI shape contract — usable on NES @ids and NGM nes_ids alike."""
    assert isinstance(iri, str), f"not a string: {iri!r}"
    assert ENTITY_IRI_RE.match(iri), f"id fails canonical entity-IRI grammar: {iri!r}"


def _entity_id(entity: dict) -> str:
    return entity.get("@id") or entity.get("id") or ""


# --- Pure contract checks on the canonical sample data (no stack needed) ------


def test_sample_entity_id_obeys_shape():
    """Fixture sanity: our canonical IRI is itself contract-valid."""
    assert_entity_iri_shape(SAMPLE_ENTITY_IRI)
    assert SAMPLE_NES_ENTITY["@id"] == SAMPLE_ENTITY_IRI


def test_sample_ngm_party_nes_id_matches_nes_entity():
    """An NGM party's ``nes_id`` is exactly an NES entity @id IRI — same string, shape."""
    nes_id = SAMPLE_NGM_PARTY["nes_id"]
    assert_entity_iri_shape(nes_id)
    assert nes_id == SAMPLE_NES_ENTITY["@id"]


# --- Live shape assertions on whatever real data is present -------------------


@pytest.mark.live
def test_live_nes_entity_id_shape(clients):
    """Any entity NES returns must satisfy the @id IRI shape contract."""
    r = clients["nes"].get("/api/nes/entities", params={"query": "a", "limit": 1})
    if r.status_code != 200:
        pytest.skip("NES entities endpoint not ready")
    entities = r.json().get("entities") or []
    if not entities:
        pytest.skip("PENDING-DATA: no entities seeded yet")
    assert_entity_iri_shape(_entity_id(entities[0]))


@pytest.mark.live
@pytest.mark.xfail(
    reason="PENDING-DATA: NGM court tables empty -> no party nes_id populated yet. "
    "Paths/shape are correct against the monolith read plane (/api/ngm/cases/ -> "
    "DRF results); flips green when court data + nes_id write-back land.",
    strict=False,
)
def test_live_ngm_party_nes_id_resolves_and_matches_shape(clients):
    """A resolved NGM court party carries a contract-valid NES @id IRI.

    Asserts the SHAPE on any populated ``nes_id`` today; xfail because the court
    tables are empty, so parties realistically still carry ``nes_id is None``.
    """
    cases = clients["ngm"].get("/api/ngm/cases/", params={"limit": 1})
    cases.raise_for_status()
    items = cases.json().get("results", [])
    assert items, "no court cases available"
    case = items[0]
    parties = clients["ngm"].get(
        f"/api/ngm/cases/{case['court_identifier']}/{case['case_number']}/entities/"
    )
    parties.raise_for_status()
    resolved = [p for p in parties.json().get("results", []) if p.get("nes_id")]
    assert resolved, "no parties have a resolved nes_id yet"
    for p in resolved:
        assert_entity_iri_shape(p["nes_id"])
