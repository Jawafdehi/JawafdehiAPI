"""Unified-search indexer for NES entities (``nes-entities`` index).

Projects a ``StoredEntity`` (a schema.org JSON-LD document keyed by its ``@id``
IRI) into the common index doc (see ``jawafdehi_shared.search.mappings``):

* ``iri``            ← ``@id`` (the document ``_id`` too),
* ``type``           ← ``@type`` (a list @type is comma-joined, like the
  promoted ``entity_type`` column),
* ``title_ne/en``    ← the ``name`` language map (or a script-bucketed string),
* ``title_translit`` ← the shared transliteration of the titles,
* ``body``           ← ``description`` / ``alternateName`` (bilingual-friendly),
* ``keywords``       ← schema.org ``keywords``,
* ``identifiers``    ← the IRI + alternate identifiers,
* ``raw``            ← the full JSON-LD (return-only).

Every public entry point is best-effort: an OpenSearch error is logged and
swallowed (the DB is the source of truth — see the unified-search plan §4.1).
"""

from __future__ import annotations

from typing import Any

from jawafdehi_shared.search.indexing import (
    best_effort,
    delete_doc,
    flatten_strings,
    name_to_titles,
    title_translit,
    type_token,
    upsert_doc,
)
from jawafdehi_shared.search.opensearch import ENTITY_INDEX, make_client

SOURCE_APP = "nes"


def build_doc(obj: Any) -> dict[str, Any]:
    """Map a ``StoredEntity`` (or any object with ``.iri``/``.data``) to the
    common index doc. Pure: no OpenSearch calls."""
    data: dict[str, Any] = getattr(obj, "data", None) or {}
    iri = getattr(obj, "iri", None) or data.get("@id")

    title_ne, title_en = name_to_titles(data.get("name"))

    # body: description + alternate names, both languages flattened.
    body_parts = flatten_strings(data.get("description"))
    body_parts += flatten_strings(data.get("alternateName"))
    body = " ".join(body_parts) or None

    keywords = [k for k in (data.get("keywords") or []) if isinstance(k, str)]

    identifiers: list[str] = [iri] if iri else []
    for ident in flatten_strings(data.get("identifier")):
        if ident not in identifiers:
            identifiers.append(ident)

    doc: dict[str, Any] = {
        "iri": iri,
        "type": type_token(data.get("@type")),
        "source_app": SOURCE_APP,
        "title_ne": title_ne,
        "title_en": title_en,
        "title_translit": title_translit(title_ne, title_en),
        "body": body,
        "keywords": keywords,
        "identifiers": identifiers,
        "raw": data,
    }
    created = getattr(obj, "created_at", None)
    updated = getattr(obj, "updated_at", None)
    if created is not None:
        doc["created_at"] = created.isoformat() if hasattr(created, "isoformat") else created
    if updated is not None:
        doc["updated_at"] = updated.isoformat() if hasattr(updated, "isoformat") else updated
    return doc


@best_effort("index entity")
def index(obj: Any, *, client=None) -> None:
    """Upsert the entity's doc into ``nes-entities`` (best-effort)."""
    upsert_doc(client or make_client(), ENTITY_INDEX, build_doc(obj))


@best_effort("delete entity")
def delete(obj: Any, *, client=None) -> None:
    """Delete the entity's doc from ``nes-entities`` (best-effort)."""
    iri = getattr(obj, "iri", None) or (getattr(obj, "data", None) or {}).get("@id")
    if iri:
        delete_doc(client or make_client(), ENTITY_INDEX, iri)
