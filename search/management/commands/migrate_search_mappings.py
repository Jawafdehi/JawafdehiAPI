"""``migrate_search_mappings`` — add newly-declared fields to a LIVE index.

``create_index`` is idempotent by no-opping on an index that already exists, so
a field added to ``common_mappings()`` after an index was created never reaches
it. The documented answer is "it lands on the next ``--rebuild``", and this
command is the cheaper alternative: a mapping PUT takes seconds and re-streams
nothing, where a rebuild re-indexes all ~346k materials.

CORRECTION, recorded because the first version of this file asserted otherwise
and the claim reached a merged commit message: a ``--rebuild`` is NOT an outage.
``reindex_materials``'s own module docstring still says it "drops + recreates the
index", but that has not been true since the generation work —
``jawafdehi_shared.search.reindex`` builds a NEW generation beside the live one
and swaps the alias atomically when it completes, keeping the old generation
serving if validation fails. So the real argument for this command is cost and
targeting, not catastrophe avoidance.

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

from jawafdehi_shared.search.aliases import resolve_alias
from jawafdehi_shared.search.mappings import common_mappings
from jawafdehi_shared.search.opensearch import (
    ALL_INDICES,
    make_client,
    put_field_mappings,
)


class Command(BaseCommand):
    help = "Add newly-declared common_mappings() fields to an existing search index."

    @staticmethod
    def _properties(client, index: str) -> dict:
        """The mapped properties of a CONCRETE index.

        Takes a concrete name, never an alias — resolve first. ``get_mapping``
        answers ``{<concrete index>: {"mappings": {"properties": {...}}}}``, so
        the caller must key on the name it actually asked about.
        """
        return (
            client.indices.get_mapping(index=index)
            .get(index, {})
            .get("mappings", {})
            .get("properties", {})
        )

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

        # ``name`` is usually an ALIAS over a generation (``ngm-materials`` ->
        # ``ngm-materials-000003``), and ``get_mapping(index=<alias>)`` answers
        # keyed by the CONCRETE index, not by the alias. Keying the response on
        # ``name`` therefore reads ``{}`` and reports every declared field as
        # missing — including straight after a PUT that actually worked.
        #
        # This is not hypothetical: it is how this command's first outing failed
        # on 2026-10-05, aborting with "dataset_bucket is None" having already
        # committed the mapping correctly. The PUT itself was never the problem;
        # ``put_mapping`` resolves the alias server-side.
        concrete = resolve_alias(client, name) or name
        declared = common_mappings()["properties"]
        live = self._properties(client, concrete)

        # Report what is new purely so the operator can see it before/after. The
        # PUT itself sends the FULL declared block, not just the diff: re-PUTting
        # an identical property is a no-op, and sending everything means a field
        # whose live mapping drifted is surfaced as a 400 here rather than going
        # unnoticed.
        missing = sorted(set(declared) - set(live))
        where = concrete if concrete == name else f"{name} -> {concrete}"
        self.stdout.write(f"{where}: {len(live)} live properties, {len(declared)} declared")
        # A live count of 0 is the tell that the alias was not resolved. It is
        # never legitimate — every index is created from common_mappings() — so
        # fail here rather than "adding" every field and reporting success.
        if not live:
            raise CommandError(
                f"{where} reported ZERO live properties, which cannot be right. "
                "The alias was probably not resolved; refusing to continue."
            )
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
        after = self._properties(client, concrete)
        still_missing = sorted(set(declared) - set(after))
        if still_missing:
            raise CommandError(
                f"these fields are STILL absent after the PUT: {', '.join(still_missing)}"
            )
        self.stdout.write(
            self.style.SUCCESS(f"{name}: {len(after)} properties, all declared fields present")
        )
