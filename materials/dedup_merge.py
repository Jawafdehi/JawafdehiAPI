"""Stage 2 merge for the jawafdehi case-upload deduplication.

Given a jawafdehi material that duplicates a canonical corpus material (decided by
the Stage 1 detect in ``materials.dedup`` + the command's DB existence check), this
module *plans* and *applies* the merge:

  1. Repoint every ``CaseMaterialReference`` from the jawafdehi IRI to the canonical
     IRI, preserving the case-specific ``additional_details`` note and ``ordinal``.
  2. On a COLLISION — the case already references the canonical (there is a
     ``unique_case_material_reference`` constraint on ``(case, material_iri)``, so a
     naive repoint would raise) — merge the note into the existing canonical ref and
     DELETE the jawafdehi ref instead of repointing.
  3. Soft-delete the jawafdehi material via the sanctioned model ``save()`` path
     (``is_deleted=True``) so the ``post_save`` search-index eviction signal fires.

Deliberately NOT done: recomputing the canonical's visibility. The canonical is
NGM-native public corpus (``ciaa_press_release`` / ``court_order``), LISTED and public
independent of any case. ``recompute_material_visibility`` computes visibility as the
MAX over referring case states with no NGM-native guard, so recomputing would DEMOTE a
public press release to PRIVATE/UNLISTED the moment a draft/in-review case referenced
it — hiding public data. There is no leak in leaving it LISTED: the document was
already public, and the draft case itself stays hidden (case evidence links are shown
only for published cases).

Cross-DB: references live on the ``default`` DB, materials on ``ngm``; no atomic
transaction spans both. The per-material ref rewrite is wrapped in a ``default``-DB
transaction; the material soft-delete is a separate ``ngm`` write. The whole operation
is idempotent — a soft-deleted material drops out of the detect pass, and a partial run
(refs moved, material still live) re-detects and completes on re-run.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.db import transaction

from .models import Material


@dataclass(frozen=True)
class MergePlan:
    """Read-only preview of what :func:`apply_merge` would do (dry-run)."""

    jawafdehi_iri: str
    canonical_iri: str
    #: Case slugs whose reference would be repointed jawafdehi -> canonical.
    refs_to_repoint: list[str]
    #: Case slugs that already reference the canonical (collision -> dedupe).
    collisions: list[str]


@dataclass(frozen=True)
class MergeResult:
    """What :func:`apply_merge` actually did."""

    jawafdehi_iri: str
    canonical_iri: str
    refs_repointed: int
    refs_deduped: int
    soft_deleted: bool


def plan_merge(jawafdehi_iri: str, canonical_iri: str) -> MergePlan:
    """Read-only: classify each referencing case as repoint vs collision-dedupe."""
    from cases.models import CaseMaterialReference

    repoint: list[str] = []
    collisions: list[str] = []
    refs = list(
        CaseMaterialReference.objects.filter(
            material_iri=jawafdehi_iri
        ).select_related("case")
    )
    if refs:
        # One query for all colliding cases instead of one per ref (avoid N+1).
        colliding = set(
            CaseMaterialReference.objects.filter(
                case_id__in=[r.case_id for r in refs], material_iri=canonical_iri
            ).values_list("case_id", flat=True)
        )
        for ref in refs:
            (collisions if ref.case_id in colliding else repoint).append(ref.case.slug)
    return MergePlan(jawafdehi_iri, canonical_iri, repoint, collisions)


def apply_merge(jawafdehi_iri: str, canonical_iri: str) -> MergeResult:
    """Repoint references, dedupe collisions, soft-delete the jawafdehi material.

    Idempotent: re-running after a full or partial merge is a no-op / completes the
    remainder. Does NOT touch the canonical's visibility (see module docstring).
    """
    from cases.models import CaseMaterialReference

    repointed = 0
    deduped = 0
    with transaction.atomic(using="default"):
        refs = list(
            CaseMaterialReference.objects.select_for_update().filter(
                material_iri=jawafdehi_iri
            )
        )
        for ref in refs:
            # Lock the canonical ref too so a concurrent merge can't race the
            # collision check (no-op on sqlite; real on Postgres).
            existing = (
                CaseMaterialReference.objects.select_for_update()
                .filter(case_id=ref.case_id, material_iri=canonical_iri)
                .first()
            )
            if existing is not None:
                _merge_note(existing, ref)
                ref.delete()
                deduped += 1
            else:
                ref.material_iri = canonical_iri
                ref.save(update_fields=["material_iri"])
                repointed += 1

    soft_deleted = False
    row = Material.objects.filter(pk=jawafdehi_iri, is_deleted=False).first()
    if row is not None:
        row.is_deleted = True
        # save() (not .update()) so the post_save signal evicts it from search.
        row.save(update_fields=["is_deleted", "updated_at"])
        soft_deleted = True

    return MergeResult(jawafdehi_iri, canonical_iri, repointed, deduped, soft_deleted)


def _merge_note(canonical_ref, jawafdehi_ref) -> None:
    """Fold the jawafdehi ref's case-note into the canonical ref (no data loss)."""
    note = (jawafdehi_ref.additional_details or "").strip()
    if not note:
        return
    existing = (canonical_ref.additional_details or "").strip()
    # Match on whole lines, not raw substring — a note that merely appears inside
    # unrelated text (a shared case number, a common phrase) must not be dropped.
    if note in existing.splitlines():
        return
    canonical_ref.additional_details = f"{existing}\n{note}".strip()
    canonical_ref.save(update_fields=["additional_details"])
