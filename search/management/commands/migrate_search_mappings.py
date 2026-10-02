"""``migrate_search_mappings`` — add newly-declared fields to a LIVE index.

``create_index`` is idempotent by no-opping on an index that already exists, so
a field added to ``common_mappings()`` after an index was created never reaches
it. The documented answer used to be "it lands on the next ``--rebuild``". For
materials that answer is too expensive: ``reindex_materials --rebuild`` DROPS
and recreates ``ngm-materials`` in place — no alias, no generation to swap (that
is ``reindex_courtcases``) — so it is an outage over 346k documents on an index
with a history of OOM kills.

This command PUTs the declared properties onto the existing index instead.
Online, seconds, idempotent. OpenSearch accepts an additive property and a
re-PUT of an identical one, and REJECTS a type change on an existing field with
a 400 — so this can never silently retype a live field.

It does NOT backfill: documents indexed before the PUT carry no value for the
new field, and a ``terms`` clause excludes them until they are re-indexed
(``reindex_materials --since <date>``).

ORDERING. Run this BEFORE deploying an indexer that emits the new field.
``common_mappings()`` declares no ``dynamic`` setting, so OpenSearch defaults to
dynamic mapping on: the first write of an undeclared field types it from the
value seen. A hyphenated token (``report_annual-report``) typed ``text`` is
split by the analyzer, and a ``terms`` clause against it then matches NOTHING
while still returning 200 — a silent wrong answer, and a later PUT to
``keyword`` is rejected.

Usage::

    python manage.py migrate_search_mappings --index ngm-materials
    python manage.py migrate_search_mappings --index ngm-materials --dry-run

``--index`` is required rather than defaulting to every index on purpose. Some
live indices picked fields up by dynamic mapping before they were declared (see
the ``weight`` note in ``mappings.py``), so a blanket loop risks a conflict 400
on an index nobody meant to touch.
"""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from jawafdehi_shared.search.mappings import common_mappings
from jawafdehi_shared.search.opensearch import (
    ALL_INDICES,
    make_client,
    put_field_mappings,
)


class Command(BaseCommand):
    help = "Add newly-declared common_mappings() fields to an existing search index."

    def add_arguments(self, parser):
        parser.add_argument(
            "--index",
            required=True,
            help=f"Index to update. One of: {', '.join(ALL_INDICES)}",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print the fields that would be added, then exit without writing.",
        )

    def handle(self, *args, **options):
        name = options["index"]
        if name not in ALL_INDICES:
            raise CommandError(f"unknown index {name!r}; expected one of {ALL_INDICES}")

        client = make_client()
        if not client.indices.exists(index=name):
            raise CommandError(
                f"index {name!r} does not exist — create it with ensure_indices() first"
            )

        declared = common_mappings()["properties"]
        live = (
            client.indices.get_mapping(index=name)
            .get(name, {})
            .get("mappings", {})
            .get("properties", {})
        )

        # Report what is new purely so the operator can see it before/after. The
        # PUT itself sends the FULL declared block, not just the diff: re-PUTting
        # an identical property is a no-op, and sending everything means a field
        # whose live mapping drifted is surfaced as a 400 here rather than going
        # unnoticed.
        missing = sorted(set(declared) - set(live))
        self.stdout.write(f"{name}: {len(live)} live properties, {len(declared)} declared")
        if missing:
            self.stdout.write(self.style.WARNING(f"  to add: {', '.join(missing)}"))
        else:
            self.stdout.write("  no new fields to add")

        if options["dry_run"]:
            self.stdout.write("dry run — nothing written")
            return

        response = put_field_mappings(client, name, declared)
        self.stdout.write(self.style.SUCCESS(f"put_mapping -> {json.dumps(response)}"))

        # Re-read so the operator sees the committed state, not the ack. A PUT
        # that reports acknowledged still leaves the field absent if it was
        # filtered upstream, and this command's whole job is to make the next
        # step (a terms query) safe to trust.
        after = (
            client.indices.get_mapping(index=name)
            .get(name, {})
            .get("mappings", {})
            .get("properties", {})
        )
        still_missing = sorted(set(declared) - set(after))
        if still_missing:
            raise CommandError(
                f"these fields are STILL absent after the PUT: {', '.join(still_missing)}"
            )
        self.stdout.write(
            self.style.SUCCESS(f"{name}: {len(after)} properties, all declared fields present")
        )
