"""Copy a source's publisher onto sibling materials that carry none.

Some shapers set ``publisher`` and some do not. The `ciaa` scraper stamps
``अख्तियार दुरुपयोग अनुसन्धान आयोग`` on all 3,556 of its press releases, while
the 24 case-uploaded ones under ``press_release`` carry nothing — the same
documents, one attributed and one not.

**The value is copied from a real sibling row, never composed here.** An earlier
version wrote the publisher out of ``SOURCE_LABELS``, which is the wrong SHAPE:
a label's publisher is bilingual display text (``{"en": …, "ne": …}``), while a
material's is a schema.org node (``{"@type": "GovernmentOrganization", "name":
{…}}``). Consumers read ``publisher.name`` — the MCP document tool does — so
those rows would have stayed effectively unattributed while looking fixed, which
is worse than the gap. Copying an existing document's own value cannot get the
shape wrong, and it means this command can only ever propagate an attribution
the archive already makes somewhere else.

That also bounds the blast radius without a second rule. ``--from`` must be a
source whose rows actually carry a publisher, so ``--from ag`` is refused: `ag`
has none either, and the earlier label-driven version would happily have stamped
one generic name onto all 99,750 charge sheets.

Guards, all about not overwriting a truth we did not establish:

* the donor's publisher must be UNANIMOUS across the rows sampled — a source
  with two different values has no single answer to copy;
* a row that already has one is never touched, whatever it says;
* rows are locked and re-read inside the transaction, so a concurrent edit is
  not silently undone;
* ``--source`` and ``--from`` are both required and take no default.

Dry-run by default.

    manage.py backfill_publisher --source press_release --from ciaa_press_release
    manage.py backfill_publisher --source press_release --from ciaa_press_release --write
"""

from __future__ import annotations

import json
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from materials.models import Material

#: How many donor rows to inspect when establishing the value. The question is
#: "does this source speak with one voice", which a sample answers; reading
#: 99,750 rows to find the second distinct value would not change the verdict.
DONOR_SAMPLE = 200


def needs_publisher(data: Any) -> bool:
    """True iff the doc carries no usable ``publisher``.

    An empty dict or string counts as ABSENT — a falsy publisher is not an
    attribution, and treating it as present would mean the gap never closes.
    """
    return isinstance(data, dict) and not data.get("publisher")


def donor_publisher(source: str) -> dict[str, Any]:
    """The one publisher node ``source`` uses, or raise if there isn't exactly one."""
    rows = (
        Material.objects.filter(source=source, is_deleted=False)
        .values_list("data", flat=True)[:DONOR_SAMPLE]
    )
    seen = {
        json.dumps(d["publisher"], sort_keys=True, ensure_ascii=False)
        for d in rows
        if isinstance(d, dict) and d.get("publisher")
    }
    if not seen:
        raise CommandError(
            f"--from {source!r} carries no publisher on any of its rows, so there is "
            f"nothing to copy. (This is why `ag` cannot be used as a donor: its "
            f"99,750 charge sheets have no attribution either.)"
        )
    if len(seen) > 1:
        raise CommandError(
            f"--from {source!r} uses {len(seen)} different publishers, so there is no "
            f"single value to copy: {sorted(seen)[:3]}"
        )
    return json.loads(seen.pop())


class Command(BaseCommand):
    help = "Copy a source's publisher onto sibling materials that carry none."

    def add_arguments(self, parser):
        parser.add_argument("--source", required=True, help="Token to backfill.")
        parser.add_argument(
            "--from",
            dest="donor",
            required=True,
            help="Token to copy the publisher FROM. Must carry one, unanimously.",
        )
        parser.add_argument("--write", action="store_true", help="Apply the change.")
        parser.add_argument(
            "--limit", type=int, default=0, help="Stop after N rows (0 = no limit)."
        )

    def handle(self, *args, **options):
        source, donor = options["source"], options["donor"]
        limit = options["limit"]
        if limit < 0:
            raise CommandError("--limit cannot be negative.")
        if source == donor:
            raise CommandError("--source and --from must differ.")

        publisher = donor_publisher(donor)
        targets = [
            iri
            for iri, data in Material.objects.filter(
                source=source, is_deleted=False
            ).values_list("iri", "data")
            if needs_publisher(data)
        ]
        if limit:
            targets = targets[:limit]

        self.stdout.write(
            f"{source}: {len(targets)} row(s) without a publisher\n"
            f"copying from {donor}: "
            f"{json.dumps(publisher, ensure_ascii=False, sort_keys=True)}"
        )
        # Every IRI, not a head: an operator approving a write to a public
        # archive should be able to see each row it will touch, and a truncated
        # list is exactly where an unintended one hides.
        for iri in targets:
            self.stdout.write(f"  {iri}")

        if not targets:
            self.stdout.write(self.style.SUCCESS("nothing to do"))
            return
        if not options["write"]:
            self.stdout.write(
                self.style.WARNING(f"\nDRY RUN — pass --write to update {len(targets)} row(s)")
            )
            return

        written = 0
        with transaction.atomic(using="ngm"):
            # Locked and RE-READ inside the transaction. The scan above ran
            # outside it, so a caseworker edit landing in between would be
            # silently undone by writing back the stale document we had in
            # hand — this command adds one key, it does not replay a snapshot.
            for row in (
                Material.objects.using("ngm")
                .select_for_update()
                .filter(iri__in=targets, is_deleted=False)
            ):
                if not needs_publisher(row.data):
                    continue
                row.data = {**row.data, "publisher": publisher}
                row.save(update_fields=["data", "updated_at"])
                written += 1
        skipped = len(targets) - written
        self.stdout.write(self.style.SUCCESS(f"\nwrote publisher to {written} row(s)"))
        if skipped:
            self.stdout.write(f"{skipped} row(s) changed under us and were left alone")
