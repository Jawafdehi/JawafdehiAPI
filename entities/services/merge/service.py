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
        enforce_reference_cap: bool = True,
    ) -> Dict[str, Any]:
        survivor_iri, duplicate_iris = self._canonicalize(survivor_iri, duplicate_iris)
        survivor_doc = self._load_survivor(survivor_iri)
        candidates = self._classify(survivor_iri, duplicate_iris, survivor_doc, type_family)

        if not candidates:
            pending = self._pending_merges(survivor_iri, duplicate_iris)
            if not pending:
                return self._response(
                    merge_id=None, status="already_merged", dry_run=dry_run,
                    survivor=survivor_doc, retired=duplicate_iris,
                    inherited={}, per_store={}, warnings=[],
                )
            # Every duplicate is tombstoned but an earlier attempt stopped before it
            # finished repointing. Returning "already_merged" here would strand those
            # references on a retired entity forever.
            pending_duplicates = sorted({iri for record in pending for iri in record.duplicate_iris})
            if dry_run:
                counts = references.count_references(pending_duplicates, survivor_iri)
                return self._response(
                    merge_id=None, status="planned", dry_run=True,
                    survivor=survivor_doc, retired=pending_duplicates, inherited={},
                    per_store={k: {"repointed": v, "deduplicated": 0}
                               for k, v in counts.items()},
                    warnings=[],
                )
            per_store: Dict[str, Dict[str, int]] = {}
            for record in pending:
                for store, counts in self._phase_two(
                    record, record.duplicate_iris, survivor_iri, author_id
                ).items():
                    running = per_store.setdefault(store, {"repointed": 0, "deduplicated": 0})
                    running["repointed"] += counts["repointed"]
                    running["deduplicated"] += counts["deduplicated"]
            return self._response(
                # The newest record resumed — one id has to stand for the batch.
                merge_id=str(pending[-1].id), status="complete", dry_run=False,
                survivor=self.repo.get_entity(survivor_iri), retired=pending_duplicates,
                inherited={}, per_store=per_store,
                warnings=self._phase_three(pending[-1], survivor_iri),
            )

        retired = list(candidates)
        counts = references.count_references(retired, survivor_iri)
        total = sum(counts.values())
        # The cap keeps a large merge from timing out inside a request. manage.py
        # merge_entities has no request to time out, and is what the spec names as
        # the way to run one, so it lifts the cap.
        if enforce_reference_cap and total > MAX_REFERENCES:
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
        warnings = [*warnings, *self._phase_three(merge, survivor_iri)]

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
        """Repoint each store in its own transaction. Idempotent, so a retry resumes.

        The returned counts are what THIS call moved, not the merge's running total —
        a resume reports only its own work, same as ``repoint_entity_links``' per-link
        counts already differ from its per-document manifest entries.
        """
        per_store: Dict[str, Dict[str, int]] = {}
        # Seed from whatever an earlier, interrupted attempt already saved, and
        # de-duplicate by identity, so a resume's save doesn't erase entries a crash
        # already recorded (e.g. a dedup that deleted a row on the first attempt).
        manifest: List[Dict[str, Any]] = list(merge.reference_manifest or [])
        seen = {(e["store"], e["pk"], e["field"]) for e in manifest}

        def record(entries: List[Dict[str, Any]]) -> None:
            for entry in entries:
                key = (entry["store"], entry["pk"], entry["field"])
                if key in seen:
                    continue
                seen.add(key)
                manifest.append(entry)

        try:
            with transaction.atomic(using="default"):
                counts, entries = references.repoint_case_binds(retired, survivor_iri)
                per_store["case_entity_binds"] = vars(counts)
                record(entries)

            with transaction.atomic(using="ngm"):
                court_counts, entries = references.repoint_court_rows(retired, survivor_iri)
                per_store.update({k: vars(v) for k, v in court_counts.items()})
                record(entries)

            with transaction.atomic(using="nes"):
                counts, entries = references.repoint_entity_links(
                    retired, survivor_iri, author_id=author_id, merge_id=str(merge.id)
                )
                per_store["entity_to_entity_links"] = vars(counts)
                record(entries)
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

    def _phase_three(self, merge, survivor_iri) -> List[str]:
        """Re-index affected cases, returning a warning per case that could not be indexed.

        Phase two already marked the merge complete and moved every row, so a search
        outage must not present as a failed merge. The spec's ``warnings`` array is
        advisory and never blocks.
        """
        from cases.models import Case
        from cases import search_index as case_search

        stale = 0
        for case_id in references.affected_case_ids(survivor_iri):
            case = Case.objects.filter(pk=case_id).first()
            if case is None:
                continue
            try:
                # index_now, not index — index() is the best_effort wrapper and
                # returns None on failure, so phase three could not count stale cases.
                case_search.index_now(case)
            except Exception:
                logger.exception("merge %s could not re-index case %s", merge.id, case_id)
                stale += 1
        if not stale:
            return []
        return [
            f"{stale} case(s) could not be re-indexed for search. The merge is "
            "complete; re-index them to refresh search results."
        ]

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

    def _pending_merges(self, survivor_iri: str, duplicate_iris: List[str]) -> List[EntityMerge]:
        """Unfinished merges these duplicates belong to, oldest first.

        Also matches a record whose own survivor has since been merged away: the
        record still names the old survivor, but its references belong on whatever
        that survivor now resolves to.
        """
        requested = set(duplicate_iris)
        found = []
        for merge in EntityMerge.objects.filter(status=EntityMerge.PENDING).order_by("created_at"):
            if not requested & set(merge.duplicate_iris):
                continue
            if merge.survivor_iri == survivor_iri:
                found.append(merge)
            elif self.repo.resolve_tombstone(merge.survivor_iri) == survivor_iri:
                found.append(merge)
        return found

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
