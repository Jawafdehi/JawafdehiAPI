"""Live (on-write) unified-search indexing + evidence-visibility recompute for
Jawafdehi cases.

``post_save``/``pre_delete``/``post_delete`` on ``Case`` schedule best-effort work
on ``transaction.on_commit`` (plan §4.1). The ``pre_delete`` hook snapshots the
referenced material IRIs so a HARD delete (queryset/admin bulk-delete) can still
demote orphaned evidence in ``post_delete``, where the pk and join rows are gone:

* Search indexing — the CASE-ONLY-PUBLISHED rule lives in the indexer:
  ``search_index.index(case)`` upserts a PUBLISHED case and DELETES the doc for
  any non-published state — so a case that leaves PUBLISHED (or is soft-deleted to
  CLOSED) is evicted from the all-public index.
* Evidence-visibility recompute — a case's referenced Materials derive their
  ``visibility`` from the MAX over their referring case states (ADR: cases own no
  documents). Wiring this to the model ``post_save`` (rather than only the DRF
  view) means EVERY state change recomputes evidence visibility — Django admin,
  management commands, ``Case.publish()/submit()/delete()``, and shell writes
  included — closing the leak where a case demoted outside the API left its
  evidence publicly LISTED.
"""

from __future__ import annotations

from django.db import transaction
from django.db.models.signals import post_delete, post_save, pre_delete
from django.dispatch import receiver

from . import search_index
from .models import (
    Case,
    CaseCourtCaseReference,
    CaseEntityRelationship,
    CaseMaterialReference,
)

# Attribute we stash the pre-delete material-IRI snapshot on. Set in pre_delete
# (while the row + its CaseMaterialReference children still exist) and consumed in
# post_delete (by which point the pk is cleared and the join rows are CASCADE-gone).
_PENDING_IRIS_ATTR = "_pending_evidence_iris"
# Same idea for the court-case references (courts.search_visibility rule 3: a court
# case referenced by a PUBLISHED case is a hard SHOW).
_PENDING_COURTCASE_IRIS_ATTR = "_pending_courtcase_iris"
# And for the NES entity binds (entities.search_visibility: an entity is publicly
# searchable iff a PUBLISHED case cites it, so a case leaving PUBLISHED — including
# by hard delete — has to re-hide the entities nothing else cites).
_PENDING_ENTITY_IRIS_ATTR = "_pending_entity_iris"


@receiver(post_save, sender=Case, dispatch_uid="jawafdehi_case_search_index")
def _index_case(sender, instance, **kwargs):
    # index() applies the published gate: upsert if PUBLISHED, else delete.
    transaction.on_commit(lambda: search_index.index(instance))
    # Recompute the visibility of every material this case references, so a
    # demotion/promotion done via ANY write path (admin, command, model method)
    # can never leave a draft/closed case's evidence publicly LISTED — nor a
    # published case's evidence stuck PRIVATE. Best-effort, post-commit.
    iris = _referenced_material_iris(instance)
    transaction.on_commit(lambda: _recompute_evidence_iris(instance, iris))
    # A change to this case's PUBLISHED state flips the publish-link visibility of
    # the court cases it references (courts.search_visibility rule 3) — re-index
    # them so a just-published case surfaces its cited court cases live.
    cc_iris = _referenced_courtcase_iris(instance)
    transaction.on_commit(lambda: _refresh_referenced_courtcases(cc_iris))
    # A change to this case's PUBLISHED state also flips the public visibility of
    # every NES entity it binds (entities.search_visibility) — re-index them so a
    # just-published case surfaces the people it names, and an unpublished one
    # re-hides those no other published case cites.
    entity_iris = _bound_entity_iris(instance)
    transaction.on_commit(lambda: _refresh_bound_entities(entity_iris))


@receiver(pre_delete, sender=Case, dispatch_uid="jawafdehi_case_capture_evidence")
def _capture_case_evidence(sender, instance, **kwargs):
    # Snapshot the referenced material IRIs NOW — in post_delete the instance pk is
    # None and the CaseMaterialReference rows have been CASCADE-deleted, so the
    # reverse manager can no longer enumerate them (this is the F2 hard-delete leak).
    setattr(instance, _PENDING_IRIS_ATTR, _referenced_material_iris(instance))
    # Same snapshot for the court-case references (join rows CASCADE-gone in post_delete).
    setattr(instance, _PENDING_COURTCASE_IRIS_ATTR, _referenced_courtcase_iris(instance))
    # Same snapshot for the entity binds, for the same reason: CaseEntityRelationship
    # rows CASCADE away with the case, so post_delete can no longer enumerate which
    # entities just lost a published citation.
    setattr(instance, _PENDING_ENTITY_IRIS_ATTR, _bound_entity_iris(instance))


@receiver(post_delete, sender=Case, dispatch_uid="jawafdehi_case_search_delete")
def _delete_case(sender, instance, **kwargs):
    transaction.on_commit(lambda: search_index.delete(instance))
    # A hard-delete (Case.delete() soft-deletes to CLOSED, but a queryset/admin
    # hard-delete still fires post_delete) drops all referrers → the materials that
    # were only LISTED because of this case must demote. Use the pre_delete snapshot
    # (the join rows are already gone here), not a live query.
    iris = getattr(instance, _PENDING_IRIS_ATTR, [])
    transaction.on_commit(lambda: _recompute_evidence_iris(instance, iris))
    # Likewise re-evaluate the publish-link visibility of the court cases this
    # (now-deleted) case referenced, from the pre_delete snapshot.
    cc_iris = getattr(instance, _PENDING_COURTCASE_IRIS_ATTR, [])
    transaction.on_commit(lambda: _refresh_referenced_courtcases(cc_iris))
    # Likewise re-hide the entities this (now-deleted) case was the only published
    # citation for, from the pre_delete snapshot.
    entity_iris = getattr(instance, _PENDING_ENTITY_IRIS_ATTR, [])
    transaction.on_commit(lambda: _refresh_bound_entities(entity_iris))


def _referenced_material_iris(case) -> list[str]:
    """Material IRIs this case currently references (empty if pk is gone)."""
    if case.pk is None:
        return []
    return list(
        CaseMaterialReference.objects.filter(case=case).values_list(
            "material_iri", flat=True
        )
    )


def _referenced_courtcase_iris(case) -> list[str]:
    """Court-case IRIs this case currently references (empty if the pk is gone)."""
    if case.pk is None:
        return []
    return list(
        CaseCourtCaseReference.objects.filter(case=case).values_list(
            "courtcase_iri", flat=True
        )
    )


def _bound_entity_iris(case) -> list[str]:
    """NES entity IRIs this case currently binds (empty if the pk is gone)."""
    if case.pk is None:
        return []
    return list(
        CaseEntityRelationship.objects.filter(case=case)
        .values_list("nes_id", flat=True)
        .distinct()
    )


@receiver(
    post_save,
    sender=CaseEntityRelationship,
    dispatch_uid="jawafdehi_bind_entity_visibility",
)
@receiver(
    post_delete,
    sender=CaseEntityRelationship,
    dispatch_uid="jawafdehi_unbind_entity_visibility",
)
def _refresh_entity_on_bind_change(sender, instance, **kwargs):
    """Re-index one entity when a case bind is created or removed.

    This is what makes an archived entity un-archive with no operator step. It
    hangs off the BIND, not off ``Case``, because the bind-rewrite path
    (``cases.api_views._rewrite_entity_binds``) deletes and recreates
    ``CaseEntityRelationship`` rows WITHOUT saving the parent ``Case`` — so the
    ``Case`` ``post_save`` above never fires for the most common way binds change.
    Django's ``QuerySet.delete()`` fetches the rows and emits ``post_delete`` per
    object, so the whole-list replace is covered.

    A rewrite therefore emits one re-index per deleted row and one per created
    row. That is redundant by design: the upsert is idempotent, a PATCH is a
    human-scale event, and de-duplicating across a transaction would need
    bookkeeping that could itself drop an IRI.
    """
    iri = getattr(instance, "nes_id", None)
    if not iri:
        return
    transaction.on_commit(lambda: _refresh_bound_entities([iri]))


def _refresh_bound_entities(iris) -> None:
    """Re-index the given NES entities so their ``case_count`` matches the binds.

    ``case_count`` is the public visibility gate (``entities.search_visibility``):
    it is derived from the binds, so nothing in the ``StoredEntity`` row changes
    when a case is published or bound and no entity signal fires. This is the
    only path that tells the entity index a citation appeared or vanished.

    Cross-app / cross-DB (entities → ``nes``) and best-effort, mirroring
    ``_refresh_referenced_courtcases``: a failure must not break the case write,
    and ``reconcile_entity_visibility`` is the periodic backstop.
    """
    if not iris:
        return
    import logging

    logger = logging.getLogger(__name__)
    try:
        from entities import search_index as entities_search_index
        from entities.search_visibility import clear_cache, entity_case_count
    except Exception:  # noqa: BLE001 — entities/opensearch stack optional in some contexts
        return
    # The published-bind set just changed → drop the cached copy so the recomputed
    # counts read the new state (and so do other processes, via Redis).
    clear_cache()
    for iri in dict.fromkeys(iris):
        try:
            entities_search_index.index_by_iri(
                iri, case_count=entity_case_count(iri)
            )
        except Exception:  # noqa: BLE001 — best-effort; the reconcile backstops
            logger.exception("entity visibility reindex failed for %s", iri)


def _refresh_referenced_courtcases(iris) -> None:
    """Re-index the referenced court cases so the publish-link visibility rule
    (``courts.search_visibility`` rule 3) is applied live: a just-published case
    surfaces its cited court cases; an unpublished/deleted one re-hides those not
    otherwise public.

    Cross-app / cross-DB (courts → ``ngm``) and best-effort — a failure must not
    break the case write; ``reindex_courtcases`` is the periodic backstop.
    """
    if not iris:
        return
    import logging

    logger = logging.getLogger(__name__)
    try:
        from jawafdehi_shared.entities.ids import parse_courtcase_iri

        from courts import search_index as courts_search_index
        from courts.models import CourtCase
        from courts.search_visibility import clear_published_cache
    except Exception:  # noqa: BLE001 — courts/opensearch stack optional in some contexts
        return
    # The set of PUBLISHED-referenced court cases just changed → drop the cache so
    # the recomputed visibility reads the new state.
    clear_published_cache()
    for iri in iris:
        try:
            ref = parse_courtcase_iri(iri)
            # The IRI lowercases the case_number; the stored column is uppercase
            # (normalize_case_number uppercases), so upper() round-trips it back to
            # the natural key (index-friendly exact match).
            obj = CourtCase.objects.filter(
                court_id=ref.court, case_number=ref.case_number.upper()
            ).first()
            if obj is not None:
                courts_search_index.index_or_evict(obj)
        except Exception:  # noqa: BLE001 — best-effort; reindex_courtcases backstops
            logger.exception("courtcase publish-link reindex failed for %s", iri)


def _recompute_evidence_iris(case, iris) -> None:
    """Recompute visibility for the given material IRIs; never raise.

    Cross-app, in-process (materials → ngm DB, cases → default). A failure here
    must not break the case write, so it is logged and swallowed — the
    ``recompute_material_visibility`` management command is the periodic backstop.
    """
    if not iris:
        return
    import logging

    from materials.visibility import recompute_material_visibility

    logger = logging.getLogger(__name__)
    slug = getattr(case, "slug", getattr(case, "pk", "?"))
    # Isolate per IRI: one material's recompute failing must not skip the rest
    # (they're independent). Best-effort — the reconciler command backstops any
    # that still slip through.
    for iri in iris:
        try:
            recompute_material_visibility(iri)
        except Exception:  # noqa: BLE001 — best-effort; reconciler command backstops
            logger.exception(
                "evidence-visibility recompute failed for case %s material %s",
                slug,
                iri,
            )
