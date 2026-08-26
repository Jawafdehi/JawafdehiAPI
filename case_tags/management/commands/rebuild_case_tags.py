"""Recompute every case's tags from the vocabulary.

ONE command, not a backfill followed by a curation pass. It recomputes from scratch
on every run:

    tags = resolve(tags_source) ∪ curation.add − curation.remove

Two sequential mutating commands would mean re-running the first silently wipes what
the second wrote. Recomputing from two files instead makes the result a pure function
of the inputs: order-independent, re-runnable, and reproducible from the repo.

    uv run python manage.py rebuild_case_tags --dry-run
    uv run python manage.py rebuild_case_tags --apply

`tags_source` is written only when NULL, so the pre-cleanup snapshot survives every
re-run — that is what makes this reversible.
"""

from __future__ import annotations

import pathlib
from collections.abc import Mapping
from typing import Any

import yaml
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import transaction

from case_tags.models import Resolution, Tag, TagStatus, resolve
from cases.models import Case

DEFAULT_CURATION = pathlib.Path("case_tags/curation.yml")


class Command(BaseCommand):
    help = "Recompute Case.tags from the vocabulary and the curation file."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--curation", default=str(DEFAULT_CURATION))
        parser.add_argument("--apply", action="store_true", help="Write the changes.")
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report only. The default — --apply is required to write.",
        )
        parser.add_argument("--slug", help="Restrict to one case, for spot checks.")

    def handle(self, *args: Any, **options: Any) -> None:
        curation = self._load_curation(pathlib.Path(options["curation"]))
        cases = Case.objects.all()
        if options["slug"]:
            cases = cases.filter(slug=options["slug"])
            if not cases.exists():
                raise CommandError(f"No case with slug {options['slug']!r}")

        plans = [self._plan(case, curation) for case in cases]
        self._report(plans, curation)

        if not options["apply"]:
            self.stdout.write(self.style.WARNING("\ndry run — nothing written"))
            return

        # One transaction over every case: a partial rebuild would leave some rows
        # canonical and some raw, and nothing downstream could tell which was which.
        with transaction.atomic():
            changed = [p for p in plans if p.changed]
            for plan in changed:
                case = plan.case
                if case.tags_source is None:
                    case.tags_source = list(plan.source)
                case.tags = list(plan.result)
                case.save(update_fields=["tags", "tags_source", "updated_at"])
        self._reindex(changed)
        self.stdout.write(self.style.SUCCESS(f"\napplied to {len(changed)} cases"))

    # -- planning ---------------------------------------------------------

    def _plan(self, case: Case, curation: dict[str, dict[str, list[str]]]) -> _Plan:
        # tags_source is the input once it exists, so a re-run reads the ORIGINAL
        # free text rather than the canonical ids the last run wrote.
        source: list[str] = list(
            case.tags_source if case.tags_source is not None else (case.tags or [])
        )

        resolved: list[str] = []
        retired: list[str] = []
        unknown: list[str] = []
        for raw in source:
            outcome = resolve(raw)
            if outcome.resolution is Resolution.CANONICAL and outcome.tag_id:
                if outcome.tag_id not in resolved:
                    resolved.append(outcome.tag_id)
            elif outcome.resolution is Resolution.RETIRED:
                retired.append(raw)
            else:
                unknown.append(raw)

        entry = curation.get(case.slug, {})
        for tag_id in entry.get("add", []):
            if tag_id not in resolved:
                resolved.append(tag_id)
        removals = set(entry.get("remove", []))
        result = [t for t in resolved if t not in removals]

        return _Plan(
            case=case,
            source=source,
            result=result,
            retired=retired,
            unknown=unknown,
            curated=bool(entry),
        )

    def _load_curation(self, path: pathlib.Path) -> dict[str, dict[str, list[str]]]:
        """Optional. Absent until the curation PR lands, and the rebuild is still
        correct without it — it just leaves the thin cases thin."""
        if not path.exists():
            return {}
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        entries: dict[str, dict[str, list[str]]] = {}
        for entry in document.get("cases") or []:
            slug = entry["slug"]
            if slug in entries:
                # Last-wins would make the file's meaning depend on its order.
                raise CommandError(f"{path}: duplicate slug {slug!r}")
            if not (entry.get("why") or "").strip():
                raise CommandError(f"{path}: {slug!r} has no `why` — required.")
            # Curation names canonical IDS, not raw values — so check the Tag table
            # directly. Going through the alias table would be the raw-value path and
            # would accept `Land Management` here, which reads as a tag id but is not.
            for tag_id in [*entry.get("add", []), *entry.get("remove", [])]:
                if not Tag.objects.filter(pk=tag_id, status=TagStatus.ACTIVE).exists():
                    raise CommandError(
                        f"{path}: {slug!r} names {tag_id!r}, which is not an active tag."
                    )
            entries[slug] = {
                "add": list(entry.get("add", [])),
                "remove": list(entry.get("remove", [])),
            }
        self._check_slugs(entries, path)
        return entries

    def _check_slugs(
        self, entries: dict[str, dict[str, list[str]]], path: pathlib.Path
    ) -> None:
        """Fail loudly on a slug that no longer resolves.

        Published cases DO get re-slugged (see CaseSlugHistory), so a curation entry
        can rot. A skipped entry logged at debug level is how curation quietly stops
        applying; this makes it stop the run instead.
        """
        known = set(Case.objects.values_list("slug", flat=True))
        missing = sorted(set(entries) - known)
        if not missing:
            return
        from cases.models import CaseSlugHistory

        retired = dict(
            CaseSlugHistory.objects.filter(slug__in=missing).values_list(
                "slug", "case__slug"
            )
        )
        lines = [
            f"  {slug} -> renamed to {retired[slug]!r}" if slug in retired else
            f"  {slug} -> no such case"
            for slug in missing
        ]
        raise CommandError(f"{path}: unresolvable slugs:\n" + "\n".join(lines))

    # -- reporting --------------------------------------------------------

    def _report(self, plans: list[_Plan], curation: Mapping[str, object]) -> None:
        changed = [p for p in plans if p.changed]
        before = sum(len(p.source) for p in plans)
        after = sum(len(p.result) for p in plans)
        empty = [p for p in plans if not p.result]
        unknown: dict[str, int] = {}
        for plan in plans:
            for raw in plan.unknown:
                unknown[raw] = unknown.get(raw, 0) + 1

        self.stdout.write(f"cases:            {len(plans)}")
        self.stdout.write(f"cases changing:   {len(changed)}")
        self.stdout.write(f"tags per case:    {before / max(len(plans), 1):.1f} -> "
                          f"{after / max(len(plans), 1):.1f}")
        self.stdout.write(f"curation entries: {len(curation)}")

        if unknown:
            self.stdout.write(
                self.style.WARNING(
                    f"\n{len(unknown)} raw values match NO alias and are being dropped "
                    "— add them to vocabulary.yml or accept the loss:"
                )
            )
            for raw, n in sorted(unknown.items(), key=lambda kv: -kv[1]):
                self.stdout.write(f"  {n:>3}  {raw!r}")

        if empty:
            self.stdout.write(
                self.style.WARNING(f"\n{len(empty)} cases end with NO tags:")
            )
            for plan in empty:
                self.stdout.write(f"  {plan.case.slug}  (was: {plan.source})")

    def _reindex(self, plans: list[_Plan]) -> None:
        """Tag writes go through QuerySet-free saves, but the search document is a
        separate store — without this the facet counts disagree with the results."""
        try:
            from cases.search_index import index as index_case
        except ImportError:  # pragma: no cover - indexer optional in some settings
            return
        for plan in plans:
            try:
                index_case(plan.case)
            except Exception as exc:  # noqa: BLE001 - best effort, reported not raised
                self.stderr.write(f"reindex failed for {plan.case.slug}: {exc}")


class _Plan:
    """What one case would become."""

    def __init__(
        self,
        *,
        case: Case,
        source: list[str],
        result: list[str],
        retired: list[str],
        unknown: list[str],
        curated: bool,
    ) -> None:
        self.case = case
        self.source = source
        self.result = result
        self.retired = retired
        self.unknown = unknown
        self.curated = curated

    @property
    def changed(self) -> bool:
        return list(self.case.tags or []) != self.result or self.case.tags_source is None


__all__ = ["Command"]
