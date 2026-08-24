"""Apply :mod:`case_tags.cleanup` to the published corpus.

DRY RUN BY DEFAULT. ``--apply`` is required to write, because this rewrites ``Case.tags``
on published cases and deleting a tag value is not recoverable from the row itself.

The snapshot is not optional either. policy §12 step 7 says preserve the original value,
and the whole reason the tagger can be trusted to re-tag later is that we can see what was
there before it did.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from jawafdehi_shared.tags.normalize import normalize_tag

from case_tags.cleanup import plan
from case_tags.models import AliasSource, Tag, TagAlias


class Command(BaseCommand):
    help = "Normalize, remap and prune the free-text tags on published cases."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Actually write. Without this the command only reports.",
        )
        parser.add_argument(
            "--snapshot",
            default="",
            help="Where to write the pre-change snapshot (required with --apply).",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        from cases.models import Case  # noqa: PLC0415

        apply = options["apply"]
        cases = list(Case.objects.filter(state="PUBLISHED").order_by("slug"))
        raw_values = [t for c in cases for t in (c.tags or []) if isinstance(t, str)]

        result = plan(raw_values)
        self.stdout.write(
            f"{len(cases)} published cases, "
            f"{len(set(raw_values))} distinct tag values, "
            f"{len(raw_values)} applications"
        )
        self.stdout.write(
            self.style.WARNING(f"  delete {len(result.delete)}") + f"  remap {len(result.remap)}"
            f"  keep {len(result.keep)}"
        )
        for raw, reason in sorted(result.delete.items()):
            self.stdout.write(f"    DELETE  {raw!r:50} {reason}")
        for raw, canonical in sorted(result.remap.items()):
            self.stdout.write(f"    REMAP   {raw!r:50} -> {canonical}")
        for raw in sorted(result.keep):
            self.stdout.write(f"    keep    {raw!r}")

        missing = sorted(set(result.remap.values()) - set(Tag.objects.values_list("id", flat=True)))
        if missing:
            raise CommandError(
                f"The map targets terms that do not exist: {missing}. "
                "Seed the vocabulary first (migration 0002), or fix cleanup.FRAGMENTATION."
            )

        if not apply:
            self.stdout.write(self.style.NOTICE("\nDry run. Re-run with --apply to write."))
            return

        snapshot_path = options["snapshot"]
        if not snapshot_path:
            raise CommandError("--snapshot is required with --apply.")

        snapshot = {c.slug: list(c.tags or []) for c in cases}
        Path(snapshot_path).write_text(
            json.dumps(
                {"taken_at": timezone.now().isoformat(), "cases": snapshot},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        self.stdout.write(f"Snapshot written to {snapshot_path}")

        changed = 0
        with transaction.atomic():
            # Aliases first, so the resolver can reach the remapped values immediately
            # and so a partly-applied run leaves the mapping recorded rather than lost.
            for raw, canonical in result.remap.items():
                TagAlias.objects.get_or_create(
                    value=normalize_tag(raw),
                    defaults={
                        "tag_id": canonical,
                        "source": AliasSource.SEED,
                        "approved_by": "clean_case_tags",
                        "approved_at": timezone.now(),
                    },
                )
            for case in cases:
                out: list[str] = []
                for raw in case.tags or []:
                    if not isinstance(raw, str):
                        continue
                    if raw in result.delete:
                        continue
                    # De-duplicate: two raw values collapsing onto one term is the point,
                    # and the term must not then appear twice on the case.
                    value = result.remap.get(raw, raw)
                    if value not in out:
                        out.append(value)
                if out != (case.tags or []):
                    case.tags = out
                    case.save(update_fields=["tags"])
                    changed += 1

        self.stdout.write(self.style.SUCCESS(f"Applied. {changed} cases rewritten."))
