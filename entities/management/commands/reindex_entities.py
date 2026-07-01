"""``reindex_entities`` — bulk-(re)index NES entities into ``nes-entities``.

Streams every ``StoredEntity`` through the entity indexer's ``build_doc`` into
OpenSearch. ``--rebuild`` drops + recreates the index first (mapping changes).
The DB router pins ``StoredEntity`` reads to the ``nes`` DB automatically.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from jawafdehi_shared.search.opensearch import ENTITY_INDEX
from jawafdehi_shared.search.reindex import reindex
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
        result = reindex(
            index=ENTITY_INDEX,
            records=StoredEntity.objects.all().iterator(),
            build_doc=search_index.build_doc,
            rebuild=options["rebuild"],
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"nes-entities: indexed={result['indexed']} skipped={result['skipped']}"
            )
        )
