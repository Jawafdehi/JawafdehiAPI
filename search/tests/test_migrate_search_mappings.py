"""``migrate_search_mappings`` against an ALIASED index.

Every real search index is an alias over a generation (``ngm-materials`` ->
``ngm-materials-000003``), and ``get_mapping(index=<alias>)`` answers keyed by the
CONCRETE index. The first version of this command keyed the response on the alias,
read ``{}``, and so reported every declared field as missing — including straight
after a PUT that had actually committed. It aborted in production on 2026-10-05
with "dataset_bucket is None" while the mapping was already correct.

These pin the resolution, and the zero-properties guard that would have caught it.
"""

from __future__ import annotations

from io import StringIO
from unittest.mock import MagicMock

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

ALIAS = "ngm-materials"
CONCRETE = "ngm-materials-000003"


def _client(*, mapped_under: str, properties: dict | None = None):
    """A fake OpenSearch whose get_mapping answers keyed by ``mapped_under``."""
    props = {"iri": {"type": "keyword"}, "title_ne": {"type": "text"}}
    if properties:
        props = {**props, **properties}
    client = MagicMock()
    client.indices.exists.return_value = True
    client.indices.get_mapping.return_value = {
        mapped_under: {"mappings": {"properties": props}}
    }
    client.indices.get_alias.return_value = {CONCRETE: {"aliases": {ALIAS: {}}}}
    # A real dict, not a MagicMock: the command json.dumps() this.
    client.indices.put_mapping.return_value = {"acknowledged": True}
    return client


@pytest.fixture
def patched(monkeypatch):
    def _apply(client, *, concrete: str | None = CONCRETE):
        monkeypatch.setattr(
            "search.management.commands.migrate_search_mappings.make_client",
            lambda: client,
        )
        monkeypatch.setattr(
            "search.management.commands.migrate_search_mappings.resolve_alias",
            lambda _client, _alias: concrete,
        )
        # Keep the declared set small and predictable.
        monkeypatch.setattr(
            "search.management.commands.migrate_search_mappings.common_mappings",
            lambda: {
                "properties": {
                    "iri": {"type": "keyword"},
                    "title_ne": {"type": "text"},
                    "dataset_bucket": {"type": "keyword"},
                }
            },
        )
        return client

    return _apply


def test_reads_the_mapping_off_the_CONCRETE_index_not_the_alias(patched):
    """The regression. The response is keyed by the generation, so resolving the
    alias is what makes the before/after reads non-empty."""
    client = patched(_client(mapped_under=CONCRETE))
    out = StringIO()

    call_command("migrate_search_mappings", "--index", ALIAS, "--dry-run", stdout=out)

    # It asked about the generation, never about the bare alias.
    assert client.indices.get_mapping.call_args.kwargs["index"] == CONCRETE
    text = out.getvalue()
    assert f"{ALIAS} -> {CONCRETE}" in text
    assert "2 live properties" in text
    assert "to add: dataset_bucket" in text


def test_verification_passes_once_the_put_has_landed(patched):
    """After the PUT the re-read must SEE the new field. Keyed on the alias this
    read {} and the command raised despite having succeeded."""
    client = patched(
        _client(
            mapped_under=CONCRETE,
            properties={"dataset_bucket": {"type": "keyword"}},
        )
    )
    out = StringIO()

    call_command("migrate_search_mappings", "--index", ALIAS, stdout=out)

    client.indices.put_mapping.assert_called_once()
    assert "all declared fields present" in out.getvalue()


def test_zero_live_properties_is_refused(patched):
    """The guard that would have caught the original bug on its own.

    No index is ever created without common_mappings(), so an empty property set
    means the lookup was wrong — not that the index is empty. Failing here beats
    "adding" every field and reporting success.
    """
    client = MagicMock()
    client.indices.exists.return_value = True
    # Mapping present, but filed under a name the command did not ask about —
    # exactly what an unresolved alias looks like.
    client.indices.get_mapping.return_value = {
        "some-other-index": {"mappings": {"properties": {"iri": {"type": "keyword"}}}}
    }
    patched(client)

    with pytest.raises(CommandError, match="ZERO live properties"):
        call_command("migrate_search_mappings", "--index", ALIAS, "--dry-run")

    client.indices.put_mapping.assert_not_called()


def test_falls_back_to_the_bare_name_when_it_is_not_an_alias(patched):
    """A concrete index that is nobody's alias still has to work."""
    client = patched(_client(mapped_under=ALIAS), concrete=None)
    out = StringIO()

    call_command("migrate_search_mappings", "--index", ALIAS, "--dry-run", stdout=out)

    assert client.indices.get_mapping.call_args.kwargs["index"] == ALIAS
    # No "alias -> generation" arrow when there is no alias to resolve.
    assert f"{ALIAS}:" in out.getvalue()


def test_an_unknown_index_name_is_rejected_before_any_call(patched):
    client = patched(_client(mapped_under=CONCRETE))

    with pytest.raises(CommandError, match="unknown index"):
        call_command("migrate_search_mappings", "--index", "not-an-index")

    client.indices.get_mapping.assert_not_called()
