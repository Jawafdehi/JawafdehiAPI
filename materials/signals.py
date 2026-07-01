"""Live (on-write) unified-search indexing for NGM materials.

``post_save``/``post_delete`` on ``Material`` schedule a best-effort index
upsert/delete on ``transaction.on_commit`` (plan §4.1).
"""

from __future__ import annotations

from django.db import transaction
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from . import search_index
from .models import Material


@receiver(post_save, sender=Material, dispatch_uid="ngm_material_search_index")
def _index_material(sender, instance, **kwargs):
    # A soft-delete is an ORM save (is_deleted=True); evict from the index rather
    # than re-indexing a row that is no longer on the read plane.
    if getattr(instance, "is_deleted", False):
        transaction.on_commit(lambda: search_index.delete(instance))
    else:
        transaction.on_commit(lambda: search_index.index(instance))


@receiver(post_delete, sender=Material, dispatch_uid="ngm_material_search_delete")
def _delete_material(sender, instance, **kwargs):
    transaction.on_commit(lambda: search_index.delete(instance))
