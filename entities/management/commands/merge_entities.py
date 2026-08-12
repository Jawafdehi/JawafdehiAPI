"""Merge duplicate entities from the command line.

Same service as POST /api/entities/merge, with the reference cap lifted — this is how
a merge touching more than MAX_REFERENCES references gets done.
"""

import json

from django.core.management.base import BaseCommand, CommandError

from entities.services.merge import EntityMergeService, MergeError


class Command(BaseCommand):
    help = "Merge one or more duplicate entities into a survivor."

    def add_arguments(self, parser):
        parser.add_argument("--survivor", required=True, help="Survivor entity @id IRI")
        parser.add_argument(
            "--duplicate", action="append", required=True,
            help="Duplicate entity @id IRI (repeat for several)",
        )
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument(
            "--author", default="author:entity-merge-command",
            help="Author id recorded on the merge",
        )
        parser.add_argument("--change-description", default="")

    def handle(self, *args, **opts):
        try:
            result = EntityMergeService().merge(
                survivor_iri=opts["survivor"],
                duplicate_iris=opts["duplicate"],
                author_id=opts["author"],
                change_description=opts["change_description"],
                dry_run=opts["dry_run"],
                enforce_reference_cap=False,
            )
        except MergeError as exc:
            raise CommandError(f"{exc.code}: {exc.message}") from exc

        summary = {k: v for k, v in result.items() if k != "survivor"}
        self.stdout.write(json.dumps(summary, indent=2, ensure_ascii=False))
