"""``reindex_entities`` — bulk-(re)index NES entities into ``nes-entities``.

Streams the SEARCHABLE ``StoredEntity`` set through the entity indexer's
``build_doc`` into OpenSearch. ``--rebuild`` drops + recreates the index first
(mapping changes). The DB router pins ``StoredEntity`` reads to the ``nes`` DB
automatically.

Only ``is_deleted=False`` rows are indexed — the SAME gate the live ``post_save``
signal applies (``entities.signals``: a soft-deleted row is EVICTED, not indexed,
because DELETE flips the flag rather than removing the row). Streaming ``.all()``
here instead RESURRECTS every tombstone: an entity deleted from the read plane
comes back in anonymous unified search on the next reindex. Mirrors the identical
gate in ``reindex_materials``.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from jawafdehi_shared.search.opensearch import ENTITY_INDEX
from jawafdehi_shared.search.reindex import reindex, summary
from entities import search_index
from entities.models import StoredEntity


class Command(BaseCommand):
    help = "Bulk-(re)index NES entities into the nes-entities OpenSearch index."

    def add_arguments(self, parser):
        parser.add_argument(
            "--rebuild",
            action="store_true",
            help="Drop and recreate the index before reindexing.",
        )

    def handle(self, *args, **options):
        # Mirror the live indexer's gate (entities.signals): index ONLY the rows
        # still on the read plane. Without this a reindex re-adds soft-deleted
        # entities to public search — the exact rows the signal evicted.
        def changed(since):
            """``(iri, entity_or_None)`` for everything written during the build.

            Unfiltered on is_deleted so a DELETE landing mid-build arrives as a
            TOMBSTONE. NES delete is a soft delete, so without this the swap
            resurrects the very tombstone the signal evicted — the failure this
            command's is_deleted gate already exists to prevent, reintroduced
            through a different door.

            ``updated_at`` is NOT auto_now here; entities.persistence sets it on
            every re-publish, which is exactly the write we need to catch.
            """
            for e in StoredEntity.objects.filter(updated_at__gte=since).iterator():
                yield e.iri, (None if e.is_deleted else e)

        result = reindex(
            index=ENTITY_INDEX,
            records=StoredEntity.objects.filter(is_deleted=False).iterator(),
            build_doc=search_index.build_doc,
            rebuild=options["rebuild"],
            catchup=changed,
        )
        self.stdout.write(self.style.SUCCESS(summary("nes-entities", result)))
