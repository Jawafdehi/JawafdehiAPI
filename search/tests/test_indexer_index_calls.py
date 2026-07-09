"""index()/delete() behavior tests for the indexers, with a MOCK OpenSearch client.

No live cluster: a ``unittest.mock.MagicMock`` stands in for the opensearch-py
client. Asserts the upsert/delete calls and the two contract behaviors that live
in ``index()``:

* best-effort — an exception from the client is logged + swallowed (never raised
  into the caller),
* case-only-published — ``cases.search_index.index`` upserts a PUBLISHED case
  and DELETES a non-published one.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from cases import search_index as case_index
from entities import search_index as entity_index


def _entity():
    iri = "https://jawafdehi.org/entity/person/x"
    return SimpleNamespace(iri=iri, data={"@id": iri, "@type": "Person", "name": "X"})


def test_entity_index_upserts_by_iri():
    client = MagicMock()
    entity_index.index(_entity(), client=client)
    client.index.assert_called_once()
    _, kwargs = client.index.call_args
    assert kwargs["index"] == "nes-entities"
    assert kwargs["id"] == "https://jawafdehi.org/entity/person/x"
    assert kwargs["body"]["iri"] == "https://jawafdehi.org/entity/person/x"


def test_entity_index_is_best_effort_on_client_error():
    client = MagicMock()
    client.index.side_effect = RuntimeError("cluster down")
    # Must NOT raise — best-effort by contract.
    assert entity_index.index(_entity(), client=client) is None


def test_entity_delete_calls_delete_by_iri():
    client = MagicMock()
    entity_index.delete(_entity(), client=client)
    client.delete.assert_called_once()
    _, kwargs = client.delete.call_args
    assert kwargs["index"] == "nes-entities"
    assert kwargs["id"] == "https://jawafdehi.org/entity/person/x"


def _case(state):
    return SimpleNamespace(
        state=state,
        public_iri=(
            "https://jawafdehi.org/case/x" if state == "PUBLISHED" else None
        ),
        slug="x",
        title="X",
        description="d",
        short_description="",
        key_allegations=[],
        tags=[],
        case_type="CORRUPTION",
        court_cases=[],
        case_start_date=None,
        created_at=None,
        updated_at=None,
    )


def test_case_index_published_upserts():
    client = MagicMock()
    case_index.index(_case("PUBLISHED"), client=client)
    client.index.assert_called_once()
    _, kwargs = client.index.call_args
    assert kwargs["index"] == "jawafdehi-cases"
    assert kwargs["id"] == "https://jawafdehi.org/case/x"
    client.delete.assert_not_called()


def test_case_index_non_published_deletes():
    # A case that is not (or no longer) PUBLISHED must be evicted from the index.
    for state in ("DRAFT", "IN_REVIEW", "CLOSED"):
        client = MagicMock()
        case_index.index(_case(state), client=client)
        client.index.assert_not_called()
        client.delete.assert_called_once()
        _, kwargs = client.delete.call_args
        # Falls back to building the IRI from the slug since public_iri is None.
        assert kwargs["id"] == "https://jawafdehi.org/case/x"


def test_case_index_resolves_and_denormalizes_entities(monkeypatch):
    """index() resolves NES display names at index time and stores them in the card."""
    rel = SimpleNamespace(
        nes_id="https://jawafdehi.org/entity/person/x",
        relationship_type="accused",
        outcome="convicted",
        notes="",
    )
    case = _case("PUBLISHED")
    case.entity_relationships = SimpleNamespace(all=lambda: [rel])

    monkeypatch.setattr(
        "cases.services.nes_resolver.resolve_entities",
        lambda ids: {rel.nes_id: {"display_name": "Person X", "entity_type": "Person"}},
    )

    client = MagicMock()
    case_index.index(case, client=client)
    _, kwargs = client.index.call_args
    entities = kwargs["body"]["raw"]["card"]["entities"]
    assert entities == [
        {
            "nes_id": rel.nes_id,
            "display_name": "Person X",
            "entity_type": "Person",
            "type": "accused",
            "outcome": "convicted",
            "notes": "",
        }
    ]


def test_case_index_survives_entity_resolution_failure(monkeypatch):
    """A NES resolution error must not stop the case from being indexed."""
    rel = SimpleNamespace(
        nes_id="https://jawafdehi.org/entity/person/x",
        relationship_type="accused",
        outcome="charged",
        notes="",
    )
    case = _case("PUBLISHED")
    case.entity_relationships = SimpleNamespace(all=lambda: [rel])

    def _boom(ids):
        raise RuntimeError("NES down")

    monkeypatch.setattr("cases.services.nes_resolver.resolve_entities", _boom)

    client = MagicMock()
    case_index.index(case, client=client)
    client.index.assert_called_once()
    _, kwargs = client.index.call_args
    # Still indexed, with the bind present but names left null for the reconcile.
    entity = kwargs["body"]["raw"]["card"]["entities"][0]
    assert entity["display_name"] is None
    assert entity["type"] == "accused"


def test_case_index_survives_corrupt_relationship_row(monkeypatch):
    """A relationship row missing expected attrs must not crash the best-effort
    indexer — the case is still indexed, with empty entities."""
    corrupt = SimpleNamespace()  # no nes_id / relationship_type
    case = _case("PUBLISHED")
    case.entity_relationships = SimpleNamespace(all=lambda: [corrupt])
    monkeypatch.setattr(
        "cases.services.nes_resolver.resolve_entities", lambda ids: {}
    )

    client = MagicMock()
    case_index.index(case, client=client)
    client.index.assert_called_once()
    _, kwargs = client.index.call_args
    assert kwargs["body"]["raw"]["card"]["entities"] == []
