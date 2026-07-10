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
from .models import Case, CaseMaterialReference

# Attribute we stash the pre-delete material-IRI snapshot on. Set in pre_delete
# (while the row + its CaseMaterialReference children still exist) and consumed in
# post_delete (by which point the pk is cleared and the join rows are CASCADE-gone).
_PENDING_IRIS_ATTR = "_pending_evidence_iris"


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


@receiver(pre_delete, sender=Case, dispatch_uid="jawafdehi_case_capture_evidence")
def _capture_case_evidence(sender, instance, **kwargs):
    # Snapshot the referenced material IRIs NOW — in post_delete the instance pk is
    # None and the CaseMaterialReference rows have been CASCADE-deleted, so the
    # reverse manager can no longer enumerate them (this is the F2 hard-delete leak).
    setattr(instance, _PENDING_IRIS_ATTR, _referenced_material_iris(instance))


@receiver(post_delete, sender=Case, dispatch_uid="jawafdehi_case_search_delete")
def _delete_case(sender, instance, **kwargs):
    transaction.on_commit(lambda: search_index.delete(instance))
    # A hard-delete (Case.delete() soft-deletes to CLOSED, but a queryset/admin
    # hard-delete still fires post_delete) drops all referrers → the materials that
    # were only LISTED because of this case must demote. Use the pre_delete snapshot
    # (the join rows are already gone here), not a live query.
    iris = getattr(instance, _PENDING_IRIS_ATTR, [])
    transaction.on_commit(lambda: _recompute_evidence_iris(instance, iris))


def _referenced_material_iris(case) -> list[str]:
    """Material IRIs this case currently references (empty if pk is gone)."""
    if case.pk is None:
        return []
    return list(
        CaseMaterialReference.objects.filter(case=case).values_list(
            "material_iri", flat=True
        )
    )


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
