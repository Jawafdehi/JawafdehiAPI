"""Bulk-ingestion service (JSON-LD clean-slate port).

``BulkIngestService.ingest_entities`` is the batch write path. Each record carries
an entity payload (a full JSON-LD doc OR the authoring shape) plus its sources.

1. **Validation parity** — every record is normalized to JSON-LD and minimally
   validated with the SAME rules as ``PublicationService.create_entity``
   (``validate_jsonld_entity``: @type known, @id valid IRI, name present).
2. **≥2-source HOLD gate** — a public entity needs ≥2 *distinct-publisher*
   sources (independence keyed on authority or eTLD+1 host). Below that the
   record is HELD (staged in ``held_entities``), not published.
3. **First-wins in-batch dedup** — a later record resolving to an @id already
   decided this batch is counted ``deduped_in_batch``.
4. **Versioning** — a version row is kept for every written entity; new IRIs get
   version 1, existing IRIs are re-versioned with an incremented number.

The whole accepted set is written inside one DB transaction.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from django.db import transaction

from nes_service.entities.persistence import EntityRepository
from nes_service.entities.validation import validate_jsonld_entity
from nes_service.entities.write_validation import normalize_authoring_payload
from nes_service.services.bulk_ingest.models import (
    BulkIngestResult,
    IngestRecord,
    IngestRecordError,
)

UTC = timezone.utc
logger = logging.getLogger(__name__)

#: Minimum verifiable sources required to PUBLISH a public entity.
MIN_SOURCES_TO_PUBLISH = 2


class BulkIngestService:
    """Validate and bulk-upsert many JSON-LD entity records into the database."""

    def __init__(
        self,
        repo: Optional[EntityRepository] = None,
        *,
        min_sources: int = MIN_SOURCES_TO_PUBLISH,
    ):
        self.repo = repo or EntityRepository()
        self.min_sources = min_sources
        logger.info("BulkIngestService initialized (min_sources=%d)", min_sources)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def ingest_entities(
        self,
        records: Iterable[Any],
        author_id: str,
        change_description: str = "Bulk ingest",
        dry_run: bool = False,
    ) -> BulkIngestResult:
        """Validate and bulk-upsert a batch of entity records."""
        result = BulkIngestResult(dry_run=dry_run)

        record_list = list(records)
        author = self._build_author(author_id)

        # to_write: (doc, version_number, created_at)
        to_write: List[Tuple[Dict[str, Any], int, datetime]] = []
        held_to_stage: List[Tuple[str, IngestRecord]] = []
        existing = self._existing_entities(record_list)

        seen_ids: set[str] = set()

        for index, raw in enumerate(record_list):
            try:
                record = (
                    raw if isinstance(raw, IngestRecord) else IngestRecord.from_dict(raw)
                )
            except Exception as e:  # noqa: BLE001 - per-record isolation
                result.total += 1
                result.failed += 1
                result.errors.append(
                    IngestRecordError(index=index, slug=None, message=str(e))
                )
                continue

            result.total += 1
            slug = record.entity_data.get("slug")
            try:
                doc = self._normalize_and_validate(record)
                iri = doc["@id"]
            except Exception as e:  # noqa: BLE001
                result.failed += 1
                result.errors.append(
                    IngestRecordError(index=index, slug=slug, message=str(e))
                )
                continue

            if iri in seen_ids:
                result.deduped_in_batch += 1
                continue
            seen_ids.add(iri)

            # ≥2-source HOLD gate (staged, not published).
            if self._is_held(record):
                result.held += 1
                result.held_ids.append(iri)
                held_to_stage.append((iri, record))
                continue

            prior = existing.get(iri)
            now = datetime.now(UTC)
            if prior is None:
                version_number = 1
                created_at = now
                result.created += 1
            else:
                version_number = prior[0] + 1
                created_at = prior[1]
                result.updated += 1

            doc = dict(doc)
            doc["dateCreated"] = doc.get("dateCreated") or created_at.isoformat()
            doc["jawafdehi:version"] = {
                "entity_iri": iri,
                "version_number": version_number,
                "author": author,
                "change_description": change_description,
                "created_at": now.isoformat(),
            }
            to_write.append((doc, version_number, created_at))

        if not dry_run and to_write:
            self._persist_batch(author_id, author, to_write, change_description)

        if not dry_run and held_to_stage:
            self._stage_held(held_to_stage)

        logger.info(
            "Bulk ingest %s: total=%d created=%d updated=%d held=%d "
            "deduped_in_batch=%d failed=%d",
            "(dry-run)" if dry_run else "committed",
            result.total, result.created, result.updated, result.held,
            result.deduped_in_batch, result.failed,
        )
        return result

    # ------------------------------------------------------------------
    # ≥2-source HOLD gate
    # ------------------------------------------------------------------

    def _is_held(self, record: IngestRecord) -> bool:
        return self._distinct_publisher_count(record) < self.min_sources

    @staticmethod
    def _distinct_publisher_count(record: IngestRecord) -> int:
        publishers = {
            key
            for src in record.sources
            if (key := src.publisher_key()) is not None
        }
        return len(publishers)

    # ------------------------------------------------------------------
    # Validation + construction (mirrors PublicationService.create_entity)
    # ------------------------------------------------------------------

    def _normalize_and_validate(self, record: IngestRecord) -> Dict[str, Any]:
        """Normalize a record's payload to JSON-LD and minimally validate it."""
        payload = dict(record.entity_data)
        # The record may carry entity_prefix separately from entity_data (the
        # authoring shape); a full JSON-LD payload (with @id) needs no prefix.
        if record.entity_prefix and "@id" not in payload:
            payload.setdefault("entity_prefix", record.entity_prefix)
        doc = normalize_authoring_payload(payload)
        validate_jsonld_entity(doc)
        return doc

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _persist_batch(
        self, author_id: str, author: Dict[str, Any],
        to_write: List[Tuple[Dict[str, Any], int, datetime]],
        change_description: str,
    ) -> None:
        docs = [doc for doc, _, _ in to_write]
        versions = {doc["@id"]: v for doc, v, _ in to_write}
        created_ats = {doc["@id"]: c for doc, _, c in to_write}
        now = datetime.now(UTC)
        with transaction.atomic():
            self.repo.put_author(author_id, author)
            self.repo.bulk_put_entities(docs, versions=versions, created_ats=created_ats)
            for doc, version_number, _ in to_write:
                self.repo.put_version(
                    iri=doc["@id"],
                    version_number=version_number,
                    author_id=author_id,
                    snapshot=doc,
                    created_at=now,
                )

    def _stage_held(self, held: List[Tuple[str, IngestRecord]]) -> None:
        reason = f"fewer than {self.min_sources} distinct-publisher sources"
        payload = [
            {
                "iri": iri,
                "entity_data": record.entity_data,
                "sources": [
                    {
                        "url": s.url,
                        "title": s.title,
                        "kind": s.kind,
                        "authority": s.authority,
                    }
                    for s in record.sources
                ],
                "reason": reason,
            }
            for iri, record in held
        ]
        try:
            self.repo.stage_held_entities(payload)
        except Exception as e:  # noqa: BLE001 - staging is best-effort
            logger.warning("Failed to stage %d held record(s): %s", len(payload), e)

    def _existing_entities(self, records: List[Any]) -> Dict[str, Tuple[int, datetime]]:
        """Map @id -> (current version, created_at) for records already stored."""
        existing: Dict[str, Tuple[int, datetime]] = {}
        for raw in records:
            try:
                record = (
                    raw if isinstance(raw, IngestRecord) else IngestRecord.from_dict(raw)
                )
                iri = self._normalize_and_validate(record)["@id"]
            except Exception:  # noqa: BLE001
                continue
            if iri in existing:
                continue
            version = self.repo.entity_version(iri)
            if version is not None:
                created_at = self.repo.entity_created_at(iri) or datetime.now(UTC)
                existing[iri] = (version, created_at)
        return existing

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_author(author_id: str) -> Dict[str, Any]:
        slug = author_id.split(":", 1)[1] if ":" in author_id else author_id
        return {"id": author_id, "slug": slug}
