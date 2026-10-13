"""Serialization for the material content read plane (``…/content?type=…``).

One route, three content kinds, two shapes each — a LIST of stubs, or ONE item in
full. The split is forced by size, not taste: one CIAA report's table markdown
comes to 830 KiB and a single table reaches 170 KiB, and its pages hold the
whole transcript, so a listing that inlined either would be unservable. Figures
are the exception and carry their full series in the list, because the entire
35-report corpus is 6,116 points.

Keys are snake_case — the platform's convention for its non-JSON-LD endpoints
(cf. ``/api/cases/``). This is a projection of a material, not a JSON-LD document
in its own right, so it carries no ``@context``.

What this replaces: API#514's ``/extraction`` manifest, which answered three
questions in one payload (*how much is in here*, *what tables*, *what charts*).
The two listings became their own addressable resources here; the counts are
denormalised onto ``DocumentContent`` ready for the material detail payload,
which is a FOLLOW-UP and not yet wired. Until it lands a client still has to
probe this route to learn whether a document has any content, which
is the one thing the split has not yet bought back.

**No query here pins its database alias.** The router sends ``materials`` to
``ngm`` already, and — unlike an explicit ``using()`` — it also honours the
read-replica flag that ``ReadReplicaRoutingMiddleware`` sets for anonymous
reads. These are the heaviest public payloads in the archive, so they are
exactly the reads that should be eligible for a replica rather than exempt
from it.
"""

from __future__ import annotations

from django.db.models import Prefetch

from .models import (
    DocumentContent,
    DocumentFigure,
    DocumentFigurePoint,
    DocumentPage,
    DocumentTable,
)

#: The ``type`` values ``…/content`` accepts. ``page`` is addressed by ``num``
#: (the printed folio) or by a ``from``/``to`` range; ``table`` and ``figure`` by
#: ``id`` (their stable key).
CONTENT_TYPES = ("page", "table", "figure")

#: How many pages one ``from``/``to`` request may return.
#:
#: A page range is the only call on this plane that can grow without bound: the
#: CIAA corpus averages ~1.3 KB of text per page (13 MB over 10,015 pages), so 50
#: pages is ~65 KB — the same order as the 129 KiB worst case the old manifest
#: already served — while the whole 446-page report would be ~580 KB.
#:
#: Over the cap the response is a 400 that names it, NOT a silent truncation. A
#: caller that asked for pages 30-200 and received 30-79 without being told has no
#: way to know its answer is missing three quarters of the evidence, and anything
#: summarising that result will report the gap as an absence of findings.
MAX_PAGE_SPAN = 50


# ── pages ────────────────────────────────────────────────────────────────────


def _page_stub(page: DocumentPage) -> dict:
    """A page without its text — enough to navigate, and to know what is missing.

    ``coverage_state`` and ``chars`` ride on the stub deliberately. A client
    listing 446 pages needs to see which of them we could not read BEFORE it
    fetches one and gets an empty string back, or it will present a failed OCR
    as a blank page.
    """
    return {
        "page_no": page.page_no,
        "uid": page.uid,
        "coverage_state": page.coverage_state,
        "anchor_fidelity": page.anchor_fidelity,
        "ocr_source": page.ocr_source,
        "landscape": page.landscape,
        "n_table_blocks": page.n_table_blocks,
        "chars": page.chars,
    }


def page_payload(page: DocumentPage) -> dict:
    """One page in full: the stub plus the recovered text."""
    return {**_page_stub(page), "text": page.text}


# ── tables ───────────────────────────────────────────────────────────────────


def _table_stub(table: DocumentTable) -> dict:
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


def table_payload(table: DocumentTable) -> dict:
    """One table in full: the stub plus the markdown a client renders."""
    return {**_table_stub(table), "markdown": table.markdown}


# ── figures ──────────────────────────────────────────────────────────────────


def figure_payload(figure: DocumentFigure) -> dict:
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
            # Pre-fetched by the queryset below; iterating the related manager
            # here would be an N+1 across every figure in the listing.
            for point in figure.points.all()
        ],
    }


# ── queries ──────────────────────────────────────────────────────────────────


def content_for(iri: str) -> DocumentContent | None:
    """The content row for ``iri``, or None.

    Callers MUST have resolved the material's visibility first — this applies no
    gate of its own (see ``materials.views.material_content``).
    """
    return DocumentContent.objects.filter(pk=iri).first()


def list_pages(iri: str) -> list[dict]:
    """Every page stub. ``defer("text")`` is the point: without it this reads the
    document's entire transcript out of Postgres to serialize none of it."""
    rows = DocumentPage.objects.filter(content_id=iri).defer("text")
    return [_page_stub(p) for p in rows.order_by("page_no")]


def get_page(iri: str, page_no: int) -> DocumentPage | None:
    return DocumentPage.objects.filter(content_id=iri, page_no=page_no).first()


def get_page_range(iri: str, first: int, last: int) -> list[dict]:
    """Pages ``first``..``last`` inclusive, each with its text.

    Inclusive because a page range in a citation is: "pages 30–32" is three
    pages, and a reader asking for one page as ``from=30&to=30`` should get it.
    The caller is responsible for bounding the span — see ``MAX_PAGE_SPAN``.
    """
    rows = DocumentPage.objects.filter(
        content_id=iri, page_no__gte=first, page_no__lte=last
    )
    return [page_payload(p) for p in rows.order_by("page_no")]


def list_tables(iri: str) -> list[dict]:
    """Every table stub.

    ``defer("markdown")`` is load-bearing, not a micro-optimisation: a bare query
    would pull every table's markdown — ~830 KiB for the largest CIAA report —
    out of the database only for the serializer to drop it again.
    """
    rows = DocumentTable.objects.filter(content_id=iri).defer("markdown")
    return [_table_stub(t) for t in rows.order_by("ordinal")]


def get_table(iri: str, key: str) -> DocumentTable | None:
    return DocumentTable.objects.filter(content_id=iri, key=key).first()


def _figures_queryset(iri: str):
    """Figures for one document, with their points pre-fetched.

    The Prefetch names an explicitly ORDERED queryset, not an alias: points are
    rendered in series order and a bare prefetch would leave that to the
    database. Routing is the router's job — see the module docstring.
    """
    points = DocumentFigurePoint.objects.order_by("point_index", "id")
    return (
        DocumentFigure.objects.filter(content_id=iri)
        .prefetch_related(Prefetch("points", queryset=points))
        .order_by("ordinal")
    )


def list_figures(iri: str) -> list[dict]:
    """Every figure with its full series inline.

    Inlined because they are small — 6,116 points across the entire 35-report
    corpus — which is why #514's manifest inlined them too. The asymmetry with
    tables is deliberate and measured.
    """
    return [figure_payload(f) for f in _figures_queryset(iri)]


def get_figure(iri: str, key: str) -> DocumentFigure | None:
    return _figures_queryset(iri).filter(key=key).first()


def version_token(content: DocumentContent) -> str:
    """A weak ETag for any content response.

    ``ingested_at`` alone is sufficient: the ingest replaces a document's content
    wholesale inside one transaction, so nothing about any of its rows can change
    without that timestamp moving.
    """
    return f'W/"{content.ingested_at.timestamp():.6f}"'


def content_summary(content: DocumentContent) -> dict:
    """What is inside this document, for the material detail payload.

    Reports the DENORMALISED counters, which come from the upstream document row
    rather than from ``COUNT`` over ours — see ``DocumentContent``. That is what
    keeps this cheap enough to sit on the hottest read in the archive: one LEFT
    JOIN, no aggregate, and nothing extra at all for the ~346k materials that
    have no stored content.

    ``pages_read`` is not ``pages``. A page we hold but could not transcribe is
    still a page of the document, and a reader comparing the two sees how much of
    the document is actually quotable. Collapsing them would present an
    untranscribed report as an empty one.
    """
    return {
        "pages": content.page_count,
        "pages_read": content.pages_with_text,
        "tables": content.n_tables,
        "figures": content.n_figures,
        "points": content.n_points,
        "provenance": {
            "doc_id": content.doc_id,
            "dataset": content.dataset,
            "dataset_revision": content.dataset_revision,
            "text_source": content.text_source,
            "transcript_verdict": content.transcript_verdict,
            "figures_verdict": content.figures_verdict,
            "ingested_at": content.ingested_at.isoformat(),
        },
    }
