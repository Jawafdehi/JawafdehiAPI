"""Rebuild or verify derived case evidence-source links."""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from cases.models import Case, CaseEvidenceSource, DocumentSource
from cases.services.case_evidence_links import rebuild_case_evidence_sources


class Command(BaseCommand):
    help = "Rebuild or verify indexed links from Case.evidence to DocumentSource."

    def add_arguments(self, parser):
        parser.add_argument("--check", action="store_true")
        parser.add_argument("--case-id", action="append", dest="case_ids")
        parser.add_argument("--max-errors", type=int, default=10)

    def handle(self, *args, **options):
        queryset = Case.objects.all().order_by("pk")
        if options["case_ids"]:
            queryset = queryset.filter(case_id__in=options["case_ids"])

        if options["check"]:
            drift = self._find_drift(queryset)
            if drift:
                sample = [
                    f"{case_id} expected={expected} actual={actual}"
                    for case_id, expected, actual in drift[: options["max_errors"]]
                ]
                raise CommandError(
                    f"Case evidence link drift detected for {len(drift)} case(s): "
                    f"{'; '.join(sample)}"
                )
            self.stdout.write(self.style.SUCCESS("Case evidence links are in sync."))
            return

        results = rebuild_case_evidence_sources(cases=queryset)
        missing_count = sum(len(result.missing_source_ids) for result in results)
        linked_count = sum(result.linked for result in results)
        self.stdout.write(
            self.style.SUCCESS(
                f"Rebuilt {linked_count} evidence link(s) for {len(results)} case(s)."
            )
        )
        if missing_count:
            self.stdout.write(
                self.style.WARNING(
                    f"Skipped {missing_count} evidence item(s) with missing sources."
                )
            )

    def _find_drift(self, queryset):
        drift = []
        for case in queryset.iterator():
            expected = set(
                DocumentSource.objects.filter(
                    source_id__in=[
                        item.get("source_id")
                        for item in case.evidence or []
                        if isinstance(item, dict) and item.get("source_id")
                    ]
                ).values_list("source_id", flat=True)
            )
            actual = set(
                CaseEvidenceSource.objects.filter(case=case).values_list(
                    "document_source__source_id", flat=True
                )
            )
            if expected != actual:
                drift.append(
                    (
                        case.case_id,
                        sorted(expected),
                        sorted(actual),
                    )
                )
        return drift
