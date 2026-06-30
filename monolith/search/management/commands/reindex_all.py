"""``reindex_all`` — ensure every index exists then run all four reindexers.

Umbrella command: ``ensure_indices()`` (create any missing index with the
bilingual config) followed by each app's ``reindex_<x>`` command. ``--rebuild``
is forwarded to each (drop + recreate every index first).
"""

from __future__ import annotations

from django.core.management import call_command
from django.core.management.base import BaseCommand

from jawafdehi_shared.search.opensearch import ensure_indices

_SUBCOMMANDS = (
    "reindex_entities",
    "reindex_materials",
    "reindex_courtcases",
    "reindex_cases",
)


class Command(BaseCommand):
    help = "Ensure all unified-search indices exist, then reindex every app."

    def add_arguments(self, parser):
        parser.add_argument(
            "--rebuild",
            action="store_true",
            help="Drop and recreate every index before reindexing.",
        )

    def handle(self, *args, **options):
        rebuild = options["rebuild"]
        # Create any missing index with the bilingual config up front. (Each
        # sub-command also create_index()s its own; --rebuild handles drops.)
        created = ensure_indices()
        if created:
            self.stdout.write(self.style.SUCCESS(f"created indices: {created}"))
        for name in _SUBCOMMANDS:
            self.stdout.write(f"running {name}...")
            call_command(name, rebuild=rebuild)
        self.stdout.write(self.style.SUCCESS("reindex_all complete."))
