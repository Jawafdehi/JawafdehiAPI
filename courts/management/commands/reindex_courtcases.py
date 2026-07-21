"""``reindex_courtcases`` — bulk-(re)index NGM court cases into ``ngm-courtcases``.

Streams every ``CourtCase`` (with its ``court`` selected so the indexer can read
the English court name without an extra query per row) through the court-case
indexer. ``--rebuild`` drops + recreates the index. Router pins ``CourtCase`` to
``ngm``.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from jawafdehi_shared.search.opensearch import COURTCASE_INDEX
from jawafdehi_shared.search.reindex import reindex
from courts import search_index
from courts.models import CourtCase


class Command(BaseCommand):
    help = "Bulk-(re)index NGM court cases into the ngm-courtcases OpenSearch index."

    def add_arguments(self, parser):
        parser.add_argument(
            "--rebuild",
            action="store_true",
            help="Drop and recreate the index before reindexing.",
        )
        parser.add_argument(
            "--since",
            help="Only (re)index cases with updated_at >= this AD date/ISO "
            "datetime (incremental). Ignored with --rebuild (a full rebuild "
            "must re-stream every case). Lets the importer drive a cheap "
            "incremental reindex instead of re-streaming the whole corpus.",
        )

    def handle(self, *args, **options):
        qs = CourtCase.objects.select_related("court")
        if options.get("since") and not options["rebuild"]:
            qs = qs.filter(updated_at__gte=options["since"])
        result = reindex(
            index=COURTCASE_INDEX,
            records=qs.order_by("court_id", "case_number").iterator(),
            build_doc=search_index.build_doc,
            rebuild=options["rebuild"],
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"ngm-courtcases: indexed={result['indexed']} skipped={result['skipped']}"
            )
        )
