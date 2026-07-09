"""Live (on-write) unified-search indexing + evidence-visibility recompute for
Jawafdehi cases.

``post_save``/``post_delete`` on ``Case`` schedule best-effort work on
``transaction.on_commit`` (plan §4.1):

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
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from . import search_index
from .models import Case


@receiver(post_save, sender=Case, dispatch_uid="jawafdehi_case_search_index")
def _index_case(sender, instance, **kwargs):
    # index() applies the published gate: upsert if PUBLISHED, else delete.
    transaction.on_commit(lambda: search_index.index(instance))
    # Recompute the visibility of every material this case references, so a
    # demotion/promotion done via ANY write path (admin, command, model method)
    # can never leave a draft/closed case's evidence publicly LISTED — nor a
    # published case's evidence stuck PRIVATE. Best-effort, post-commit.
    transaction.on_commit(lambda: _recompute_case_evidence_visibility(instance))


@receiver(post_delete, sender=Case, dispatch_uid="jawafdehi_case_search_delete")
def _delete_case(sender, instance, **kwargs):
    transaction.on_commit(lambda: search_index.delete(instance))
    # A hard-delete (Case.delete() soft-deletes to CLOSED, but a queryset/admin
    # hard-delete still fires post_delete) also drops referrers → recompute.
    transaction.on_commit(lambda: _recompute_case_evidence_visibility(instance))


def _recompute_case_evidence_visibility(case) -> None:
    """Recompute visibility for the case's referenced materials; never raise.

    Cross-app, in-process (materials → ngm DB, cases → default). A failure here
    must not break the case write, so it is logged and swallowed — the periodic
    ``recompute_all`` reconciler is the backstop.
    """
    import logging

    from materials.visibility import recompute_for_case

    try:
        recompute_for_case(case)
    except Exception:  # noqa: BLE001 — best-effort; reconciler is the backstop
        logging.getLogger(__name__).exception(
            "evidence-visibility recompute failed for case %s",
            getattr(case, "slug", getattr(case, "pk", "?")),
        )
