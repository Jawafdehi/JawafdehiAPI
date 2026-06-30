"""Live (on-write) unified-search indexing for NGM court cases.

``post_save``/``post_delete`` on ``CourtCase`` schedule a best-effort index
upsert/delete on ``transaction.on_commit`` (plan §4.1). A change to a party row
(``CaseEntity``) also re-indexes its parent ``CourtCase`` so party-name/IRI
changes stay reflected (the courtcase doc folds party names into body/keywords).
"""

from __future__ import annotations

from django.db import transaction
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from . import search_index
from .models import CaseEntity, CourtCase


@receiver(post_save, sender=CourtCase, dispatch_uid="ngm_courtcase_search_index")
def _index_courtcase(sender, instance, **kwargs):
    transaction.on_commit(lambda: search_index.index(instance))


@receiver(post_delete, sender=CourtCase, dispatch_uid="ngm_courtcase_search_delete")
def _delete_courtcase(sender, instance, **kwargs):
    transaction.on_commit(lambda: search_index.delete(instance))


def _reindex_parent_courtcase(instance):
    """Re-index the CourtCase a CaseEntity belongs to (best-effort)."""
    try:
        case = CourtCase.objects.filter(
            court_id=instance.court_id, case_number=instance.case_number
        ).first()
    except Exception:  # noqa: BLE001
        case = None
    if case is not None:
        search_index.index(case)


@receiver(post_save, sender=CaseEntity, dispatch_uid="ngm_caseentity_reindex")
@receiver(post_delete, sender=CaseEntity, dispatch_uid="ngm_caseentity_reindex_del")
def _reindex_on_party_change(sender, instance, **kwargs):
    transaction.on_commit(lambda: _reindex_parent_courtcase(instance))
