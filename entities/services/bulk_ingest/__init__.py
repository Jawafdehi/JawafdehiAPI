"""Bulk-ingestion service for sourcing many public entities at once.

Ported from ``nes.services.bulk_ingest``. The I/O shapes (``IngestRecord`` /
``IngestSource`` / ``BulkIngestResult``) are framework-agnostic and copied
verbatim; ``BulkIngestService`` keeps the ≥2-source HOLD gate, the eTLD+1
publisher-key independence rule, the first-wins ``deduped_in_batch`` logic and
the held-entity staging — only the async ``EntityDatabase`` writes became
synchronous Django ``EntityRepository`` calls.

This is the batch write path that retires the per-entity migration runner
(decision Q10): large public-entity sources land here, not via a migration.
"""

from entities.services.bulk_ingest.ingest import BulkIngestService
from entities.services.bulk_ingest.models import (
    BulkIngestResult,
    IngestRecord,
    IngestRecordError,
    IngestSource,
    IngestStatus,
)

__all__ = [
    "BulkIngestService",
    "BulkIngestResult",
    "IngestRecord",
    "IngestRecordError",
    "IngestSource",
    "IngestStatus",
]
