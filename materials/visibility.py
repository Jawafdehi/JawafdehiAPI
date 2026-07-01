"""Derived-visibility recompute for case-source Materials (ADR: cases own no
documents).

A Material that originated as case evidence has no intrinsic publicness — its
visibility is the MAX over the states of every case that references it via
``CaseMaterialReference`` (YouTube-unlisted semantics; see
``materials.models.Visibility``). This module is the single place that maps
"the set of referring case states" → a ``Visibility`` and writes it back.

Cross-app, in-process, NO cross-DB FK: we import the cases app's models and
query ``CaseMaterialReference`` by ``material_iri`` string. Materials with NO
case referrers keep their current visibility (NGM-native court materials stay
LISTED by default); only materials that ARE referenced get (re)computed.

The recompute TRIGGERS (case publish/review/unpublish/delete, evidence add/
remove) live on the case-side transition path (wired in the evidence-cutover
slice). A periodic ``recompute_all`` reconciler is the backstop against a missed
trigger (a missed demotion is a leak; a missed promotion hides a public doc).
"""

from __future__ import annotations

import logging

from .models import Material, Visibility

logger = logging.getLogger(__name__)

#: Case state → the visibility tier a single referrer in that state confers.
#: Only PUBLISHED is public (LISTED); IN_REVIEW is reachable-but-unlisted; DRAFT
#: and CLOSED are private. CLOSED is the case SOFT-DELETE tombstone (Case has no
#: is_deleted flag — Case.delete() sets state=CLOSED and all reads exclude it),
#: so a CLOSED referrer must NOT keep a material public.
_STATE_VISIBILITY = {
    "PUBLISHED": Visibility.LISTED,
    "IN_REVIEW": Visibility.UNLISTED,
    "DRAFT": Visibility.PRIVATE,
    "CLOSED": Visibility.PRIVATE,
}

#: Rank for taking the MAX across referrers (higher = more public).
_RANK = {Visibility.PRIVATE: 0, Visibility.UNLISTED: 1, Visibility.LISTED: 2}


def visibility_for_states(states) -> Visibility:
    """MAX visibility conferred by an iterable of case-state strings.

    Empty (no referrers) → PRIVATE: a source cited by nothing is not public.
    Unknown states are treated as PRIVATE (conservative — never leak).
    """
    best = Visibility.PRIVATE
    for state in states:
        conferred = _STATE_VISIBILITY.get(state, Visibility.PRIVATE)
        if _RANK[conferred] > _RANK[best]:
            best = conferred
            if best == Visibility.LISTED:
                break
    return best


def _referring_case_states(material_iri: str) -> list[str]:
    """States of all cases referencing ``material_iri`` as evidence.

    Cases have no is_deleted flag — a soft-deleted case is CLOSED (see
    ``_STATE_VISIBILITY``), which already confers PRIVATE, so CLOSED referrers are
    included and simply don't keep the material public.
    """
    from cases.models import CaseMaterialReference

    return list(
        CaseMaterialReference.objects.filter(material_iri=material_iri)
        .values_list("case__state", flat=True)
    )


def recompute_material_visibility(material_iri: str) -> Visibility | None:
    """Recompute + persist the visibility of one case-source Material.

    Returns the new ``Visibility`` (written only if changed), or ``None`` if no
    live Material row exists for the IRI. A soft-deleted material is left alone.
    """
    material = Material.objects.filter(pk=material_iri, is_deleted=False).first()
    if material is None:
        return None
    new_visibility = visibility_for_states(_referring_case_states(material_iri))
    if material.visibility != new_visibility:
        material.visibility = new_visibility
        material.save(update_fields=["visibility", "updated_at"])
        logger.info(
            "material %s visibility → %s", material_iri, new_visibility
        )
    return new_visibility


def recompute_for_case(case) -> None:
    """Recompute visibility for every material a case references.

    Call on any case state transition (publish/review/unpublish/close) and on
    evidence add/remove. Uses the case's current ``material_references``.
    """
    iris = list(
        case.material_references.values_list("material_iri", flat=True)
    )
    for iri in iris:
        recompute_material_visibility(iri)


def recompute_all() -> int:
    """Reconciler backstop: recompute every Material that any case references.

    Returns the number of materials recomputed. Safe to run periodically (mirrors
    the casework reaper) to heal any visibility drift from a missed trigger.
    """
    from cases.models import CaseMaterialReference

    iris = (
        CaseMaterialReference.objects.values_list("material_iri", flat=True).distinct()
    )
    count = 0
    for iri in iris:
        if recompute_material_visibility(iri) is not None:
            count += 1
    return count
