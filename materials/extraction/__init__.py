"""Turning a source document into the content the read plane serves.

``materials.content_api`` answers *what is inside this document* out of
``DocumentContent`` + its pages, tables and figures. Until now exactly one thing
could put rows there: ``ingest_document_content``, which loads a hand-mapped
corpus of parquet somebody else extracted. This package is the other half — it
extracts that same shape **from the document itself**, so any material carrying a
PDF can be opened, not just the 35 CIAA annual reports.

Three pieces, deliberately separate:

- :mod:`.bundle` — the interchange shape every producer emits and the one writer
  (``materials.document_content``) consumes. The parquet ingest builds one too,
  so there is a single place where a page/table/figure becomes a row, and a
  single definition of the URL key a client addresses a table by.
- :mod:`.pdf` — the **text tier**: pages and tables read off the PDF, via
  likhit's font repair. Deterministic, no network, no model, so it can run in
  batch over the archive or on demand for one document.
- :mod:`.charts` — chart-page detection and page rendering: the worklist the
  **vision tier** needs. Reading the numbers off a chart image is a model call
  per page and is NOT done here (see the package docs in
  ``docs/2026-10-16-document-extraction-pipeline/``); what this does is say which
  pages carry charts, so that work is a bounded list rather than 446 pages.

The split in tiers is the honesty requirement, not an implementation detail. A
page we hold and could not read comes back ``coverage_state="unread"`` with empty
text, and the document says ``figures_verdict="not_attempted"`` — never a zero
count, which would assert we looked. ~94% of the archive has never been read;
if the tiers reported identically, an absence of transcription would be served
as an absence of findings.
"""

from __future__ import annotations

from .bundle import (
    ContentBundle,
    DeclaredCounts,
    FigurePointRecord,
    FigureRecord,
    PageRecord,
    TableRecord,
    content_key,
)
from .pdf import ExtractionUnavailable, extract_pdf

__all__ = [
    "ContentBundle",
    "DeclaredCounts",
    "ExtractionUnavailable",
    "FigurePointRecord",
    "FigureRecord",
    "PageRecord",
    "TableRecord",
    "content_key",
    "extract_pdf",
]
