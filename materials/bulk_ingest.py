"""Bulk-ingestion for NGM materials (schema.org JSON-LD documents).

The NGM counterpart to ``entities.services.bulk_ingest`` — the batch write
path for landing large public-document sources (CIAA press releases / annual
reports, Supreme Court NKP issues, development projects, procurement contracts,
…) as :class:`~materials.models.Material` rows.

Each input record carries a full schema.org JSON-LD ``material`` document (keyed
by its ``@id`` material IRI) plus its ``sources``. The pipeline mirrors the NES
one:

1. **Validation** — each doc is checked with ``validate_material_jsonld`` (known
   ``@type``, valid material ``@id`` IRI, ``name`` present) — the SAME rule the
   ``Material`` model enforces in ``clean()``.
2. **≥2-source HOLD gate** — a material needs ≥2 *distinct-publisher* sources
   (independence keyed on ``authority`` or the URL's registrable domain, reusing
   the NES :class:`IngestSource`). Below that the record is HELD (reported, not
   written). NGM has no held-staging table, so held ids are returned for a later
   pass (parity with the NES ``held_ids`` contract).
3. **First-wins in-batch dedup** — a later record with an ``@id`` already seen in
   the batch is counted ``deduped_in_batch`` and skipped.
4. **Upsert** — ``Material`` rows are created/updated by ``@id``; saving each one
   triggers the post_save signal that indexes it into ``ngm-materials``.

Input record shapes accepted (see :func:`_coerce_record`):
- ``{"material": {<json-ld>}, "sources": [...], "material_type": "..."}`` (the
  shape the sourcing normalizers emit), or
- a bare JSON-LD doc (``@id`` present) — treated as a single material with no
  sources (so it HOLDs unless ``--min-sources 0``).
``material_type`` is optional; when absent it is inferred from the doc's
``@type`` via :func:`_infer_material_type`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

from django.db import transaction

# Reuse the NES source-independence logic (publisher_key / registrable domain)
# so the ≥2-source gate behaves identically across NES + NGM.
from entities.services.bulk_ingest.models import IngestSource

from .jsonld import (
    MaterialType,
    validate_material_jsonld,
)
from .models import Material

logger = logging.getLogger("ngm.materials.bulk_ingest")

#: Minimum distinct-publisher sources required to PUBLISH a material.
MIN_SOURCES_TO_PUBLISH = 2

#: schema.org @type -> NGM material_type token, for deriving the promoted column
#: when a record doesn't state material_type explicitly. Falls back to DOCUMENT.
_TYPE_BY_SCHEMA: dict[str, str] = {
    "Legislation": MaterialType.LEGAL_CORPUS,
    "LegislationObject": MaterialType.LEGAL_CORPUS,
    "Report": MaterialType.OFFICIAL_REPORT,
    "Manuscript": MaterialType.MANUSCRIPT,
    "DigitalDocument": MaterialType.DOCUMENT,
    "CreativeWork": MaterialType.DOCUMENT,
}


@dataclass
class MaterialIngestResult:
    """Structured result of a :meth:`MaterialBulkIngestService.ingest` call."""

    total: int = 0
    created: int = 0
    updated: int = 0
    held: int = 0
    deduped_in_batch: int = 0
    failed: int = 0
    dry_run: bool = False
    held_ids: List[str] = field(default_factory=list)
    errors: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def written(self) -> int:
        return self.created + self.updated

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total": self.total,
            "created": self.created,
            "updated": self.updated,
            "held": self.held,
            "deduped_in_batch": self.deduped_in_batch,
            "failed": self.failed,
            "dry_run": self.dry_run,
            "held_ids": list(self.held_ids),
            "errors": list(self.errors),
        }


def _infer_material_type(doc: Dict[str, Any]) -> str:
    """Derive a material_type token from the doc's @type (first known schema type)."""
    atype = doc.get("@type")
    types = atype if isinstance(atype, list) else [atype]
    for t in types:
        if isinstance(t, str) and t in _TYPE_BY_SCHEMA:
            return _TYPE_BY_SCHEMA[t]
    return MaterialType.DOCUMENT


def _coerce_record(raw: Any) -> Tuple[Dict[str, Any], List[IngestSource], Optional[str]]:
    """Return (jsonld_doc, sources, material_type|None) from a raw input record.

    Accepts the ``{"material": …, "sources": […], "material_type": …}`` envelope
    or a bare JSON-LD doc (``@id`` present).
    """
    if not isinstance(raw, dict):
        raise ValueError(f"Record must be a JSON object, got {type(raw).__name__}")

    if "material" in raw or "jsonld" in raw or "sources" in raw or "material_type" in raw:
        # Accept the doc under any of the keys the sourcing normalizers emit:
        # "material" / "jsonld" / "entity_data".
        doc = raw.get("material") or raw.get("jsonld") or raw.get("entity_data") or {}
        raw_sources = raw.get("sources") or []
        material_type = raw.get("material_type")
    elif "@id" in raw:
        doc, raw_sources, material_type = raw, [], None
    else:
        raise ValueError(
            "Record must carry a 'material' JSON-LD doc (or be one with '@id')."
        )

    if not isinstance(doc, dict) or "@id" not in doc:
        raise ValueError("material document must be a JSON-LD object with an '@id'.")

    sources = [IngestSource.from_obj(s) for s in raw_sources]
    return doc, sources, material_type


class MaterialBulkIngestService:
    """Validate and bulk-upsert many JSON-LD material documents."""

    def __init__(self, *, min_sources: int = MIN_SOURCES_TO_PUBLISH):
        self.min_sources = min_sources
        logger.info("MaterialBulkIngestService initialized (min_sources=%d)", min_sources)

    def ingest(
        self,
        records: Iterable[Any],
        *,
        dry_run: bool = False,
    ) -> MaterialIngestResult:
        result = MaterialIngestResult(dry_run=dry_run)
        record_list = list(records)

        to_write: List[Tuple[Dict[str, Any], str]] = []  # (doc, material_type)
        seen_ids: set[str] = set()
        existing_ids = self._existing_ids(record_list)

        for index, raw in enumerate(record_list):
            result.total += 1
            try:
                doc, sources, material_type = _coerce_record(raw)
                validate_material_jsonld(doc)  # @type known, @id valid IRI, name present
                iri = doc["@id"]
            except Exception as e:  # noqa: BLE001 — per-record isolation
                result.failed += 1
                result.errors.append({"index": index, "message": str(e)})
                continue

            if iri in seen_ids:
                result.deduped_in_batch += 1
                continue
            seen_ids.add(iri)

            # ≥2-source HOLD gate (reported, not written — no NGM staging table).
            if self._distinct_publisher_count(sources) < self.min_sources:
                result.held += 1
                result.held_ids.append(iri)
                continue

            mtype = material_type or _infer_material_type(doc)
            to_write.append((doc, mtype))
            if iri in existing_ids:
                result.updated += 1
            else:
                result.created += 1

        if not dry_run and to_write:
            self._persist(to_write)

        logger.info(
            "Material bulk ingest %s: total=%d created=%d updated=%d held=%d "
            "deduped_in_batch=%d failed=%d",
            "(dry-run)" if dry_run else "committed",
            result.total, result.created, result.updated, result.held,
            result.deduped_in_batch, result.failed,
        )
        return result

    # ------------------------------------------------------------------

    @staticmethod
    def _distinct_publisher_count(sources: List[IngestSource]) -> int:
        return len(
            {key for s in sources if (key := s.publisher_key()) is not None}
        )

    @staticmethod
    def _existing_ids(records: List[Any]) -> set[str]:
        """The subset of the batch's @ids that already exist as Material rows."""
        ids: set[str] = set()
        for raw in records:
            try:
                doc, _, _ = _coerce_record(raw)
                ids.add(doc["@id"])
            except Exception:  # noqa: BLE001
                continue
        if not ids:
            return set()
        return set(
            Material.objects.filter(pk__in=ids).values_list("pk", flat=True)
        )

    def _persist(self, to_write: List[Tuple[Dict[str, Any], str]]) -> None:
        """Upsert each material by ``@id``, running model validation + indexing.

        Upsert-by-``@id`` via ``update_or_create`` (not a fresh-instance
        ``save()``): re-ingesting an existing ``@id`` is an UPDATE, so
        ``full_clean`` skips ``validate_unique`` (an existing pk is not a
        duplicate) and only the mutable columns are written — preserving
        ``created_at`` (``auto_now_add`` fires on INSERT only) while the
        ``post_save`` ngm-materials indexer still runs. Mirrors
        ``single_source_ingest.upsert_single_source_material`` so both write
        paths are idempotent and behave identically on re-ingest.
        """
        with transaction.atomic():
            for doc, material_type in to_write:
                candidate = Material.from_jsonld(doc, material_type=material_type)
                candidate.full_clean(validate_unique=False)
                Material.objects.update_or_create(
                    iri=candidate.iri,
                    defaults={
                        "material_type": candidate.material_type,
                        "source": candidate.source,
                        "ident": candidate.ident,
                        "data": candidate.data,
                    },
                )
