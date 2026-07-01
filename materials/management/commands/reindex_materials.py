"""``reindex_materials`` — bulk-(re)index NGM materials into ``ngm-materials``.

Streams every ``Material`` through the material indexer into OpenSearch.
``--rebuild`` drops + recreates the index. The router pins ``Material`` to ``ngm``.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from jawafdehi_shared.search.opensearch import MATERIAL_INDEX
from jawafdehi_shared.search.reindex import reindex
from materials import search_index
from materials.models import Material


class Command(BaseCommand):
    help = "Bulk-(re)index NGM materials into the ngm-materials OpenSearch index."

    def add_arguments(self, parser):
        parser.add_argument(
            "--rebuild",
            action="store_true",
            help="Drop and recreate the index before reindexing.",
        )
        parser.add_argument(
            "--since",
            help="Only (re)index materials with updated_at >= this ISO datetime "
            "(incremental). Ignored with --rebuild (a full rebuild must re-stream "
            "every material). Lets a sync run reindex ONLY the materials it just "
            "upserted instead of rebuilding the whole index.",
        )

    def handle(self, *args, **options):
        qs = Material.objects.all()
        if options.get("since") and not options["rebuild"]:
            qs = qs.filter(updated_at__gte=options["since"])
        result = reindex(
            index=MATERIAL_INDEX,
            records=qs.order_by("iri").iterator(),
            build_doc=search_index.build_doc,
            rebuild=options["rebuild"],
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"ngm-materials: indexed={result['indexed']} skipped={result['skipped']}"
            )
        )
