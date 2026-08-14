"""A minimal in-memory stand-in for the OpenSearch indices/alias API.

A MagicMock is the wrong tool for the swap tests: every attribute it returns is
truthy, so a test would pass against code that resolved aliases incorrectly. This
fake instead enforces the ONE cluster rule the whole design turns on — a name
cannot be both a concrete index and an alias — so a bootstrap that forgets to
``remove_index`` fails here rather than in production.

Behaviour is modelled on what was verified against the live cluster on
2026-08-14 (see ``jawafdehi_shared.search.aliases``), not on the docs. In
particular ``exists`` answers True for an alias name, matching the live
``HEAD /<alias>`` -> 200.
"""

from __future__ import annotations

import fnmatch


class NotFoundError(Exception):
    """Stands in for opensearchpy.NotFoundError (status 404)."""

    status_code = 404


class AliasNameConflict(Exception):
    """An alias was pointed at a name that is still a concrete index."""


class Store:
    """The cluster state: ``indices`` maps name -> {doc id: doc}."""

    def __init__(self, indices=None, aliases=None):
        self.indices: dict[str, dict] = indices or {}
        self.aliases: dict[str, list[str]] = aliases or {}

    def resolve(self, name: str) -> str:
        """The concrete index a read/write against ``name`` lands on."""
        if name in self.aliases:
            targets = self.aliases[name]
            if len(targets) != 1:
                raise ValueError(f"{name} points at {len(targets)} indices")
            return targets[0]
        return name


class FakeIndices:
    """The ``client.indices`` namespace."""

    def __init__(self, store: Store):
        self.store = store
        # Names passed to create(), in order — lets a test assert that a rebuild
        # built a FRESH index (the only way a mapping change reaches the cluster)
        # rather than reusing one.
        self.created: list[str] = []

    def exists(self, index: str) -> bool:
        return index in self.store.indices or index in self.store.aliases

    def exists_alias(self, name: str) -> bool:
        return name in self.store.aliases

    def get_alias(self, name: str) -> dict:
        if name not in self.store.aliases:
            raise NotFoundError(name)
        return {i: {"aliases": {name: {}}} for i in self.store.aliases[name]}

    def get(self, index: str, ignore_unavailable: bool = False) -> dict:
        if "*" in index:
            return {
                name: {"settings": {}, "mappings": {}}
                for name in sorted(self.store.indices)
                if fnmatch.fnmatch(name, index)
            }
        if index not in self.store.indices:
            if ignore_unavailable:
                return {}
            raise NotFoundError(index)
        return {index: {"settings": {}, "mappings": {}}}

    def create(self, index: str, body: dict | None = None) -> dict:
        if index in self.store.aliases:
            raise AliasNameConflict(f"{index} is an alias, not an index")
        self.created.append(index)
        self.store.indices.setdefault(index, {})
        return {"acknowledged": True}

    def delete(self, index: str, ignore_unavailable: bool = False) -> dict:
        if index not in self.store.indices and not ignore_unavailable:
            raise NotFoundError(index)
        self.store.indices.pop(index, None)
        return {"acknowledged": True}

    def refresh(self, index: str | None = None) -> dict:
        return {"_shards": {"failed": 0}}

    def update_aliases(self, body: dict) -> dict:
        """Apply the action list: validate against a copy, then commit at once."""
        aliases = {k: list(v) for k, v in self.store.aliases.items()}
        indices = dict(self.store.indices)
        for action in body["actions"]:
            ((verb, spec),) = action.items()
            if verb == "remove":
                aliases.get(spec["alias"], []).remove(spec["index"])
                if not aliases[spec["alias"]]:
                    del aliases[spec["alias"]]
            elif verb == "remove_index":
                if spec["index"] not in indices:
                    raise NotFoundError(spec["index"])
                del indices[spec["index"]]
            elif verb == "add":
                if spec["alias"] in indices:
                    # The rule the bootstrap branch exists to satisfy.
                    raise AliasNameConflict(
                        f"{spec['alias']} is a concrete index; it cannot also "
                        f"be an alias"
                    )
                aliases.setdefault(spec["alias"], []).append(spec["index"])
            else:  # pragma: no cover — an action the driver never emits.
                raise ValueError(verb)
        self.store.aliases = aliases
        self.store.indices = indices
        return {"acknowledged": True}


class Client:
    """What the driver is handed: ``.indices`` is the API, ``.store`` the data."""

    def __init__(self, indices=None, aliases=None):
        self.store = Store(indices=indices, aliases=aliases)
        self.indices = FakeIndices(self.store)

    def count(self, index: str) -> dict:
        target = self.store.resolve(index)
        if target not in self.store.indices:
            raise NotFoundError(index)
        return {"count": len(self.store.indices[target])}

    def bulk_write(self, index: str, docs) -> int:
        """Test-side stand-in for ``stream_bulk`` (which the tests patch out)."""
        target = self.store.resolve(index)
        if target not in self.store.indices:
            raise NotFoundError(index)
        for doc in docs:
            self.store.indices[target][doc["iri"]] = doc
        return len(docs)

    def search_ids(self, index: str) -> list[str]:
        """The doc ids a search through ``index`` would return."""
        return sorted(self.store.indices[self.store.resolve(index)])
