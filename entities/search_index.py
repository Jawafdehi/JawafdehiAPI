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
* ``case_count``     ← PUBLISHED Jawafdehi cases citing this entity (the public
  visibility gate — see ``entities.search_visibility``; 0 means anonymous
  callers do not see it, but the doc is still indexed so the caseworker entity
  picker can find it),
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


def build_doc(obj: Any, *, case_count: int | None = None) -> dict[str, Any]:
    """Map a ``StoredEntity`` (or any object with ``.iri``/``.data``) to the
    common index doc. No OpenSearch calls.

    ``case_count`` is the number of PUBLISHED Jawafdehi cases citing this entity
    — the public-search visibility gate (see ``entities.search_visibility``).
    Pass it when you already have the value: the bulk reindex loads ONE grouped
    map for the whole corpus, because a per-document query over 187k entities is
    not viable. When it is ``None`` this resolves it for the single entity, which
    is the live ``post_save`` path.

    It must always be resolved, never defaulted to 0: writing 0 for an entity
    that IS cited would archive it from public search until the next reconcile.
    """
    data: dict[str, Any] = getattr(obj, "data", None) or {}
    iri = getattr(obj, "iri", None) or data.get("@id")
    if case_count is None:
        # Lazy import: search_visibility reaches into the cases app, and this
        # module is imported by the DB-less shaping tests.
        from entities.search_visibility import entity_case_count

        case_count = entity_case_count(iri)

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
        "case_count": case_count,
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
def index(obj: Any, *, client=None, case_count: int | None = None) -> None:
    """Upsert the entity's doc into ``nes-entities`` (best-effort).

    ``case_count`` is forwarded to :func:`build_doc`; callers that already know
    it (``cases.signals`` after a bind write, the reconcile command) pass it to
    skip the per-document lookup.
    """
    upsert_doc(
        client or make_client(), ENTITY_INDEX, build_doc(obj, case_count=case_count)
    )


@best_effort("index entity by iri")
def index_by_iri(iri: str, *, client=None, case_count: int | None = None) -> None:
    """Re-index the entity ``iri`` from the store (best-effort, no-op if absent).

    The entry point for CROSS-APP triggers: a case bind write changes an
    entity's ``case_count`` without touching the ``StoredEntity`` row, so no
    entity signal fires and ``cases.signals`` has to ask for the re-index by IRI.

    A soft-deleted or missing row is EVICTED rather than indexed, matching the
    ``post_save`` rule in ``entities.signals`` — otherwise a bind pointing at a
    deleted entity would resurrect it in public search.
    """
    from entities.models import StoredEntity

    entity = StoredEntity.objects.filter(iri=iri).first()
    resolved = client or make_client()
    if entity is None or entity.is_deleted:
        delete_doc(resolved, ENTITY_INDEX, iri)
        return
    upsert_doc(resolved, ENTITY_INDEX, build_doc(entity, case_count=case_count))


@best_effort("delete entity")
def delete(obj: Any, *, client=None) -> None:
    """Delete the entity's doc from ``nes-entities`` (best-effort)."""
    iri = getattr(obj, "iri", None) or (getattr(obj, "data", None) or {}).get("@id")
    if iri:
        delete_doc(client or make_client(), ENTITY_INDEX, iri)


@best_effort("delete entity by iri")
def delete_by_iri(iri: str, *, client=None) -> None:
    """Delete the doc for ``iri`` (best-effort), with no store row needed.

    For callers holding only an IRI whose entity is gone from the store — the
    reconcile command evicting a document whose ``StoredEntity`` no longer
    exists, where :func:`delete` has no object to read the IRI off.
    """
    if iri:
        delete_doc(client or make_client(), ENTITY_INDEX, iri)
