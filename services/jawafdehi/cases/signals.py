"""Live (on-write) unified-search indexing for Jawafdehi cases.

``post_save``/``post_delete`` on ``Case`` schedule a best-effort index update on
``transaction.on_commit`` (plan §4.1). The CASE-ONLY-PUBLISHED rule lives in the
indexer: ``search_index.index(case)`` upserts a PUBLISHED case and DELETES the
doc for any non-published state — so a case that leaves PUBLISHED (or is
soft-deleted to CLOSED) is evicted from the all-public index.
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


@receiver(post_delete, sender=Case, dispatch_uid="jawafdehi_case_search_delete")
def _delete_case(sender, instance, **kwargs):
    transaction.on_commit(lambda: search_index.delete(instance))
