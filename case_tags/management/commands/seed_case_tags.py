"""Load the tag vocabulary from YAML into the database.

Vocabulary content lives in a reviewable YAML file rather than a data migration:
it churns, and migrations are append-only, so a dozen label edits would become a
dozen migrations and no single place to read the current list. Schema goes in
migrations; content goes here.

    uv run python manage.py seed_case_tags --path case_tags/vocabulary.yml --dry-run
    uv run python manage.py seed_case_tags --path case_tags/vocabulary.yml
"""

from __future__ import annotations

import pathlib
from typing import Any

import yaml
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import transaction

from case_tags.models import Tag, TagAlias, TagStatus
from case_tags.normalize import normalize

DEFAULT_PATH = pathlib.Path("case_tags/vocabulary.yml")


class Command(BaseCommand):
    help = "Upsert the canonical tag vocabulary from a YAML file."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--path", default=str(DEFAULT_PATH))
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would change and roll back.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        path = pathlib.Path(options["path"])
        if not path.exists():
            raise CommandError(f"No vocabulary at {path}")
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        entries: list[dict[str, Any]] = document.get("tags") or []
        dropped: list[dict[str, Any]] = document.get("dropped") or []
        if not entries:
            raise CommandError(f"{path} defines no tags")

        try:
            with transaction.atomic():
                created, updated = self._upsert_tags(entries)
                aliases = self._upsert_aliases(entries, dropped)
                removed = self._report_removals(entries)
                self.stdout.write(
                    f"tags: {created} created, {updated} updated, "
                    f"{len(entries)} in file\naliases: {aliases} total"
                )
                if removed:
                    self.stdout.write(
                        self.style.WARNING(
                            "in the DB but absent from the file (left alone): "
                            + ", ".join(sorted(removed))
                        )
                    )
                if options["dry_run"]:
                    raise _Rollback
        except _Rollback:
            self.stdout.write(self.style.WARNING("dry run — rolled back"))

    def _upsert_tags(self, entries: list[dict[str, Any]]) -> tuple[int, int]:
        """Two passes: every row must exist before any ``broader`` can point at one."""
        created = updated = 0
        for entry in entries:
            _, was_created = Tag.objects.update_or_create(
                pk=entry["id"],
                defaults={
                    "label_ne": entry["label_ne"],
                    "label_en": entry["label_en"],
                    "status": entry.get("status", TagStatus.PROPOSED),
                    "sort_order": entry.get("sort_order", 0),
                    "note": entry.get("note", "") or "",
                },
            )
            created += was_created
            updated += not was_created

        for entry in entries:
            tag = Tag.objects.get(pk=entry["id"])
            tag.broader_id = entry.get("broader")
            tag.merged_into_id = entry.get("merged_into")
            tag.full_clean(exclude=["id"])
            tag.save(update_fields=["broader", "merged_into"])
        return created, updated

    def _upsert_aliases(
        self, entries: list[dict[str, Any]], dropped: list[dict[str, Any]]
    ) -> int:
        """Rebuild the alias table from the file — it is the source of truth.

        The ``dropped:`` groups land here too, as rows with a null tag. That is what
        lets a retired filter value answer "this was removed" instead of "unknown".
        """
        wanted: dict[str, tuple[str | None, str, str]] = {}
        for entry in entries:
            for raw in [*(entry.get("aliases") or []), entry["id"]]:
                wanted[normalize(str(raw))] = (entry["id"], "", str(raw))
        for group in dropped:
            reason = group["reason"]
            for raw in group.get("values") or []:
                key = normalize(str(raw))
                if key in wanted and wanted[key][0] is not None:
                    raise CommandError(
                        f"{raw!r} is listed as dropped but also aliases "
                        f"{wanted[key][0]!r}"
                    )
                wanted[key] = (None, reason, str(raw))

        TagAlias.objects.exclude(key__in=wanted).delete()
        for key, (tag_id, reason, source) in wanted.items():
            TagAlias.objects.update_or_create(
                key=key,
                defaults={"tag_id": tag_id, "retired_reason": reason, "source": source},
            )
        return len(wanted)

    def _report_removals(self, entries: list[dict[str, Any]]) -> set[str]:
        """Never delete a tag silently.

        A tag missing from the file might be a deliberate retirement or a bad merge;
        either way cases may still carry it, and dropping the row would strand them.
        Report and let a human decide.
        """
        in_file = {entry["id"] for entry in entries}
        return set(Tag.objects.exclude(pk__in=in_file).values_list("pk", flat=True))


class _Rollback(Exception):
    """Abort the transaction after a dry run."""
