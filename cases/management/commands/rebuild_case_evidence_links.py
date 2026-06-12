"""Rebuild or verify derived case evidence-source links."""

from __future__ import annotations

from collections import defaultdict

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
        for cases_data in self._iter_case_evidence_batches(queryset):
            all_source_ids = {
                source_id for _, _, source_ids in cases_data for source_id in source_ids
            }
            existing_source_ids = set(
                DocumentSource.objects.filter(source_id__in=all_source_ids).values_list(
                    "source_id", flat=True
                )
            )

            actual_links = defaultdict(set)
            for case_id, source_id in CaseEvidenceSource.objects.filter(
                case_id__in=[case_id for case_id, _, _ in cases_data]
            ).values_list("case_id", "document_source__source_id"):
                actual_links[case_id].add(source_id)

            for case_id, case_identifier, source_ids in cases_data:
                expected = source_ids & existing_source_ids
                actual = actual_links[case_id]
                if expected != actual:
                    drift.append(
                        (
                            case_identifier,
                            sorted(expected),
                            sorted(actual),
                        )
                    )
        return drift

    def _iter_case_evidence_batches(self, queryset, batch_size=1000):
        batch = []
        for case in queryset.iterator(chunk_size=batch_size):
            source_ids = {
                source_id
                for source_id in (
                    self._evidence_source_id(item) for item in case.evidence or []
                )
                if source_id
            }
            batch.append((case.id, case.case_id, source_ids))
            if len(batch) >= batch_size:
                yield batch
                batch = []
        if batch:
            yield batch

    def _evidence_source_id(self, item):
        if not isinstance(item, dict):
            return None
        source_id = item.get("source_id")
        if not source_id:
            return None
        return str(source_id).strip()
