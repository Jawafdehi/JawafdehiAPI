"""Backfill NULL source_type values for DocumentSource records.

Classification is delegated to ``cases.services.source_classifier`` — the same
deterministic, LLM-free rules used by the ``revamp_source_types`` data migration
and by source producers — so the command can never drift from them.

Usage::

    python manage.py backfill_source_types --dry-run
    python manage.py backfill_source_types --dry-run --verbose
    python manage.py backfill_source_types --limit 100
    python manage.py backfill_source_types --source-id source:20260601:abc12345
    python manage.py backfill_source_types --allow-production

CLI flags::

    --dry-run              classify but don't save
    --limit N              process max N sources
    --source-id S          classify a single source by source_id
    --verbose              detailed per-source logging
    --allow-production     required when DEBUG=False
"""

from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from cases.models import DocumentSource
from cases.services.source_classifier import classify_source_type


class Command(BaseCommand):
    help = (
        "Backfill NULL source_type on DocumentSource records using the shared "
        "deterministic classifier (cases.services.source_classifier). No LLM."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Classify but do not save.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=0,
            help="Max sources to process (0 = unlimited).",
        )
        parser.add_argument(
            "--source-id",
            type=str,
            default=None,
            help="Classify a single source by source_id.",
        )
        parser.add_argument(
            "--verbose",
            action="store_true",
            help="Detailed per-source logging.",
        )
        parser.add_argument(
            "--allow-production",
            action="store_true",
            help="Required when DEBUG=False.",
        )

    def handle(self, *args, **options):
        self.verbose = options["verbose"]

        if not settings.DEBUG and not options["allow_production"]:
            raise CommandError(
                "Refusing to run in production. Use --allow-production to override."
            )

        qs = self._get_queryset(options)
        total = qs.count()
        if total == 0:
            self.stdout.write("No sources found with NULL source_type.")
            return

        sources, actual = self._apply_limit(qs, options["limit"], total)
        dry_run = options["dry_run"]

        classified, results = self._classify_batch(sources, dry_run=dry_run)
        self._print_summary(actual, classified, dry_run, results)

    def _get_queryset(self, options):
        qs = DocumentSource.objects.filter(source_type__isnull=True, is_deleted=False)
        if options["source_id"]:
            qs = qs.filter(source_id=options["source_id"])
        return qs

    def _apply_limit(self, qs, limit, total):
        if limit > 0:
            qs = qs[:limit]
            actual = len(qs)
            self.stdout.write(f"Processing up to {actual} of {total} eligible sources.")
        else:
            qs = list(qs)
            actual = len(qs)
            self.stdout.write(f"Processing all {actual} eligible sources.")
        return qs, actual

    def _classify_batch(self, sources, *, dry_run):
        classified = 0
        results: dict[str, int] = {}
        updates: dict[str, list[str]] = {}

        for source in sources:
            label = classify_source_type(
                source.title,
                source.description,
                source.url_links,
                prior_type=source.source_type,
            )
            results[label] = results.get(label, 0) + 1
            classified += 1

            if dry_run:
                self._log_verbose(f"[DRY-RUN] {source.source_id}: → {label}")
            else:
                updates.setdefault(label, []).append(source.source_id)
                self._log_verbose(f"[SET] {source.source_id}: → {label}")

        if not dry_run and updates:
            now = timezone.now()
            for label, source_ids in updates.items():
                DocumentSource.objects.filter(source_id__in=source_ids).update(
                    source_type=label,
                    updated_at=now,
                )

        return classified, results

    def _print_summary(self, actual, classified, dry_run, results):
        self.stdout.write("-" * 50)
        self.stdout.write(f"Total={actual}  Classified={classified}  Dry-run={dry_run}")
        if classified:
            self.stdout.write("Breakdown:")
            for label, count in sorted(results.items(), key=lambda x: -x[1]):
                self.stdout.write(f"  {label}: {count}")

    def _log_verbose(self, msg: str) -> None:
        if self.verbose:
            self.stdout.write(msg)
