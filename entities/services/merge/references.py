"""Count and repoint every store that holds an entity IRI.

Five stores across three databases. Each repoint is idempotent — it selects rows
still holding a retired IRI, so re-running after a partial failure finds nothing
left to do. Rows are saved one at a time rather than with ``QuerySet.update()``
because ``CaseEntityRelationship`` is auditlog-registered and bulk updates emit no
audit entry.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

from django.db.models import Q, TextField
from django.db.models.functions import Cast

from .document import rewrite_references

STORE_KEYS: Tuple[str, ...] = (
    "case_entity_binds",
    "court_cases",
    "court_case_parties",
    "blacklisted_firms",
    "entity_to_entity_links",
)

#: Verdicts that settle a case. CHARGED means "pending", so anything here beats it.
TERMINAL_OUTCOMES = frozenset({"convicted", "acquitted", "abated"})


@dataclass
class RefCounts:
    repointed: int = 0
    deduplicated: int = 0


@dataclass
class _Manifest:
    entries: List[Dict[str, Any]] = dc_field(default_factory=list)

    def add(self, store, pk, field, from_iri, to_iri, action):
        self.entries.append({
            "store": store, "pk": str(pk), "field": field,
            "from": from_iri, "to": to_iri, "action": action,
        })


def _link_candidates(retired: List[str]):
    """Live entities whose document text mentions any retired IRI.

    One OR'd Q over a single annotated queryset — combining annotated querysets with
    ``|`` collides on the annotation alias.
    """
    from entities.models import StoredEntity

    if not retired:
        return StoredEntity.objects.none()
    condition = Q()
    for iri in retired:
        condition |= Q(_text__contains=iri)
    return (
        StoredEntity.objects.filter(is_deleted=False)
        .exclude(iri__in=retired)
        .annotate(_text=Cast("data", TextField()))
        .filter(condition)
    )


def count_references(retired: List[str]) -> Dict[str, int]:
    """How many references each store holds for the retired IRIs."""
    from cases.models import CaseEntityRelationship
    from courts.models import BlacklistedFirm, CaseEntity, CourtCase

    return {
        "case_entity_binds": CaseEntityRelationship.objects.filter(nes_id__in=retired).count(),
        "court_cases": CourtCase.objects.filter(nes_id__in=retired).count(),
        "court_case_parties": CaseEntity.objects.filter(nes_id__in=retired).count(),
        "blacklisted_firms": BlacklistedFirm.objects.filter(nes_id__in=retired).count(),
        "entity_to_entity_links": _link_candidates(retired).count(),
    }


def detect_outcome_conflicts(retired: List[str], survivor: str) -> List[Dict[str, Any]]:
    """Cases where the survivor and a duplicate carry different settled verdicts."""
    from cases.models import CaseEntityRelationship

    survivor_rows = {
        (r.case_id, r.relationship_type): r.outcome
        for r in CaseEntityRelationship.objects.filter(nes_id=survivor)
    }
    conflicts = []
    for row in CaseEntityRelationship.objects.filter(nes_id__in=retired):
        theirs = survivor_rows.get((row.case_id, row.relationship_type))
        if (
            theirs in TERMINAL_OUTCOMES
            and row.outcome in TERMINAL_OUTCOMES
            and theirs != row.outcome
        ):
            conflicts.append({
                "case_id": row.case_id,
                "survivor_outcome": theirs,
                "duplicate_outcome": row.outcome,
            })
    return conflicts


def _better_outcome(survivor_outcome, duplicate_outcome):
    """A settled verdict wins over CHARGED; otherwise keep the survivor's."""
    if survivor_outcome in TERMINAL_OUTCOMES:
        return survivor_outcome
    if duplicate_outcome in TERMINAL_OUTCOMES:
        return duplicate_outcome
    return survivor_outcome or duplicate_outcome


def repoint_case_binds(retired: List[str], survivor: str) -> Tuple[RefCounts, List[Dict]]:
    """Move case↔entity binds onto the survivor, folding rows that would collide.

    ``UniqueConstraint(case, nes_id, relationship_type)`` means a case already bound
    to the survivor under the same role cannot take a second row, so the duplicate's
    row is merged into the sibling and deleted.
    """
    from cases.models import CaseEntityRelationship

    counts, manifest = RefCounts(), _Manifest()
    for row in CaseEntityRelationship.objects.filter(nes_id__in=retired):
        sibling = (
            CaseEntityRelationship.objects.filter(
                case_id=row.case_id, nes_id=survivor,
                relationship_type=row.relationship_type,
            )
            .exclude(pk=row.pk)
            .first()
        )
        if sibling is None:
            was = row.nes_id
            row.nes_id = survivor
            row.save()
            counts.repointed += 1
            manifest.add("case_entity_binds", row.pk, "nes_id", was, survivor, "repointed")
            continue

        if row.notes and row.notes not in (sibling.notes or ""):
            sibling.notes = " | ".join(p for p in (sibling.notes, row.notes) if p)[:500]
        sibling.outcome = _better_outcome(sibling.outcome, row.outcome)
        sibling.save()
        manifest.add("case_entity_binds", row.pk, "nes_id", row.nes_id, survivor, "deduplicated")
        row.delete()
        counts.deduplicated += 1
    return counts, manifest.entries


def repoint_court_rows(retired: List[str], survivor: str) -> Tuple[Dict[str, RefCounts], List[Dict]]:
    """Move the three ngm stores onto the survivor. None has a unique key on nes_id."""
    from courts.models import BlacklistedFirm, CaseEntity, CourtCase

    manifest = _Manifest()
    per_store = {k: RefCounts() for k in ("court_cases", "court_case_parties", "blacklisted_firms")}

    for store, model in (
        ("court_cases", CourtCase),
        ("court_case_parties", CaseEntity),
        ("blacklisted_firms", BlacklistedFirm),
    ):
        for row in model.objects.filter(nes_id__in=retired):
            # CourtCase's PK is composite (case_number, court) — there is no id column.
            pk = (
                f"{row.court_id}/{row.case_number}"
                if store == "court_cases"
                else row.pk
            )
            was = row.nes_id
            row.nes_id = survivor
            row.save(update_fields=["nes_id", "updated_at"])
            per_store[store].repointed += 1
            manifest.add(store, pk, "nes_id", was, survivor, "repointed")
    return per_store, manifest.entries


def repoint_entity_links(
    retired: List[str], survivor: str, *, author_id: str, merge_id: str
) -> Tuple[RefCounts, List[Dict]]:
    """Rewrite ``{"@id": retired}`` inside other entities' documents, versioning each."""
    from entities.services.publication import PublicationService

    mapping = {iri: survivor for iri in retired}
    service = PublicationService()
    counts, manifest = RefCounts(), _Manifest()

    for row in list(_link_candidates(retired)):
        rewritten, changed = rewrite_references(row.data, mapping)
        if not changed:
            continue
        service.update_entity(
            doc=rewritten,
            author_id=author_id,
            change_description=f"Reference repointed by merge {merge_id}",
        )
        counts.repointed += changed
        manifest.add("entity_to_entity_links", row.iri, "data", retired[0], survivor, "repointed")
    return counts, manifest.entries


def affected_case_ids(survivor: str) -> List[int]:
    """Cases now bound to the survivor — their search documents denormalize entity names."""
    from cases.models import CaseEntityRelationship

    return sorted(
        set(
            CaseEntityRelationship.objects.filter(nes_id=survivor)
            .values_list("case_id", flat=True)
        )
    )


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
