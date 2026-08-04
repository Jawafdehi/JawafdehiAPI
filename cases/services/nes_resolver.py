"""In-process resolution seam for NES entity display details.

NES (Nepal Entity Service) is the single source of truth for entities.
Jawafdehi stores only the canonical entity @id IRI
(``https://jawafdehi.org/entity/<prefix>/<slug>``) as a join key on the
``CaseEntityRelationship`` bind and in ``DocumentSource.related_entities``; it
never stores entity data (names/type).

This module is the *seam* that turns those IRIs into display details. NES keys
its ``StoredEntity`` rows by the same @id IRI (the ``iri`` PK), so resolution is
a direct ``iri``-lookup against the NES table. The service consolidation that would
let Jawafdehi share the NES database in-process is not done yet, so resolution is
best-effort:

* If the NES app models are importable in this process
  (``entities.models.StoredEntity`` — same physical ``entities``
  table NES owns), we read name/type directly from the stored schema.org
  JSON-LD document. No cross-DB foreign key is involved; this is an
  IRI -> document lookup (``StoredEntity.iri`` is the PK) against whatever DB
  the StoredEntity model is routed to.
* Otherwise we fall back to a documented stub that returns a minimal record
  (``{"nes_id": ...}`` with the other fields ``None``) for every requested id,
  so callers can render the id without crashing. Wiring this seam to the NES
  HTTP API (``settings.NES_API_URL``) is the planned follow-up.

The function is intentionally typed and total: it always returns one entry per
requested id, so callers can do ``resolve_entities(ids)[nes_id]`` safely.
"""

from __future__ import annotations

import logging
from typing import Optional, TypedDict

logger = logging.getLogger(__name__)


class ResolvedEntity(TypedDict):
    """Display details for a single NES entity, resolved from its id."""

    nes_id: str
    display_name: Optional[str]
    entity_type: Optional[str]


def _stub_entity(nes_id: str) -> ResolvedEntity:
    """Minimal record used when NES is not resolvable in-process."""
    return {"nes_id": nes_id, "display_name": None, "entity_type": None}


def _primary_name_from_document(data: dict) -> Optional[str]:
    """Extract a human-readable name from a stored NES schema.org JSON-LD document.

    The stored doc is schema.org JSON-LD: ``name`` is either a plain string or a
    language map ``{"en": "...", "ne": "..."}`` (the bilingual representation).
    Prefers English, then Nepali, then any non-empty language value.
    """
    if not isinstance(data, dict):
        return None
    name = data.get("name")
    if isinstance(name, str):
        return name.strip() or None
    if isinstance(name, dict):
        # Language map: prefer en, then ne, then any non-empty string value.
        for lang in ("en", "ne"):
            value = name.get(lang)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for value in name.values():
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def resolve_entities(nes_ids) -> dict[str, ResolvedEntity]:
    """Resolve canonical NES entity @id IRIs to display details.

    Args:
        nes_ids: An iterable of canonical NES entity @id IRI strings
            (``https://jawafdehi.org/entity/<prefix>/<slug>``). Duplicates and
            falsy values are ignored. NES keys its rows by the same IRI, so this
            is a direct ``StoredEntity.id``-IRI lookup.

    Returns:
        A dict mapping every requested (non-empty) id to a ``ResolvedEntity``.
        Ids that NES cannot resolve (or when NES is unavailable in-process) map
        to a stub record with ``display_name``/``entity_type`` set to ``None``.
    """
    ids = [nid for nid in dict.fromkeys(nes_ids) if nid]
    if not ids:
        return {}

    resolved: dict[str, ResolvedEntity] = {nid: _stub_entity(nid) for nid in ids}

    try:
        # NES app models live in the standalone NES service. The package may be
        # IMPORTABLE in this process (monorepo) yet the model is not usable until
        # the service consolidation adds `entities` to INSTALLED_APPS and
        # routes its DB — touching the class before then raises RuntimeError
        # ("doesn't declare an explicit app_label / isn't in INSTALLED_APPS").
        # Treat both "not importable" and "not registered" as "NES unavailable
        # in-process" → stubs. (Narrower than a blanket except: a genuine query
        # error below still surfaces via its own handler.)
        from django.apps import apps as _django_apps

        if not _django_apps.is_installed("entities"):
            raise ModuleNotFoundError("entities not in INSTALLED_APPS")
        from entities.models import StoredEntity
    except (ImportError, RuntimeError):  # pragma: no cover - stub fallback path
        logger.debug(
            "NES models not available in-process (not importable or not an "
            "installed app); returning stub entity records for %d id(s).",
            len(ids),
        )
        return resolved

    try:
        # StoredEntity is keyed by `iri` (the canonical @id), not `id`.
        for stored in StoredEntity.objects.filter(iri__in=ids):
            data = stored.data if isinstance(stored.data, dict) else {}
            resolved[stored.iri] = {
                "nes_id": stored.iri,
                "display_name": _primary_name_from_document(data),
                "entity_type": stored.entity_type or None,
            }
    except Exception:  # pragma: no cover  # noqa: BLE001 - defensive: DB not routed/migrated
        logger.warning(
            "Failed to resolve NES entities in-process; returning stubs.",
            exc_info=True,
        )

    return resolved


def build_entity_binds(
    relationships, resolved, *, include_notes: bool = False
) -> list[dict]:
    """Shape ``CaseEntityRelationship`` rows + resolved NES details into the entity
    binds used by BOTH the API (``CaseSerializer.get_entities``) and the search
    index card. One definition so the two consumers can't drift.

    ``resolved`` is a :func:`resolve_entities` result (``nes_id -> ResolvedEntity``);
    a missing/unresolved id yields ``None`` name/type rather than raising, so this
    is safe on the best-effort indexing path as well as the API path.

    Per-entity ``notes`` are internal casework content (BB-04): they must NOT reach
    public/anonymous callers. The ``notes`` key is always present for schema
    stability, but its value is ``""`` unless ``include_notes`` is set. The API
    passes ``include_notes`` only for casework-role viewers; the public search-card
    path leaves it ``False`` so the denormalized card never leaks internal notes."""
    return [
        {
            "nes_id": rel.nes_id,
            "display_name": (resolved.get(rel.nes_id) or {}).get("display_name"),
            "entity_type": (resolved.get(rel.nes_id) or {}).get("entity_type"),
            "type": rel.relationship_type,
            "outcome": rel.outcome,
            "notes": rel.notes if include_notes else "",
        }
        for rel in relationships
    ]
