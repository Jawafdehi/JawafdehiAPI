"""``reindex_materials`` — bulk-(re)index NGM materials into ``ngm-materials``.

Streams the SEARCHABLE ``Material`` set through the material indexer into
OpenSearch. ``--rebuild`` drops + recreates the index. The router pins
``Material`` to ``ngm``.

Only ``is_deleted=False`` + ``visibility=LISTED`` rows are indexed — the SAME
gate the live ``post_save`` signal applies (``materials.signals``: a soft-deleted
or non-LISTED row is EVICTED, not indexed). Without this filter a ``--rebuild``
would re-add every soft-deleted material (tombstones resurrected) and every
UNLISTED/PRIVATE case-source material (a draft case's evidence leaked into anon
search) — the exact rows the signal spent its writes evicting.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from jawafdehi_shared.search.opensearch import MATERIAL_INDEX
from jawafdehi_shared.search.reindex import reindex, summary
from materials import search_index
from materials.models import Material, Visibility


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
        # Mirror the live indexer's gate (materials.signals): index ONLY the
        # searchable set (live + LISTED). A --rebuild otherwise resurrects
        # soft-deleted rows and leaks non-LISTED (draft/in-review) evidence.
        def stream(since=None):
            qs = Material.objects.filter(
                is_deleted=False, visibility=Visibility.LISTED
            )
            if since:
                qs = qs.filter(updated_at__gte=since)
            return qs.order_by("iri").iterator()

        result = reindex(
            index=MATERIAL_INDEX,
            records=stream(None if options["rebuild"] else options.get("since")),
            build_doc=search_index.build_doc,
            rebuild=options["rebuild"],
            # Materials uploaded while the new generation was building land on the
            # OLD one and would be lost at the swap.
            catchup=stream,
        )
        self.stdout.write(self.style.SUCCESS(summary("ngm-materials", result)))
