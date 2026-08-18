"""Live (on-write) unified-search indexing for NES entities.

``post_save``/``post_delete`` on ``StoredEntity`` schedule a best-effort index
upsert/delete on ``transaction.on_commit`` so we never index a row that rolled
back (plan §4.1). The indexer itself swallows OpenSearch errors, so the write
path is never broken by an index hiccup.
"""

from __future__ import annotations

from django.db import transaction
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from . import search_index
from .models import StoredEntity


@receiver(post_save, sender=StoredEntity, dispatch_uid="nes_entity_search_index")
def _index_entity(sender, instance, **kwargs):
    # A soft-delete is an ORM save (is_deleted=True), so evict from the index
    # here rather than re-indexing a row that is no longer on the read plane.
    if getattr(instance, "is_deleted", False):
        transaction.on_commit(lambda: search_index.delete(instance), using="nes")
    else:
        transaction.on_commit(lambda: search_index.index(instance), using="nes")


@receiver(post_delete, sender=StoredEntity, dispatch_uid="nes_entity_search_delete")
def _delete_entity(sender, instance, **kwargs):
    transaction.on_commit(lambda: search_index.delete(instance), using="nes")
