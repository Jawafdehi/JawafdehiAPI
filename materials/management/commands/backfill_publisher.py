"""Write a reviewed publisher onto materials that carry none.

Some shapers set ``publisher`` and some do not. The `ciaa` scraper stamps
``अख्तियार दुरुपयोग अनुसन्धान आयोग`` on all 3,556 of its press releases, while
the 24 case-uploaded ones under ``press_release`` carry nothing — the same
documents, one with an attribution and one without. This command closes that
gap for a source whose publisher is already REVIEWED.

**The allowed set is not a list in this file — it is ``SOURCE_LABELS``.** A
source carries a publisher there only when one could be confirmed from evidence
(the materials' own field, a scraper, or the frontend's editorial registry), and
sources whose attribution varies per record deliberately carry none:
``news`` (the outlet differs per article), ``document``, ``legal_corpus``,
``social_media``, ``court_filing``, ``province/koshi``. So "refuse a source with
no label publisher" is the same rule as "refuse a source where a single
publisher would be a lie", already decided once and reviewed. That is why this
command reads the labels instead of taking a value on the command line: a
``--publisher`` flag would let an operator stamp an unreviewed attribution onto
a public archive in one line.

Two further guards, both about not overwriting a truth we did not establish:

* a row that ALREADY has a ``publisher`` is never touched, whatever it says;
* ``--source`` is required and takes no default, so the blast radius is always
  named explicitly.

Dry-run by default: it reports what it would write and writes nothing.

    manage.py backfill_publisher --source press_release
    manage.py backfill_publisher --source press_release --write
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from materials.models import Material
from materials.sources import SOURCE_LABELS


def needs_publisher(data: Any) -> bool:
    """True iff the doc carries no usable ``publisher``.

    Treats an empty dict/string as absent — a falsy publisher is not an
    attribution, and leaving it in place would mean the gap is never closed.
    """
    if not isinstance(data, dict):
        return False
    return not data.get("publisher")


class Command(BaseCommand):
    help = "Write a source's reviewed publisher onto its materials that carry none."

    def add_arguments(self, parser):
        parser.add_argument(
            "--source",
            required=True,
            help="The source token to backfill. Must carry a publisher in SOURCE_LABELS.",
        )
        parser.add_argument(
            "--write",
            action="store_true",
            help="Apply the change. Without it the command reports and writes nothing.",
        )
        parser.add_argument(
            "--limit", type=int, default=0, help="Stop after N rows (0 = no limit)."
        )

    def handle(self, *args, **options):
        source = options["source"]
        label = SOURCE_LABELS.get(source)
        if label is None:
            raise CommandError(
                f"{source!r} has no entry in SOURCE_LABELS, so there is no reviewed "
                f"publisher to write. Label it first (materials/sources.py)."
            )
        publisher = label.get("publisher")
        if not publisher:
            raise CommandError(
                f"{source!r} is labelled but deliberately carries NO publisher — its "
                f"attribution varies per record, so a single value would be false for "
                f"most of the shelf. Nothing to backfill."
            )

        rows = Material.objects.filter(source=source, is_deleted=False).only("iri", "data")
        todo = [m for m in rows.iterator(chunk_size=500) if needs_publisher(m.data)]
        if options["limit"]:
            todo = todo[: options["limit"]]

        total = rows.count()
        self.stdout.write(
            f"{source}: {total} live materials, {len(todo)} without a publisher\n"
            f"publisher to write: {publisher}"
        )
        for m in todo[:10]:
            self.stdout.write(f"  {m.iri}")
        if len(todo) > 10:
            self.stdout.write(f"  … and {len(todo) - 10} more")

        if not todo:
            self.stdout.write(self.style.SUCCESS("nothing to do"))
            return
        if not options["write"]:
            self.stdout.write(
                self.style.WARNING(f"\nDRY RUN — pass --write to update {len(todo)} row(s)")
            )
            return

        # Saved one row at a time inside a transaction rather than bulk_update:
        # Material.save() is what fires the search reindex (materials/signals.py),
        # and a bulk_update would leave the index serving documents whose stored
        # attribution no longer matches.
        with transaction.atomic(using="ngm"):
            for m in todo:
                m.data = {**m.data, "publisher": publisher}
                m.save(update_fields=["data", "updated_at"])
        self.stdout.write(self.style.SUCCESS(f"\nwrote publisher to {len(todo)} row(s)"))
