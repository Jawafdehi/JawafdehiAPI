"""Tests for the shared OpenSearch client helpers and index constants.

The client is mocked — no live OpenSearch. Asserts the four canonical index
names and that create_index/ensure_indices drive the mock correctly (idempotent).
"""

from unittest.mock import MagicMock

from jawafdehi_shared.search import opensearch


def test_four_index_constants():
    assert opensearch.ENTITY_INDEX == "nes-entities"
    assert opensearch.MATERIAL_INDEX == "ngm-materials"
    assert opensearch.COURTCASE_INDEX == "ngm-courtcases"
    assert opensearch.CASE_INDEX == "jawafdehi-cases"
    assert opensearch.ALL_INDICES == (
        "nes-entities",
        "ngm-materials",
        "ngm-courtcases",
        "jawafdehi-cases",
    )


def test_document_index_removed():
    # The old ngm-documents constant must be gone (reconciled to materials/courtcases).
    assert not hasattr(opensearch, "DOCUMENT_INDEX")


def test_create_index_creates_when_absent():
    client = MagicMock()
    client.indices.exists.return_value = False
    created = opensearch.create_index(client, "nes-entities")
    assert created is True
    client.indices.create.assert_called_once()
    _, kwargs = client.indices.create.call_args
    assert kwargs["index"] == "nes-entities"
    body = kwargs["body"]
    # Bilingual settings + common mappings wired in by default.
    assert "devanagari" in body["settings"]["analysis"]["analyzer"]
    assert "iri" in body["mappings"]["properties"]


def test_create_index_skips_when_present():
    client = MagicMock()
    client.indices.exists.return_value = True
    created = opensearch.create_index(client, "nes-entities")
    assert created is False
    client.indices.create.assert_not_called()


def test_ensure_indices_builds_the_four_names():
    client = MagicMock()
    client.indices.exists.return_value = False
    created = opensearch.ensure_indices(client)
    assert created == list(opensearch.ALL_INDICES)
    built = {c.kwargs["index"] for c in client.indices.create.call_args_list}
    assert built == set(opensearch.ALL_INDICES)


def test_ensure_indices_idempotent_when_all_exist():
    client = MagicMock()
    client.indices.exists.return_value = True
    assert opensearch.ensure_indices(client) == []
    client.indices.create.assert_not_called()


def test_make_client_uses_basic_auth_when_creds_set(monkeypatch):
    import sys
    import types

    captured = {}

    class FakeOpenSearch:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    fake_mod = types.ModuleType("opensearchpy")
    fake_mod.OpenSearch = FakeOpenSearch
    monkeypatch.setitem(sys.modules, "opensearchpy", fake_mod)
    monkeypatch.setenv("OPENSEARCH_URL", "https://os.example:9200")
    monkeypatch.setenv("OPENSEARCH_USER", "admin")
    monkeypatch.setenv("OPENSEARCH_PASSWORD", "secret")

    opensearch.make_client()
    assert captured["hosts"] == ["https://os.example:9200"]
    assert captured["http_auth"] == ("admin", "secret")


def test_make_client_anonymous_without_creds(monkeypatch):
    import sys
    import types

    captured = {}

    class FakeOpenSearch:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    fake_mod = types.ModuleType("opensearchpy")
    fake_mod.OpenSearch = FakeOpenSearch
    monkeypatch.setitem(sys.modules, "opensearchpy", fake_mod)
    monkeypatch.delenv("OPENSEARCH_USER", raising=False)
    monkeypatch.delenv("OPENSEARCH_PASSWORD", raising=False)

    opensearch.make_client("http://localhost:9200")
    assert "http_auth" not in captured
