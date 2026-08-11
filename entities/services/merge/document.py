"""Combine entity documents, and rewrite entity references inside one.

Merge policy (approved spec): the survivor wins every scalar conflict, fields the
survivor lacks are filled from the duplicates in order, and list-valued fields are
unioned with duplicates dropped.
"""

from __future__ import annotations

import copy
import json
from typing import Any, Dict, List, Tuple

#: Identity, context and provenance are the survivor's alone.
NEVER_INHERITED: frozenset = frozenset(
    {"@id", "@type", "@context", "dateCreated", "jawafdehi:version"}
)

#: The provenance block records history and must survive a reference rewrite intact.
_VERSION_KEY = "jawafdehi:version"


def _entry_key(value: Any) -> str:
    """A stable identity for a list entry, so a union can drop duplicates."""
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def _union(existing: List[Any], incoming: List[Any]) -> List[Any]:
    out = list(existing)
    seen = {_entry_key(v) for v in out}
    for value in incoming:
        key = _entry_key(value)
        if key not in seen:
            seen.add(key)
            out.append(value)
    return out


def merge_documents(
    survivor: Dict[str, Any], duplicates: List[Dict[str, Any]]
) -> Tuple[Dict[str, Any], Dict[str, str]]:
    """Merge duplicates into the survivor. Returns (document, inherited field → source IRI)."""
    merged = copy.deepcopy(survivor)
    inherited: Dict[str, str] = {}
    same_as: List[Any] = list(merged.get("sameAs") or [])

    for dup in duplicates:
        dup_iri = dup.get("@id")
        for field, value in dup.items():
            if field in NEVER_INHERITED or field == "sameAs":
                continue
            if field not in merged or merged[field] in (None, "", [], {}):
                merged[field] = copy.deepcopy(value)
                if dup_iri is not None:
                    inherited[field] = dup_iri
            elif isinstance(merged[field], list) and isinstance(value, list):
                merged[field] = _union(merged[field], value)
        same_as = _union(same_as, list(dup.get("sameAs") or []))
        if dup_iri:
            same_as = _union(same_as, [dup_iri])

    if same_as:
        merged["sameAs"] = same_as
    return merged, inherited


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
            walked = [walk(v, top=False) for v in node]
            seen, deduped = set(), []
            for value in walked:
                key = _entry_key(value)
                if key not in seen:
                    seen.add(key)
                    deduped.append(value)
            return deduped
        return node

    return walk(doc, top=True), count
