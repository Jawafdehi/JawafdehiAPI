"""Visibility recompute for Materials (ADR: cases own no documents).

A material's cached ``visibility`` is derived from its caseworker-controlled
``visibility_policy`` (see ``materials.models.Policy``). This module is the
single place that maps a policy (plus, for ``CASE_GATED``, the states of the
cases that cite the material) → a ``Visibility`` and writes it back:

* ``PUBLIC``     → ``LISTED`` (a corpus document is public on its own merits, so
  a DRAFT case that cites it can no longer hide it — the bug this fixes).
* ``PRIVATE``    → ``PRIVATE`` (an absolute withhold).
* ``CASE_GATED`` → the MAX over the states of every case that references the
  material via ``CaseMaterialReference`` (YouTube-unlisted semantics:
  PUBLISHED→LISTED, IN_REVIEW→UNLISTED, DRAFT/CLOSED/none→PRIVATE).

Cross-app, in-process, NO cross-DB FK: we import the cases app's models and
query ``CaseMaterialReference`` by ``material_iri`` string; the states query runs
ONLY for a ``CASE_GATED`` material.

The recompute TRIGGERS (case publish/review/unpublish/delete, evidence add/
remove) live on the case-side transition path (wired in the evidence-cutover
slice) and only call ``recompute_material_visibility`` — the policy logic lives
here, so those trigger sites are unchanged. A periodic ``recompute_all``
reconciler is the backstop against a missed trigger (a missed demotion is a
leak; a missed promotion hides a public doc).
"""

from __future__ import annotations

import logging

from .models import Material, Policy, Visibility

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


def visibility_for_policy(policy: str, states_fn) -> Visibility:
    """Map a caseworker ``Policy`` → a cached ``Visibility``.

    ``PUBLIC`` → LISTED and ``PRIVATE`` → PRIVATE are fixed and never touch the
    DB. ``CASE_GATED`` defers to the MAX over the citing cases' states, which
    ``states_fn()`` supplies lazily — so the referring-states query runs ONLY for
    a case-gated material. An unknown policy is treated as ``CASE_GATED``
    (conservative: never more public than the citing cases allow).
    """
    if policy == Policy.PUBLIC:
        return Visibility.LISTED
    if policy == Policy.PRIVATE:
        return Visibility.PRIVATE
    return visibility_for_states(states_fn())


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
    """Recompute + persist the visibility of one Material from its policy.

    Returns the new ``Visibility`` (written only if changed), or ``None`` if no
    live Material row exists for the IRI. A soft-deleted material is left alone.
    """
    material = Material.objects.filter(pk=material_iri, is_deleted=False).first()
    if material is None:
        return None
    new_visibility = visibility_for_policy(
        material.visibility_policy,
        lambda: _referring_case_states(material_iri),
    )
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
    """Reconciler backstop: recompute EVERY live Material from its policy.

    Returns the number of materials whose visibility CHANGED. Safe to run
    periodically (mirrors the casework reaper) to heal any visibility drift from
    a missed trigger.

    Bulk, not per-material: the naive loop over ``recompute_material_visibility``
    is O(N) round-trips. The DBs are routed independently (no cross-DB JOIN), so
    instead: (1) one query for all ``(material_iri, case_state)`` pairs, (2) one
    query for every live Material, (3) compute in memory + ``bulk_update`` the
    changed rows. A FULL pass (not a referenced-only scan) is what settles the
    policies that have no ``CaseMaterialReference`` at all — a ``PRIVATE``-policy
    withhold and an unreferenced ``CASE_GATED`` upload must both resolve to
    PRIVATE even though no case row points at them. The materials table is the
    curated document set (hundreds of rows), so the full scan is cheap. Because
    ``bulk_update`` does NOT fire ``post_save``, the search-index eviction/index
    that the signal normally performs is done explicitly here for changed rows —
    else a material demoted to non-LISTED by the reconciler would linger in public
    search (a leak).
    """
    from collections import defaultdict

    from django.utils import timezone

    from cases.models import CaseMaterialReference

    # 1. All referring case states per material IRI (single query, cases DB).
    iri_to_states: dict[str, list[str]] = defaultdict(list)
    for iri, state in CaseMaterialReference.objects.values_list(
        "material_iri", "case__state"
    ):
        if iri:
            iri_to_states[iri].append(state)

    # 2. Every live Material (single query, ngm DB).
    materials = list(Material.objects.filter(is_deleted=False))

    # 3. Compute in memory per policy; bulk_update only the changed rows. The
    #    states lambda binds ``mat`` per-iteration (default-arg) and is consulted
    #    only for a CASE_GATED policy.
    changed: list[Material] = []
    now = timezone.now()
    for mat in materials:
        new_visibility = visibility_for_policy(
            mat.visibility_policy,
            lambda mat=mat: iri_to_states.get(mat.iri, []),
        )
        if mat.visibility != new_visibility:
            mat.visibility = new_visibility
            mat.updated_at = now
            changed.append(mat)

    if changed:
        Material.objects.bulk_update(changed, ["visibility", "updated_at"])
        # bulk_update bypasses post_save, so reconcile the search index by hand:
        # LISTED → (re)index, everything else → evict. Mirrors materials/signals.
        from . import search_index

        for mat in changed:
            if mat.visibility == Visibility.LISTED:
                search_index.index(mat)
            else:
                search_index.delete(mat)

    return len(changed)
