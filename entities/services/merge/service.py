"""Merge duplicate entities into one survivor.

Phase 1 is a single transaction in the ``nes`` database: it records the merge,
merges the survivor's document, and tombstones the duplicates — which is also what
makes the retired IRIs redirect. Phases 2 and 3 repoint the other stores per
database. Because every repoint selects rows still holding a retired IRI, a failure
between phases leaves work that the next identical request finishes.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from django.db import transaction
from jawafdehi_shared.entities.ids import canonicalize_entity_iri

from entities.models import EntityMerge, StoredEntity
from entities.persistence import EntityRepository
from entities.services.publication import PublicationService

from . import references
from .document import merge_documents
from .families import FAMILY_NAMES, families_compatible, families_for

logger = logging.getLogger(__name__)

MAX_DUPLICATES = 25
MAX_REFERENCES = 1000


class MergeError(Exception):
    """A refused merge, carrying the spec's error code and HTTP status."""

    def __init__(self, code: str, message: str, http_status: int, **extra: Any):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.extra = extra


class EntityMergeService:
    def __init__(self, repo: Optional[EntityRepository] = None):
        self.repo = repo or EntityRepository()
        self.publication = PublicationService()

    def merge(
        self,
        *,
        survivor_iri: str,
        duplicate_iris: List[str],
        author_id: str,
        change_description: str = "",
        type_family: Optional[str] = None,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        survivor_iri, duplicate_iris = self._canonicalize(survivor_iri, duplicate_iris)
        survivor_doc = self._load_survivor(survivor_iri)
        candidates = self._classify(survivor_iri, duplicate_iris, survivor_doc, type_family)

        if not candidates:
            return self._response(
                merge_id=None, status="already_merged", dry_run=dry_run,
                survivor=survivor_doc, retired=duplicate_iris,
                inherited={}, per_store={}, warnings=[],
            )

        retired = list(candidates)
        counts = references.count_references(retired, survivor_iri)
        total = sum(counts.values())
        if total > MAX_REFERENCES:
            raise MergeError(
                "MERGE_TOO_LARGE",
                f"This merge touches {total} references, over the {MAX_REFERENCES} limit. "
                "Run manage.py merge_entities instead.",
                409, reference_count=total,
            )

        conflicts = references.detect_outcome_conflicts(retired, survivor_iri)
        if conflicts:
            raise MergeError(
                "OUTCOME_CONFLICT",
                "One or more cases record different settled verdicts for these entities.",
                409, conflicts=conflicts,
            )

        merged_doc, inherited = merge_documents(
            survivor_doc, [candidates[iri] for iri in retired]
        )
        warnings = self._warnings(survivor_doc, candidates)

        if dry_run:
            return self._response(
                merge_id=None, status="planned", dry_run=True, survivor=merged_doc,
                retired=retired, inherited=inherited,
                per_store={k: {"repointed": v, "deduplicated": 0} for k, v in counts.items()},
                warnings=warnings,
            )

        merge = self._phase_one(
            survivor_iri=survivor_iri, retired=retired, candidates=candidates,
            survivor_doc=survivor_doc, merged_doc=merged_doc,
            author_id=author_id, change_description=change_description,
        )
        per_store = self._phase_two(merge, retired, survivor_iri, author_id)
        self._phase_three(merge, survivor_iri)

        return self._response(
            merge_id=str(merge.id), status="complete", dry_run=False,
            survivor=self.repo.get_entity(survivor_iri), retired=retired,
            inherited=inherited, per_store=per_store, warnings=warnings,
        )

    # --- phases ---------------------------------------------------------

    def _phase_one(self, *, survivor_iri, retired, candidates, survivor_doc,
                   merged_doc, author_id, change_description) -> EntityMerge:
        """Record the merge, merge the survivor, tombstone the duplicates. Atomic in nes."""
        description = change_description or f"Merged {len(retired)} duplicate(s)"
        with transaction.atomic(using="nes"):
            merge = EntityMerge.objects.create(
                survivor_iri=survivor_iri,
                duplicate_iris=retired,
                duplicate_snapshots={iri: candidates[iri] for iri in retired},
                survivor_snapshot_before=survivor_doc,
                status=EntityMerge.PENDING,
                author_id=author_id,
                change_description=description,
            )
            self.publication.update_entity(
                doc=merged_doc, author_id=author_id, change_description=description
            )
            for iri in retired:
                row = StoredEntity.objects.get(pk=iri)
                row.is_deleted = True
                row.merged_into = survivor_iri
                row.updated_at = references.utcnow()
                row.save(update_fields=["is_deleted", "merged_into", "updated_at"])
        return merge

    def _phase_two(self, merge, retired, survivor_iri, author_id) -> Dict[str, Dict[str, int]]:
        """Repoint each store in its own transaction. Idempotent, so a retry resumes."""
        per_store: Dict[str, Dict[str, int]] = {}
        manifest: List[Dict[str, Any]] = []
        try:
            with transaction.atomic(using="default"):
                counts, entries = references.repoint_case_binds(retired, survivor_iri)
                per_store["case_entity_binds"] = vars(counts)
                manifest += entries

            with transaction.atomic(using="ngm"):
                court_counts, entries = references.repoint_court_rows(retired, survivor_iri)
                per_store.update({k: vars(v) for k, v in court_counts.items()})
                manifest += entries

            with transaction.atomic(using="nes"):
                counts, entries = references.repoint_entity_links(
                    retired, survivor_iri, author_id=author_id, merge_id=str(merge.id)
                )
                per_store["entity_to_entity_links"] = vars(counts)
                manifest += entries
        except Exception as exc:
            merge.reference_manifest = manifest
            merge.save(update_fields=["reference_manifest"])
            logger.exception("merge %s incomplete", merge.id)
            raise MergeError(
                "MERGE_INCOMPLETE",
                "The duplicates were retired but repointing did not finish. "
                "Send the same request again to resume.",
                500, merge_id=str(merge.id),
            ) from exc

        merge.reference_manifest = manifest
        merge.status = EntityMerge.COMPLETE
        merge.completed_at = references.utcnow()
        merge.save(update_fields=["reference_manifest", "status", "completed_at"])
        return per_store

    def _phase_three(self, merge, survivor_iri) -> None:
        """Re-index cases: their search documents denormalize entity names."""
        from cases.models import Case
        from cases import search_index as case_search

        for case_id in references.affected_case_ids(survivor_iri):
            case = Case.objects.filter(pk=case_id).first()
            if case is not None:
                # index_now, not index — the resumable batch path wants failures to
                # surface rather than be swallowed the way the write-time signal does.
                case_search.index_now(case)

    # --- validation -----------------------------------------------------

    def _canonicalize(self, survivor_iri, duplicate_iris):
        if not survivor_iri or not isinstance(duplicate_iris, list) or not duplicate_iris:
            raise MergeError(
                "INVALID_REQUEST", "survivor and a non-empty duplicates list are required.", 400
            )
        if len(duplicate_iris) > MAX_DUPLICATES:
            raise MergeError(
                "INVALID_REQUEST",
                f"At most {MAX_DUPLICATES} duplicates per merge, got {len(duplicate_iris)}.",
                400,
            )
        try:
            survivor = canonicalize_entity_iri(survivor_iri)
            duplicates, seen = [], set()
            for raw in duplicate_iris:
                iri = canonicalize_entity_iri(raw)
                if iri not in seen:
                    seen.add(iri)
                    duplicates.append(iri)
        except (ValueError, TypeError) as exc:
            raise MergeError("INVALID_ENTITY_ID", str(exc), 400) from exc
        if survivor in duplicates:
            raise MergeError(
                "SELF_MERGE", "The survivor cannot also be listed as a duplicate.", 422
            )
        return survivor, duplicates

    def _load_survivor(self, survivor_iri):
        doc = self.repo.get_entity(survivor_iri)
        if doc is not None:
            return doc
        target = self.repo.resolve_tombstone(survivor_iri)
        if target:
            raise MergeError(
                "SURVIVOR_RETIRED",
                f"{survivor_iri} was already merged into {target}.",
                409, merged_into=target,
            )
        raise MergeError("NOT_FOUND", f"Entity {survivor_iri} not found.", 404)

    def _classify(self, survivor_iri, duplicate_iris, survivor_doc, type_family):
        """Live duplicates to merge. Already-merged ones drop out; the rest raise."""
        if type_family is not None:
            if type_family not in FAMILY_NAMES:
                raise MergeError(
                    "INVALID_REQUEST",
                    f"type_family must be one of {sorted(FAMILY_NAMES)}.", 400,
                )
            if type_family not in families_for(survivor_doc):
                raise MergeError(
                    "TYPE_MISMATCH",
                    f"The survivor is not a {type_family} entity.", 422,
                )

        candidates: Dict[str, Dict[str, Any]] = {}
        for iri in duplicate_iris:
            doc = self.repo.get_entity(iri)
            if doc is None:
                target = self.repo.resolve_tombstone(iri)
                if target == survivor_iri:
                    continue
                if target:
                    raise MergeError(
                        "DUPLICATE_ALREADY_MERGED",
                        f"{iri} was already merged into {target}.",
                        409, merged_into=target,
                    )
                raise MergeError("NOT_FOUND", f"Entity {iri} not found.", 404)
            if not families_compatible(survivor_doc, doc):
                raise MergeError(
                    "TYPE_MISMATCH",
                    f"Cannot merge {doc.get('@type')} into {survivor_doc.get('@type')}.",
                    422,
                )
            candidates[iri] = doc
        return candidates

    def _warnings(self, survivor_doc, candidates) -> List[str]:
        """Advisory only: flag a survivor that looks thinner than what it is absorbing."""
        out = []
        survivor_size = len(survivor_doc)
        for iri, doc in candidates.items():
            if len(doc) > survivor_size:
                out.append(
                    f"The entity you are retiring ({iri}) holds more fields than the "
                    "survivor. Check that survivor and duplicate are the right way round."
                )
        return out

    # --- response -------------------------------------------------------

    def _response(self, *, merge_id, status, dry_run, survivor, retired,
                  inherited, per_store, warnings) -> Dict[str, Any]:
        zero = {"repointed": 0, "deduplicated": 0}
        stores = {key: per_store.get(key, dict(zero)) for key in references.STORE_KEYS}
        total = sum(v["repointed"] + v["deduplicated"] for v in stores.values())
        return {
            "merge_id": merge_id,
            "status": status,
            "dry_run": dry_run,
            "survivor": survivor,
            "retired": retired,
            "fields_inherited": inherited,
            "references": stores,
            "total_references": total,
            "warnings": warnings,
        }
