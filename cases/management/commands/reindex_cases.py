"""``reindex_cases`` — bulk-(re)index PUBLISHED Jawafdehi cases.

Streams only PUBLISHED cases into ``jawafdehi-cases`` (the case-only-published
rule — the search index is all-public). ``--rebuild`` drops + recreates the
index, which also evicts any case that has since left PUBLISHED. Router pins
``Case`` to ``default``.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from cases import search_index
from cases.models import Case, CaseState
from jawafdehi_shared.search.opensearch import CASE_INDEX
from jawafdehi_shared.search.reindex import reindex


class Command(BaseCommand):
    help = "Bulk-(re)index PUBLISHED Jawafdehi cases into jawafdehi-cases."

    def add_arguments(self, parser):
        parser.add_argument(
            "--rebuild",
            action="store_true",
            help="Drop and recreate the index before reindexing.",
        )

    def handle(self, *args, **options):
        # Only PUBLISHED cases are indexed. (build_doc also yields no iri for a
        # non-published case, so the reindex driver would skip it anyway — the
        # filter just avoids streaming the whole table.)
        # build_doc reads the court_cases property (the reference join), so
        # prefetch it; chunk_size is REQUIRED for prefetch to apply under
        # .iterator() (ValueError without it on Django 5.x).
        result = reindex(
            index=CASE_INDEX,
            records=Case.objects.filter(state=CaseState.PUBLISHED)
            .prefetch_related("courtcase_references")
            .iterator(chunk_size=200),
            build_doc=search_index.build_doc,
            rebuild=options["rebuild"],
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"jawafdehi-cases: indexed={result['indexed']} skipped={result['skipped']}"
            )
        )
