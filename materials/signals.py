"""What happens to a Material the moment it is written.

Two things ride on ``post_save``, and both are best-effort on
``transaction.on_commit``: the unified-search index upsert (plan §4.1), and
queueing the document to be read.

**Why a signal rather than a call at each write path.** A material enters the
system through the upload endpoint, five scrapers, the court importer, the seed
commands and any PATCH that attaches a file — and before this, exactly ONE of
those asked for the document to be converted (the upload endpoint,
``views.py``). That is a large part of why ~94% of the archive has never been
read: not that extraction failed, but that nothing asked. Every one of those
paths funnels through ``upsert_single_source_material``'s ``update_or_create``,
so a receiver here covers all of them and the next one nobody has written yet.

**What a signal cannot cover, ever.** ``bulk_create``, ``.update()``, raw SQL
and a restored backup do not fire ``post_save``. So this is the fast path, not
the guarantee: ``extract_document_content`` run periodically is the
reconciliation that finds whatever the signal could not see, and its output is
what turns "nothing is missing" from a claim into a number.
"""

from __future__ import annotations

import logging

from django.db import router, transaction
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from . import search_index
from .models import Material, Visibility

logger = logging.getLogger("materials.signals")


def _material_db(instance) -> str:
    """The alias the row was written on — the one whose commit we must wait for.

    ``transaction.on_commit`` resolves against a specific connection, defaulting
    to ``default``. ``Material`` lives on ``ngm``, so an unqualified ``on_commit``
    asks the WRONG connection whether a transaction is open: inside
    ``atomic(using="ngm")`` the ``default`` connection is still in autocommit, so
    Django runs the callback IMMEDIATELY — indexing a document before (or instead
    of) its row becoming durable, and doing the OpenSearch round-trip while the
    row lock is still held.
    """
    return instance._state.db or router.db_for_write(Material, instance=instance)


@receiver(post_save, sender=Material, dispatch_uid="ngm_material_search_index")
def _index_material(sender, instance, **kwargs):
    # A soft-delete is an ORM save (is_deleted=True); evict from the index rather
    # than re-indexing a row that is no longer on the read plane. Likewise, only
    # LISTED materials are searchable: UNLISTED (in-review-only) and PRIVATE
    # (draft-only) case-source materials must be evicted so anon search can't
    # surface a draft case's evidence (ADR: cases own no documents). NGM-native
    # materials default to LISTED, so their indexing is unchanged.
    listed = getattr(instance, "visibility", Visibility.LISTED) == Visibility.LISTED
    if getattr(instance, "is_deleted", False) or not listed:
        transaction.on_commit(lambda: search_index.delete(instance), using=_material_db(instance))
    else:
        transaction.on_commit(lambda: search_index.index(instance), using=_material_db(instance))


@receiver(post_delete, sender=Material, dispatch_uid="ngm_material_search_delete")
def _delete_material(sender, instance, **kwargs):
    transaction.on_commit(lambda: search_index.delete(instance), using=_material_db(instance))


@receiver(post_save, sender=Material, dispatch_uid="ngm_material_extract")
def _extract_material(sender, instance, **kwargs):
    """Queue a document for reading when a material is written.

    Deliberately cheap and deliberately silent about its own failures: this runs
    inside every material write in the platform, and a queue that is down must
    slow nothing and break nothing. The sweep picks up whatever is missed.

    Four guards, in order of how often they fire:

    * a soft-deleted row is a takedown — do not read what was withdrawn;
    * no extractable document (the common case: most materials are a scraped
      page, not a PDF) — nothing to read;
    * content already stored for these exact bytes — nothing to do. The
      comparison is the file's recorded SHA-256 against the extraction's
      ``dataset_revision``, so a re-uploaded or corrected file is re-read while
      an unchanged one is not, with no network call and no re-parse;
    * the queue itself dedups on the IRI, so a material saved five times in one
      request produces one job.
    """
    if getattr(instance, "is_deleted", False):
        return
    transaction.on_commit(
        lambda: _enqueue_extract(instance), using=_material_db(instance)
    )


def _enqueue_extract(instance) -> None:
    from . import extract as material_extract

    data = instance.data or {}
    if not material_extract.is_extractable(data):
        return
    if not material_extract.content_is_stale(instance.pk, data):
        return
    try:
        material_extract.enqueue_material_extract(
            # Ahead of the sweep's backfill: somebody just wrote this document,
            # so somebody is more likely to be about to read it.
            instance.pk,
            priority=material_extract.PRIORITY_ON_WRITE,
        )
    except Exception as exc:  # noqa: BLE001 - a queue outage must not fail a write
        logger.warning("could not queue extraction for %s: %s", instance.pk, exc)
