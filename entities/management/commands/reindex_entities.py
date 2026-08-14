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
        def stream(since=None):
            qs = StoredEntity.objects.filter(is_deleted=False)
            if since:
                # updated_at is NOT auto_now here — entities.persistence sets it on
                # every re-publish, which is exactly the write we need to catch.
                qs = qs.filter(updated_at__gte=since)
            return qs.iterator()

        result = reindex(
            index=ENTITY_INDEX,
            records=stream(),
            build_doc=search_index.build_doc,
            rebuild=options["rebuild"],
            catchup=stream,
        )
        self.stdout.write(self.style.SUCCESS(summary("nes-entities", result)))
