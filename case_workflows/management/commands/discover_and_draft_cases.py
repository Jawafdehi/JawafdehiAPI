"""
Management command to discover CIAA Special Court cases and create
Jawafdehi DRAFT cases for any that don't already exist.

Usage::

    python manage.py discover_and_draft_cases
    python manage.py discover_and_draft_cases --dry-run

Idempotent: safe to run multiple times. Existing cases (matched by the
CIAA case number appearing in the title) are skipped; only missing ones
are created.

Use --dry-run to preview what would be created without writing to the DB.
"""

from django.core.management.base import BaseCommand
from django.db import IntegrityError, transaction

from case_workflows.workflows.ciaa_caseworker.constants import CIAA_CASE_NUMBERS
from cases.models import Case, CaseState, CaseType


def _case_title(case_number: str) -> str:
    return f"CIAA Special Court Case {case_number}"


class Command(BaseCommand):
    help = (
        "Seed Jawafdehi DRAFT cases for every CIAA Special Court case number "
        "in the hardcoded discovery list. Skips any that already exist. "
        "Pass --dry-run to preview without writing to the database."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Preview what would be created without writing to the database.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        created = 0
        skipped = 0

        if dry_run:
            self.stdout.write(
                self.style.WARNING("[DRY-RUN] No changes will be written.\n")
            )

        with transaction.atomic():
            for case_number in CIAA_CASE_NUMBERS:
                # Use the canonical CIAA case number for dedup — this is
                # the same key used by import_ciaa_cases and the agents
                # workflow, ensuring cross-pipeline idempotency. The unique
                # constraint on ciaa_case_number guarantees safety under
                # concurrent invocations.
                ciaa_ref = f"special:{case_number}"
                existing = Case.objects.filter(
                    ciaa_case_number=ciaa_ref,
                ).first()

                if existing:
                    self.stdout.write(
                        f"[SKIP] {case_number} — already exists as {existing.case_id}"
                    )
                    skipped += 1
                else:
                    if dry_run:
                        self.stdout.write(
                            f"[DRY-RUN] Would create: {_case_title(case_number)}"
                        )
                    else:
                        try:
                            case, was_created = Case.objects.get_or_create(
                                ciaa_case_number=ciaa_ref,
                                defaults={
                                    "title": _case_title(case_number),
                                    "case_type": CaseType.CORRUPTION,
                                    "state": CaseState.DRAFT,
                                },
                            )
                        except IntegrityError:
                            case = Case.objects.filter(
                                ciaa_case_number=ciaa_ref,
                            ).first()
                            if case is None:
                                raise
                            was_created = False

                        if was_created:
                            self.stdout.write(
                                f"[CREATED] {case_number} → {case.case_id}"
                            )
                        else:
                            self.stdout.write(
                                f"[SKIP] {case_number} — already exists as {case.case_id}"
                            )
                            skipped += 1
                            continue
                    created += 1

            if dry_run:
                transaction.set_rollback(True)

        verb = "would be created" if dry_run else "created"
        self.stdout.write(self.style.SUCCESS(f"\n{created} {verb}, {skipped} skipped"))
