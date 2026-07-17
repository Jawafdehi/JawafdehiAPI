"""Write helper: turn a Jawafdehi case "source" into an NGM Material + evidence bind.

Replaces the removed ``DocumentSource``-creating helpers (ADR: cases own no
documents). The ingest paths (CIAA draft-case service, case importer, seeders)
used to create a ``DocumentSource`` row + append a ``{source_id, description}``
evidence entry. They now:

  1. shape the source data into Material JSON-LD (``documentsource_to_jsonld``),
  2. upsert ONE Material with the ≥2-publisher gate bypassed
     (``upsert_single_source_material`` — a cited case document is inherently
     single-source, same as a court order), and
  3. bind it to the case as a ``CaseMaterialReference`` (material_iri +
     additional_details), deduped by ``(case, material_iri)``.

The Material @id is derived from the source's stable ``source_id`` via
``build_source_material_iri``, sourced by the document's ``material_type``
(``/material/<material_type>/<ident>`` — news → ``news``, a misc upload →
``document``, …) rather than the legacy monolithic ``/material/jawafdehi/``
bucket, so an upload's source reads as its kind and it is born ``PUBLIC``.
Re-ingesting the same source is idempotent, and callers that lack a source_id
can pass any stable natural key (e.g. a hashed URL) as ``source_id``.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any, Iterable

from materials.jsonld import documentsource_to_jsonld
from materials.single_source_ingest import upsert_single_source_material

logger = logging.getLogger(__name__)


def _normalize_url_list(url: Any) -> list[dict[str, str]]:
    """Coerce a source ``url`` value to the roled-link list the shaper expects.

    Accepts a bare string, a list of strings, or a list of ``{link, role}`` dicts
    (legacy shapes the old ingesters produced). A bare string / roleless entry
    defaults to RAW — the same coercion the old ``DocumentSource.normalize_url_list``
    applied. Blank links are dropped.
    """
    if url is None:
        return []
    if isinstance(url, str):
        url = [url] if url.strip() else []
    if not isinstance(url, list):
        return []
    out: list[dict[str, str]] = []
    for item in url:
        if isinstance(item, str):
            link = item.strip()
            if link:
                out.append({"link": link, "role": "RAW"})
        elif isinstance(item, dict):
            link = (item.get("link") or "").strip()
            if link:
                out.append({"link": link, "role": item.get("role") or "RAW"})
    return out


def _stable_source_id(*, source_id: str | None, title: str, links: list[dict]) -> str:
    """A stable natural key for the Material @id ident.

    Prefers an explicit ``source_id``; otherwise derives a deterministic one from
    the first link (or the title) so re-ingesting the same document upserts the
    same Material rather than duplicating it.
    """
    if source_id and source_id.strip():
        return source_id.strip()
    basis = (links[0]["link"] if links else "") or title
    digest = hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]
    return f"source:jawaf:{digest}"


def upsert_source_material(
    *,
    title: str,
    url: Any = None,
    source_type: str | None = None,
    source_id: str | None = None,
    description: str = "",
    related_entities: Iterable[str] | None = None,
    publication_date: Any = None,
) -> str | None:
    """Upsert an NGM Material from case-source data; return its ``@id`` IRI.

    Returns ``None`` when there is no usable title (mirrors the old
    ``get_or_create_source`` contract, which skipped title-less sources). The
    upsert is idempotent by ``@id`` (derived from ``source_id`` / a hash).
    """
    title = (title or "").strip()
    if not title:
        return None

    links = _normalize_url_list(url)
    sid = _stable_source_id(source_id=source_id, title=title, links=links)

    doc, material_type = documentsource_to_jsonld(
        source_id=sid,
        title=title,
        source_type=source_type,
        url=links,
        description=description or "",
        related_entities=list(related_entities or []),
        publication_date=publication_date,
    )
    material = upsert_single_source_material(doc, material_type=material_type)
    return material.iri


def bind_material_to_case(
    case,
    material_iri: str,
    *,
    additional_details: str = "",
    ordinal: int | None = None,
) -> None:
    """Create/update the ``CaseMaterialReference`` bind for a material on a case.

    Idempotent by ``(case, material_iri)``. On CREATE, ``ordinal`` defaults to the
    count of existing references (append order). On UPDATE, the existing
    ``ordinal`` is PRESERVED unless an explicit ``ordinal`` is passed — re-binding
    a material must not silently reshuffle the evidence display order.
    """
    from cases.models import CaseMaterialReference

    obj, created = CaseMaterialReference.objects.get_or_create(
        case=case,
        material_iri=material_iri,
        defaults={
            "additional_details": additional_details or "",
            "ordinal": (
                ordinal if ordinal is not None else case.material_references.count()
            ),
        },
    )
    if not created:
        obj.additional_details = additional_details or ""
        update_fields = ["additional_details"]
        if ordinal is not None:
            obj.ordinal = ordinal
            update_fields.append("ordinal")
        obj.save(update_fields=update_fields)


def ingest_source_as_evidence(
    case,
    *,
    title: str,
    url: Any = None,
    source_type: str | None = None,
    source_id: str | None = None,
    description: str = "",
    additional_details: str = "",
    related_entities: Iterable[str] | None = None,
    publication_date: Any = None,
    ordinal: int | None = None,
) -> str | None:
    """Upsert a source Material AND bind it to ``case`` as evidence in one call.

    The former ``DocumentSource`` create + evidence-append, collapsed onto the
    material surface. ``description`` is the Material's global description;
    ``additional_details`` is the case-specific evidence note. Returns the
    material IRI (or ``None`` if the source had no title).
    """
    material_iri = upsert_source_material(
        title=title,
        url=url,
        source_type=source_type,
        source_id=source_id,
        description=description,
        related_entities=related_entities,
        publication_date=publication_date,
    )
    if material_iri is None:
        return None
    bind_material_to_case(
        case,
        material_iri,
        additional_details=additional_details or description or "",
        ordinal=ordinal,
    )
    # Settle the cached visibility from the material's policy now. A case upload
    # is born PUBLIC (→ LISTED), so this is usually a no-op; but it correctly
    # HONORS a caseworker who has embargoed this @id (CASE_GATED/PRIVATE) on an
    # earlier pass — recompute maps that policy to the right cached visibility
    # from the binding case's state, rather than leaving the model-default LISTED.
    # Ingest runs outside the API's on_commit recompute (e.g. management
    # commands), so we recompute here. Best-effort: never let a visibility issue
    # abort the ingest.
    try:
        from materials.visibility import recompute_material_visibility

        recompute_material_visibility(material_iri)
    except Exception:  # noqa: BLE001 - visibility is best-effort, never fatal
        logger.warning(
            "material-visibility recompute failed for %s", material_iri, exc_info=True
        )
    return material_iri
