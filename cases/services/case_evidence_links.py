"""Maintain indexed links between cases and evidence document sources."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from django.db import transaction

from cases.models import Case, CaseEvidenceSource, DocumentSource


@dataclass(frozen=True)
class EvidenceLinkSyncResult:
    case_id: int
    linked: int
    missing_source_ids: tuple[str, ...]


def _evidence_entries(case: Case) -> list[tuple[int, str, str]]:
    entries = []
    seen = set()
    for index, item in enumerate(case.evidence or []):
        if not isinstance(item, dict):
            continue
        source_id = (item.get("source_id") or "").strip()
        if not source_id or source_id in seen:
            continue
        seen.add(source_id)
        description = item.get("description") or ""
        entries.append((index, source_id, description))
    return entries


def sync_case_evidence_sources(case: Case) -> EvidenceLinkSyncResult:
    """Replace derived evidence-source links for a single case."""
    entries = _evidence_entries(case)
    source_ids = [source_id for _, source_id, _ in entries]
    sources = {
        source.source_id: source
        for source in DocumentSource.objects.filter(source_id__in=source_ids)
    }
    links = [
        CaseEvidenceSource(
            case=case,
            document_source=sources[source_id],
            evidence_index=index,
            description=description,
        )
        for index, source_id, description in entries
        if source_id in sources
    ]
    missing = tuple(
        source_id for _, source_id, _ in entries if source_id not in sources
    )

    with transaction.atomic():
        CaseEvidenceSource.objects.filter(case=case).delete()
        if links:
            CaseEvidenceSource.objects.bulk_create(links)

    return EvidenceLinkSyncResult(
        case_id=case.pk, linked=len(links), missing_source_ids=missing
    )


def rebuild_case_evidence_sources(
    *,
    cases: Iterable[Case] | None = None,
) -> list[EvidenceLinkSyncResult]:
    """Rebuild derived evidence links for the provided cases or every case."""
    queryset = cases if cases is not None else Case.objects.all().order_by("pk")
    return [sync_case_evidence_sources(case) for case in queryset]
