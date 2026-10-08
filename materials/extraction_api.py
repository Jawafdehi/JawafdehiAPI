"""Serialization for the document-extraction read plane.

Two representations, because one would be unusable:

* the **manifest** (``/extraction``) — provenance, every figure with its full
  series, and a *stub* per table. Cheap enough to fetch on page load.
* a **table** (``/extraction/tables/<ordinal>``) — one table's markdown, fetched
  when a reader opens it.

The split is forced by size, not taste. One CIAA report's tables come to 830 KiB
of markdown and a single table reaches 170 KiB, so inlining them would make the
manifest unservable; the figures, by contrast, total 6k points across the whole
corpus and comfortably fit.

Keys are snake_case — the platform's convention for its non-JSON-LD endpoints
(cf. ``/api/cases/``). This is a projection of a material, not a JSON-LD document
in its own right, so it carries no ``@context``.
"""

from __future__ import annotations

from django.db.models import Prefetch

from .models import DocumentExtraction, ExtractedFigure, ExtractedTable


def _table_stub(table: ExtractedTable) -> dict:
    """A table without its markdown — enough to list, label and link it."""
    return {
        # ``ordinal`` is presentation order and is recomputed on every ingest;
        # ``key`` is what a client must address a table by. See the model.
        "ordinal": table.ordinal,
        "key": table.key,
        "uid": table.uid,
        "page_no": table.page_no,
        "index_on_page": table.index_on_page,
        "caption": table.caption,
        "header": table.header,
        "n_rows": table.n_rows,
        "n_cols": table.n_cols,
        "fidelity": table.fidelity,
    }


def table_payload(table: ExtractedTable) -> dict:
    """One table in full: the stub plus the markdown a client renders."""
    return {**_table_stub(table), "markdown": table.markdown}


def _figure_payload(figure: ExtractedFigure) -> dict:
    """One chart and its series.

    ``estimated`` rides on every point rather than being summarised at the figure
    level: a single chart routinely mixes printed values with ones read off the
    image, and collapsing that to one per-figure flag would misreport both kinds.
    """
    return {
        "ordinal": figure.ordinal,
        "key": figure.key,
        "uid": figure.uid,
        "page_no": figure.page_no,
        "index_on_page": figure.index_on_page,
        "title": figure.title,
        "chart_type": figure.chart_type,
        "unit": figure.unit,
        "x_axis": figure.x_axis,
        "y_axis": figure.y_axis,
        "notes": figure.notes,
        "verify_note": figure.verify_note,
        "verified": figure.verified,
        "points": [
            {
                "point_index": point.point_index,
                "label": point.label,
                "series": point.series,
                "value": point.value,
                "estimated": point.is_estimated,
            }
            # Pre-fetched by manifest_payload; iterating the related manager here
            # would be an N+1 across every figure on the page.
            for point in figure.points.all()
        ],
    }


def load_extraction(iri: str) -> DocumentExtraction | None:
    """Fetch the extraction for ``iri`` with its tables, figures and points.

    Callers MUST have resolved the material's visibility first — this function
    applies no gate of its own (see ``materials.views.material_extraction``).
    """
    return (
        DocumentExtraction.objects.using("ngm")
        .filter(pk=iri)
        .prefetch_related(
            # ``defer("markdown")`` is the point of this Prefetch, not a
            # micro-optimisation: a bare ``prefetch_related("tables")`` would pull
            # every table's markdown out of the database — ~830 KiB for the
            # largest CIAA report — only for the serializer to drop it again.
            Prefetch(
                "tables",
                queryset=ExtractedTable.objects.using("ngm").defer("markdown"),
            ),
            "figures__points",
        )
        .first()
    )


def manifest_payload(extraction: DocumentExtraction) -> dict:
    tables = list(extraction.tables.all())
    figures = list(extraction.figures.all())
    return {
        "material": extraction.material_id,
        "provenance": {
            "dataset": extraction.dataset,
            "dataset_revision": extraction.dataset_revision,
            "doc_id": extraction.doc_id,
            "page_count": extraction.page_count,
            "text_source": extraction.text_source,
            "transcript_verdict": extraction.transcript_verdict,
            "figures_verdict": extraction.figures_verdict,
            "ingested_at": extraction.ingested_at.isoformat(),
        },
        "counts": {
            "tables": len(tables),
            "figures": len(figures),
            "points": sum(len(f.points.all()) for f in figures),
        },
        "tables": [_table_stub(t) for t in tables],
        "figures": [_figure_payload(f) for f in figures],
    }


def version_token(extraction: DocumentExtraction) -> str:
    """A weak ETag for the manifest.

    ``ingested_at`` alone is sufficient: the ingest replaces an extraction
    wholesale inside one transaction, so nothing about the payload can change
    without that timestamp moving.
    """
    return f'W/"{extraction.ingested_at.timestamp():.6f}"'
