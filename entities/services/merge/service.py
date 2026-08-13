"""Merge duplicate entities into one survivor.

References are repointed FIRST, each store in its own single-database transaction,
and only then are the duplicates retired in one ``nes`` transaction. Nothing has to
remember an interrupted merge: the duplicates stay live and resolvable until the
last step, every repoint selects rows still holding a retired IRI, and the request
itself names the duplicates — so re-sending it continues from where it stopped.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Dict, List, Optional

from django.db import transaction
from jawafdehi_shared.entities.ids import canonicalize_entity_iri

from entities.models import StoredEntity
from entities.persistence import EntityRepository
from entities.services.publication import PublicationService

from . import references
from .document import drop_self_references, merge_documents
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
            return self._response(
                merge_id=None, status="already_merged", dry_run=dry_run,
                survivor=survivor_doc, retired=duplicate_iris,
                inherited={}, per_store={}, warnings=[],
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
        merged_doc, dropped = drop_self_references(merged_doc, retired)
        warnings = self._warnings(survivor_doc, candidates)
        if dropped:
            warnings.append(
                f"{dropped} reference(s) to a retired entity were removed from the "
                "survivor's own document."
            )

        if dry_run:
            return self._response(
                merge_id=None, status="planned", dry_run=True, survivor=merged_doc,
                retired=retired, inherited=inherited,
                per_store={k: {"repointed": v, "deduplicated": 0} for k, v in counts.items()},
                warnings=warnings,
            )

        # Correlates the log lines of one request. It is durable without a record of
        # its own: repoint_entity_links writes it into the ``change_description`` of
        # every document it touches, so the version history is joinable to the log
        # line. The retirement writes no document and so no version row — auditlog
        # (entities.apps registers StoredEntity) captures that column flip with its
        # actor instead.
        merge_id = str(uuid.uuid4())
        description = change_description or f"Merged {len(retired)} duplicate(s)"
        logger.info("merge %s: %s <= %s", merge_id, survivor_iri, ", ".join(retired))

        self._recheck_duplicates(retired, survivor_iri)
        # Computed before any write, from a query that is stable across attempts: a
        # crashed attempt's manifest is gone, so taking the case ids from what THIS
        # attempt moves would skip the cases whose binds already moved. That
        # deliberately re-widens the set to every case bound to the survivor — a
        # survivor with very many cases makes the request slow, and moving re-indexing
        # out of the request is the real fix.
        case_ids = references.case_ids_touched(retired, survivor_iri)

        per_store, manifest = self._repoint(retired, survivor_iri, author_id, merge_id)
        # The merge keeps no record of itself, so this line is the forensic trail: every
        # row this request moved, joinable by merge id to the auditlog entries and to the
        # version rows the same id names in their change_description.
        logger.info(
            "merge %s repointed %d reference(s): %s",
            merge_id, len(manifest), json.dumps(manifest, ensure_ascii=False, default=str),
        )
        try:
            inherited = self._retire(
                survivor_iri=survivor_iri, retired=retired, candidates=candidates,
                author_id=author_id, description=description,
            )
        except MergeError:
            raise
        except Exception as exc:
            logger.exception("merge %s could not retire the duplicates", merge_id)
            raise MergeError(
                "MERGE_INCOMPLETE",
                "References were repointed but the duplicates were not retired. "
                "Send the same request again to finish.",
                500, merge_id=merge_id,
            ) from exc
        warnings = [*warnings, *self._reindex(case_ids, merge_id)]

        return self._response(
            merge_id=merge_id, status="complete", dry_run=False,
            survivor=self.repo.get_entity(survivor_iri), retired=retired,
            inherited=inherited, per_store=per_store, warnings=warnings,
        )

    # --- phases ---------------------------------------------------------

    def _require_not_retired_elsewhere(self, iri, survivor_iri) -> None:
        """Raise unless a vanished duplicate row is already retired into THIS survivor.

        The one rule both liveness checks apply — the pre-write re-check and ``_retire``'s
        row lock — so a merge racing itself behaves the same whichever notices first.
        Already retired into this survivor is done, not in conflict; raising there would
        strand the references on this survivor and fail every resend identically.
        """
        if self.repo.resolve_tombstone(iri) != survivor_iri:
            raise MergeError(
                "DUPLICATE_ALREADY_MERGED",
                f"{iri} stopped being available for this merge — it was retired or "
                "deleted while the merge was running.",
                409,
            )

    def _recheck_duplicates(self, retired, survivor_iri) -> None:
        """Reject a duplicate retired since _classify, before any reference moves.

        The lock cannot be held across the repoint phase — three databases, three
        separate transactions — so this narrows the window rather than closing it;
        ``_retire`` re-checks each row under its own lock as the backstop.
        """
        with transaction.atomic(using="nes"):
            live = set(
                StoredEntity.objects.select_for_update()
                .filter(pk__in=retired, is_deleted=False)
                .values_list("iri", flat=True)
            )
        for iri in retired:
            if iri not in live:
                self._require_not_retired_elsewhere(iri, survivor_iri)

    def _repoint(self, retired, survivor_iri, author_id, merge_id):
        """Repoint each store in its own transaction. Idempotent, so a resend resumes.

        The returned counts are what THIS call moved, not a running total across
        attempts — a resend after a crash reports only its own work.
        """
        per_store: Dict[str, Dict[str, int]] = {}
        manifest: List[Dict[str, Any]] = []
        try:
            with transaction.atomic(using="default"):
                counts, entries = references.repoint_case_binds(retired, survivor_iri)
                per_store["case_entity_binds"] = vars(counts)
                manifest.extend(entries)

            with transaction.atomic(using="ngm"):
                court_counts, entries = references.repoint_court_rows(retired, survivor_iri)
                per_store.update({k: vars(v) for k, v in court_counts.items()})
                manifest.extend(entries)

            with transaction.atomic(using="nes"):
                counts, entries = references.repoint_entity_links(
                    retired, survivor_iri, author_id=author_id, merge_id=merge_id
                )
                per_store["entity_to_entity_links"] = vars(counts)
                manifest.extend(entries)
        except Exception as exc:
            logger.exception("merge %s incomplete", merge_id)
            raise MergeError(
                "MERGE_INCOMPLETE",
                "Repointing did not finish, so no duplicate has been retired. "
                "Send the same request again to continue from where it stopped.",
                500, merge_id=merge_id,
            ) from exc
        return per_store, manifest

    def _retire(self, *, survivor_iri, retired, candidates, author_id,
                description) -> Dict[str, str]:
        """Publish the merged survivor and tombstone the duplicates. Atomic in nes.

        The survivor document is rebuilt from a fresh read here rather than reused from
        the pre-write phase: up to ``MAX_REFERENCES`` references have moved since then,
        and an edit a caseworker made inside that window would otherwise be overwritten.
        The fresh read is not itself locked, so this narrows that window rather than
        closing it. Returns the inherited field → source IRI map the response reports.
        """
        with transaction.atomic(using="nes"):
            current = self.repo.get_entity(survivor_iri)
            if current is None:
                raise MergeError(
                    "SURVIVOR_RETIRED",
                    f"{survivor_iri} was retired while this merge was running.", 409,
                )
            merged_doc, inherited = merge_documents(
                current, [candidates[iri] for iri in retired]
            )
            merged_doc, _dropped = drop_self_references(merged_doc, retired)
            # validate=False: the merged document is assembled from documents already in
            # the store, so re-gating it on authoring rules can only wedge a merge,
            # never prevent a bad one.
            self.publication.update_entity(
                doc=merged_doc, author_id=author_id, change_description=description,
                validate=False,
            )
            for iri in retired:
                row = (
                    StoredEntity.objects.select_for_update()
                    .filter(pk=iri, is_deleted=False)
                    .first()
                )
                if row is None:
                    # Re-checked under the row lock: two operators merging the same
                    # duplicate into different survivors would otherwise both tombstone
                    # it and repoint its references two ways.
                    self._require_not_retired_elsewhere(iri, survivor_iri)
                    continue
                row.is_deleted = True
                row.merged_into = survivor_iri
                row.updated_at = references.utcnow()
                # update_fields keeps the write (and the auditlog diff) to the flip,
                # leaving the duplicate's ``data`` and ``version`` as they stand.
                row.save(update_fields=["is_deleted", "merged_into", "updated_at"])
        return inherited

    def _reindex(self, case_ids, merge_id) -> List[str]:
        """Re-index the cases this merge touched, warning rather than failing on an outage.

        Every row has moved and every duplicate is retired by now, so a search outage
        must not present as a failed merge. The spec's ``warnings`` array is advisory
        and never blocks.
        """
        from cases.models import Case
        from cases import search_index as case_search

        stale = 0
        for case_id in case_ids:
            case = Case.objects.filter(pk=case_id).first()
            if case is None:
                continue
            try:
                # index_now, not index — index() is the best_effort wrapper and
                # returns None on failure, so this could not count stale cases.
                case_search.index_now(case)
            except Exception:
                logger.exception("merge %s could not re-index case %s", merge_id, case_id)
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

    def _load_survivor(self, survivor_iri):
        doc = self.repo.get_entity(survivor_iri)
        if doc is not None:
            return doc
        target = self.repo.resolve_tombstone(survivor_iri)
        if target:
            dead = self.repo.get_entity(target) is None
            raise MergeError(
                "SURVIVOR_RETIRED",
                f"{survivor_iri} was already merged into {target}."
                + (" That entity is no longer live." if dead else ""),
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
                    dead = self.repo.get_entity(target) is None
                    raise MergeError(
                        "DUPLICATE_ALREADY_MERGED",
                        f"{iri} was already merged into {target}."
                        + (" That entity is no longer live." if dead else ""),
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
