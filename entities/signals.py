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
from .search_geography import LOCATION_PREFIX


@receiver(post_save, sender=StoredEntity, dispatch_uid="nes_entity_search_index")
def _index_entity(sender, instance, **kwargs):
    _invalidate_place_graph(instance)
    # A soft-delete is an ORM save (is_deleted=True), so evict from the index
    # here rather than re-indexing a row that is no longer on the read plane.
    if getattr(instance, "is_deleted", False):
        transaction.on_commit(lambda: search_index.delete(instance), using="nes")
    else:
        transaction.on_commit(lambda: search_index.index(instance), using="nes")


@receiver(post_delete, sender=StoredEntity, dispatch_uid="nes_entity_search_delete")
def _delete_entity(sender, instance, **kwargs):
    _invalidate_place_graph(instance)
    transaction.on_commit(lambda: search_index.delete(instance), using="nes")


def _invalidate_place_graph(instance) -> None:
    """Drop the cached place graph when a LOCATION entity is written.

    ``entities.search_geography`` caches ``{place IRI: (district, province)}``
    for an hour because the federal structure does not move. The one thing that
    can make it wrong is a write to a place itself — a local unit gaining its
    ``containedInPlace``, a district's p-code being corrected — so the
    invalidation is scoped to exactly that, and the other 203k entities pay
    nothing.

    Dropped TWICE, before and after commit, because each alone has a hole. The
    pre-commit delete covers an invalidation that never gets to run — a rolled
    back outer atomic, a path that does not reach ``on_commit``. The post-commit
    delete covers the race the pre-commit one opens: a concurrent reader between
    the delete and the commit re-populates the cache from the OLD rows and pins
    it for the full hour, silently undoing the invalidation. Two deletes of a
    838-row map cost nothing; being wrong for an hour does.
    """
    iri = getattr(instance, "iri", None)
    if not isinstance(iri, str) or LOCATION_PREFIX not in iri:
        return
    try:
        from .search_geography import clear_cache

        clear_cache()
        transaction.on_commit(clear_cache, using="nes")
    except Exception:  # noqa: BLE001 — best-effort; the TTL bounds the staleness.
        pass
