"""Count and repoint every store that holds an entity IRI.

Five stores across three databases. Each repoint is idempotent — it selects rows
still holding a retired IRI, so re-running after a partial failure finds nothing
left to do. Rows are saved one at a time because the manifest needs each row's pk
and the colliding-bind path branches per row.
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

    def add(self, store, pk, field, from_iri, to_iri, action, **extra):
        entry = {
            "store": store, "pk": str(pk), "field": field,
            "from": from_iri, "to": to_iri, "action": action,
        }
        entry.update(extra)
        self.entries.append(entry)


def _link_candidates(retired: List[str], survivor: str):
    """Live entities, other than the survivor, whose document text mentions a retired IRI.

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
        .exclude(iri__in=[*retired, survivor])
        .annotate(_text=Cast("data", TextField()))
        .filter(condition)
    )


def count_references(retired: List[str], survivor: str) -> Dict[str, int]:
    """How many references each store holds for the retired IRIs.

    The entity-link count runs the same rewrite the repoint runs and sums what it
    would change. Counting candidate documents instead would report a different
    unit from ``repoint_entity_links`` and would count a bare-string ``sameAs``
    mention that no rewrite can ever clear.
    """
    from cases.models import CaseEntityRelationship
    from courts.models import BlacklistedFirm, CaseEntity, CourtCase

    mapping = {iri: survivor for iri in retired}
    links = 0
    for row in _link_candidates(retired, survivor).only("iri", "data"):
        _, changed = rewrite_references(row.data, mapping)
        links += changed

    return {
        "case_entity_binds": CaseEntityRelationship.objects.filter(nes_id__in=retired).count(),
        "court_cases": CourtCase.objects.filter(nes_id__in=retired).count(),
        "court_case_parties": CaseEntity.objects.filter(nes_id__in=retired).count(),
        "blacklisted_firms": BlacklistedFirm.objects.filter(nes_id__in=retired).count(),
        "entity_to_entity_links": links,
    }


def case_ids_touched(retired: List[str], survivor: str) -> List[int]:
    """Cases to re-index for this merge, computed before any write.

    Deliberately wider than the binds that actually move: it includes the survivor's
    own cases, because after a crashed attempt some binds already moved and a
    retired-only query would miss them and leave those case documents stale.
    """
    from cases.models import CaseEntityRelationship

    return sorted(
        set(
            CaseEntityRelationship.objects.filter(nes_id__in=[*retired, survivor])
            .values_list("case_id", flat=True)
        )
    )


def detect_outcome_conflicts(retired: List[str], survivor: str) -> List[Dict[str, Any]]:
    """Cases where two entities in this merge carry different settled verdicts.

    Every entity is compared against every other, not just against the survivor:
    two duplicates can disagree on a case the survivor was never bound to, and
    folding them would delete one verdict silently.
    """
    from cases.models import CaseEntityRelationship

    order = [survivor, *retired]
    rank = {iri: position for position, iri in enumerate(order)}
    groups: Dict[Tuple[Any, str], Dict[str, str]] = {}
    for row in CaseEntityRelationship.objects.filter(nes_id__in=order):
        if row.outcome not in TERMINAL_OUTCOMES:
            continue
        groups.setdefault((row.case_id, row.relationship_type), {})[row.nes_id] = row.outcome

    conflicts = []
    for (case_id, relationship_type), by_iri in sorted(groups.items()):
        verdicts = [by_iri[iri] for iri in sorted(by_iri, key=lambda i: rank[i])]
        differing = next((v for v in verdicts if v != verdicts[0]), None)
        if differing is not None:
            conflicts.append({
                "case_id": case_id,
                "relationship_type": relationship_type,
                "outcomes": {iri: by_iri[iri] for iri in sorted(by_iri, key=lambda i: rank[i])},
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
            manifest.add(
                "case_entity_binds", row.pk, "nes_id", was, survivor, "repointed",
                case_id=row.case_id,
            )
            continue

        if row.notes and row.notes not in (sibling.notes or ""):
            sibling.notes = " | ".join(p for p in (sibling.notes, row.notes) if p)[:500]
        sibling.outcome = _better_outcome(sibling.outcome, row.outcome)
        sibling.save()
        # The deleted row's full field values are not copied here: auditlog captures
        # them on the delete (cases.apps registers CaseEntityRelationship).
        manifest.add(
            "case_entity_binds", row.pk, "nes_id", row.nes_id, survivor, "deduplicated",
            case_id=row.case_id,
        )
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

    for row in list(_link_candidates(retired, survivor)):
        rewritten, changed = rewrite_references(row.data, mapping)
        if not changed:
            continue
        service.update_entity(
            doc=rewritten,
            author_id=author_id,
            change_description=f"Reference repointed by merge {merge_id}",
            validate=False,
        )
        counts.repointed += changed
        manifest.add("entity_to_entity_links", row.iri, "data", retired[0], survivor, "repointed")
    return counts, manifest.entries


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
