"""``reindex_courtcases`` — bulk-(re)index NGM court cases into ``ngm-courtcases``.

Streams every ``CourtCase`` (with its ``court`` selected so the indexer can read
the English court name without an extra query per row) through the court-case
indexer. ``--rebuild`` builds a new generation and swaps the alias onto it when
it is complete — no window in which search is empty. Router pins ``CourtCase`` to
``ngm``.

This is the heaviest of the four: a ~2.2M-row sequential scan, of which ~27k rows
pass the visibility gate. The scan is the cost, not the indexing, and the weekly
cron already pays it — so running it as a rebuild (the only mode that EVICTS a
doc whose case has since become hidden) is close to free.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from jawafdehi_shared.search.opensearch import COURTCASE_INDEX
from jawafdehi_shared.search.reindex import reindex, summary
from courts import search_index
from courts.models import CourtCase
from courts.search_visibility import (
    court_case_public_visible,
    published_referenced_iris,
)


class Command(BaseCommand):
    help = "Bulk-(re)index NGM court cases into the ngm-courtcases OpenSearch index."

    def add_arguments(self, parser):
        parser.add_argument(
            "--rebuild",
            action="store_true",
            help="Build a new generation and swap the alias onto it (no downtime). "
            "The only mode that evicts docs whose case is no longer visible.",
        )
        parser.add_argument(
            "--since",
            help="Only (re)index cases with updated_at >= this AD date/ISO "
            "datetime (incremental). Ignored with --rebuild (a full rebuild "
            "must re-stream every case). Lets the importer drive a cheap "
            "incremental reindex instead of re-streaming the whole corpus.",
        )

    def handle(self, *args, **options):
        # The public index is the curated corruption / public-accountability slice,
        # NOT a mirror of the 1.6M docket. Gate on court_case_public_visible (the
        # same gate the live signal applies) plus is_deleted — mirroring
        # reindex_materials' is_deleted+LISTED gate. Refresh the publish-link cache
        # once up front so this bulk run sees the current PUBLISHED references.
        published_referenced_iris(refresh=True)

        def stream(since=None):
            qs = CourtCase.objects.select_related("court").filter(is_deleted=False)
            if since:
                qs = qs.filter(updated_at__gte=since)
            return (
                case
                for case in qs.order_by("court_id", "case_number").iterator()
                if court_case_public_visible(case)
            )

        def changed(since):
            """``(iri, case_or_None)`` for every case written during the build.

            NOT filtered on is_deleted or on the gate: a case that became hidden
            (or was soft-deleted) mid-build must come through as a TOMBSTONE. The
            live signal evicted it from the old generation, and without the
            tombstone the swap would put it back — which for a case reclassified
            to a SENSITIVE type means the sensitive floor is undone by a reindex.
            """
            # Re-read the publish-link cache: this runs an hour after the refresh
            # above, and a case published in between changes which court cases the
            # gate lets through.
            published_referenced_iris(refresh=True)
            qs = CourtCase.objects.select_related("court").filter(
                updated_at__gte=since
            )
            for case in qs.order_by("court_id", "case_number").iterator():
                yield case.iri, (case if court_case_public_visible(case) else None)

        result = reindex(
            index=COURTCASE_INDEX,
            records=stream(None if options["rebuild"] else options.get("since")),
            build_doc=search_index.build_doc,
            rebuild=options["rebuild"],
            # Cases the importer touched during the scan land on the OLD generation
            # and would be lost at the swap. The scan is ~1h wide and the court
            # scrapers run every 12h, so this is a real overlap, not a theoretical
            # one.
            catchup=changed,
        )
        self.stdout.write(self.style.SUCCESS(summary("ngm-courtcases", result)))
