"""Rewrite entity references inside a stored document.

The merge never copies fields between documents. A survivor that should carry a
duplicate's description is PATCHed before the merge runs, as its own edit with its own
author. Everything here moves references, nothing merges content.
"""

from __future__ import annotations

import json
from typing import Any, Dict, FrozenSet, Iterable, Tuple

#: Identity and provenance: never rewritten, never stripped from a document's own top level.
IDENTITY_KEYS: frozenset = frozenset(
    {"@id", "@type", "@context", "dateCreated", "jawafdehi:version"}
)

#: The provenance block records history and must survive a reference rewrite intact.
_VERSION_KEY = "jawafdehi:version"


def _entry_key(value: Any) -> str:
    """A stable identity for a list entry, so a rewrite can drop the duplicates it creates."""
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def drop_self_references(
    doc: Dict[str, Any], retired: Iterable[str], *, keep: FrozenSet[str] = frozenset()
) -> Tuple[Dict[str, Any], int]:
    """Remove references to a retired IRI from the survivor's own document.

    Repointing them at the survivor instead would assert a relation to itself, so they
    are dropped rather than rewritten. This is the only reason a merge writes the
    survivor's document at all — if it referenced nothing retired, it is left untouched.
    """
    retired = set(retired)
    count = 0

    def is_retired(value: Any) -> bool:
        if isinstance(value, dict):
            return value.get("@id") in retired
        return isinstance(value, str) and value in retired

    def walk(node: Any, *, top: bool = False) -> Any:
        nonlocal count
        if isinstance(node, dict):
            out: Dict[str, Any] = {}
            for key, value in node.items():
                if (top and key in IDENTITY_KEYS) or key in keep:
                    out[key] = value
                elif is_retired(value):
                    count += 1
                elif isinstance(value, list):
                    kept = [v for v in value if not is_retired(v)]
                    removed = len(value) - len(kept)
                    count += removed
                    # An already-empty list stays. Only entries this merge removed
                    # may make a key disappear.
                    if kept or not removed:
                        out[key] = [walk(v) for v in kept]
                else:
                    walked = walk(value)
                    # A nested object whose only content was a retired reference
                    # leaves an empty husk; drop it rather than storing {}.
                    if isinstance(value, dict) and walked == {} and value != {}:
                        continue
                    out[key] = walked
            return out
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    return walk(doc, top=True), count


def rewrite_references(
    doc: Dict[str, Any], mapping: Dict[str, str]
) -> Tuple[Dict[str, Any], int]:
    """Repoint every ``{"@id": retired}`` in ``doc`` to its survivor, at any depth.

    The document's own top-level ``@id`` and the ``jawafdehi:version`` provenance
    block are left alone: one is identity, the other is history.
    """
    count = 0

    def walk(node: Any, *, top: bool) -> Any:
        nonlocal count
        if isinstance(node, dict):
            out: Dict[str, Any] = {}
            for key, value in node.items():
                if top and key in ("@id", _VERSION_KEY):
                    out[key] = value
                    continue
                if key == "@id" and isinstance(value, str) and value in mapping:
                    out[key] = mapping[value]
                    count += 1
                    continue
                out[key] = walk(value, top=False)
            return out
        if isinstance(node, list):
            count_before = count
            walked = [walk(v, top=False) for v in node]
            if count > count_before:  # Only deduplicate if a rewrite happened in this list
                seen, deduped = set(), []
                for value in walked:
                    key = _entry_key(value)
                    if key not in seen:
                        seen.add(key)
                        deduped.append(value)
                return deduped
            return walked
        return node

    return walk(doc, top=True), count
