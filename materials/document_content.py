"""The one writer: a :class:`ContentBundle` becomes a document's stored content.

Every producer goes through :func:`store_document_content` — the parquet corpus
ingest, the PDF extractor, and anything added later. One writer is not tidiness:
the ``key`` a client addresses a table by is a public URL, the delete-then-insert
contract is what makes a re-ingest safe, and the material's ETag has to move when
the counts do. Three rules that must hold identically for every producer, so they
live in one place rather than being re-implemented per command.

Not in this module: coercing another project's columns. A producer owns its own
source's conventions (parquet NaN, JSON-string headers, Devanagari numerals) and
hands over clean Python; this normalises only what the *model* needs — field
widths, ordinals, keys.
"""

from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone

from .extraction.bundle import ContentBundle, content_key
from .models import (
    DocumentContent,
    DocumentFigure,
    DocumentFigurePoint,
    DocumentPage,
    DocumentTable,
    Material,
)

logger = logging.getLogger("materials.document_content")

#: Chunk size for the two bulk_creates that can get large: pages (one row carries
#: a whole page of transcript) and points. For MEMORY, not for sqlite's bound
#: parameter cap — Django already clamps ``batch_size`` down to what the backend
#: admits. What it does not do is keep one statement from being enormous.
BULK_BATCH = 500


#: Characters Postgres refuses inside a text value. A PDF's text layer can
#: legitimately contain NUL — a broken encoder, a padded font table, a damaged
#: stream — and psycopg rejects the whole INSERT with "A string literal cannot
#: contain NUL (0x00) characters".
#:
#: 🛑 **The test gate cannot catch this.** sqlite stores NUL happily, so an
#: extraction that passes every test here fails on the first production document
#: that carries one, as a whole job dying on a document that is otherwise
#: perfectly readable. Stripped at the writer because this is the one place every
#: producer funnels through.
_FORBIDDEN = str.maketrans({"\x00": None})


def _clean(value: str) -> str:
    """Text as Postgres will accept it. Never changes a readable character."""
    return (value or "").translate(_FORBIDDEN)


def _fit(value: str, width: int) -> str:
    """Truncate to the model's width. Applied to the short provenance/vocabulary
    fields only: those carry another project's enum values, and a corpus must not
    fail on a verdict string longer than we guessed."""
    return _clean(value)[:width]


def _uid(explicit: str, doc_id: str, suffix: str) -> str:
    """The producer's own id for this row, or one derived from where it sits.

    ``uid`` is unique per document at the database level, so a producer that
    leaves it blank does not get empty strings — it gets a second row that fails
    the constraint, which is a confusing way to learn that the field was
    mandatory. Where the producer has no id of its own (the PDF extractor, whose
    only id for a page IS its page number), the row's own handle is both unique
    by construction and honest about its origin.
    """
    return (explicit or "").strip() or f"{doc_id}#{suffix}"


def _ordered(records, kind: str):
    """Records in presentation order, each with its ordinal and public key.

    Sorted by where the thing sits in the document, so ``ordinal`` is reading
    order even when a producer emitted its rows some other way. ``key`` comes
    from :func:`content_key` — never from the ordinal, which moves between
    revisions (see that function).
    """
    rows = sorted(
        records, key=lambda r: (int(r.page_no or 0), int(r.index_on_page or 1))
    )
    return [
        (ordinal, content_key(int(r.page_no or 0), int(r.index_on_page or 1), kind), r)
        for ordinal, r in enumerate(rows, start=1)
    ]


@transaction.atomic(using="ngm")
def store_document_content(iri: str, bundle: ContentBundle) -> dict[str, int]:
    """Replace ``iri``'s stored content with ``bundle``. Returns rows written.

    **Delete-then-insert, inside one transaction.** A producer can lose a table
    between revisions, and an upsert keyed on ``uid`` would leave the dropped row
    behind forever; the cascade from ``DocumentContent`` takes the pages, tables,
    figures and points with it. One transaction because a half-written document
    is worse than an unwritten one — the content tab would appear on part of a
    report with nothing in the response to say which part.

    Atomic ``using="ngm"`` is load-bearing, not decoration: these models route to
    ``ngm``, and a bare ``atomic()`` opens a transaction on ``default`` while
    leaving the ``ngm`` connection in autocommit — so a failure halfway would
    commit the rows written so far.
    """
    DocumentContent.objects.using("ngm").filter(pk=iri).delete()

    declared = bundle.declared
    content = DocumentContent.objects.using("ngm").create(
        material_id=iri,
        doc_id=_fit(bundle.doc_id, 120),
        dataset=_fit(bundle.dataset, 200),
        dataset_revision=_fit(bundle.dataset_revision, 80),
        page_count=max(int(bundle.page_count or 0), 0),
        text_source=_fit(bundle.text_source, 40),
        transcript_verdict=_fit(bundle.transcript_verdict, 40),
        figures_verdict=_fit(bundle.figures_verdict, 40),
        # When the producer has an INDEPENDENT witness to what the document
        # contains (the parquet corpus has one: the dataset's own ``documents``
        # row), store that witness's numbers rather than a count of the rows we
        # managed to write. The two agree today and the difference is the whole
        # point: a shortfall means we failed to ingest part of what the source
        # says exists, and a count taken from our own rows would report the
        # shortfall as the truth and make the gap invisible.
        #
        # The PDF extractor has no second witness — it is the only thing that has
        # looked — so there it falls through to the rows, which are then not a
        # self-report but the measurement itself.
        n_tables=declared.tables if declared else len(bundle.tables),
        n_figures=declared.figures if declared else len(bundle.figures),
        n_points=declared.points if declared else bundle.n_points,
        pages_with_text=(
            declared.pages_with_text if declared else bundle.pages_with_text
        ),
    )

    DocumentPage.objects.using("ngm").bulk_create(
        [
            DocumentPage(
                content=content,
                page_no=max(int(page.page_no or 0), 0),
                uid=_fit(
                    _uid(page.uid, bundle.doc_id, f"p{int(page.page_no or 0):04d}"),
                    160,
                ),
                text=_clean(page.text),
                coverage_state=_fit(page.coverage_state, 40),
                anchor_fidelity=_fit(page.anchor_fidelity, 40),
                ocr_source=_fit(page.ocr_source, 40),
                landscape=bool(page.landscape),
                n_table_blocks=max(int(page.n_table_blocks or 0), 0),
                chars=max(int(page.chars or 0), 0),
            )
            for page in sorted(bundle.pages, key=lambda p: int(p.page_no or 0))
        ],
        batch_size=BULK_BATCH,
    )

    DocumentTable.objects.using("ngm").bulk_create(
        [
            DocumentTable(
                content=content,
                ordinal=ordinal,
                key=key,
                uid=_fit(_uid(table.uid, bundle.doc_id, key), 160),
                page_no=max(int(table.page_no or 0), 0),
                index_on_page=max(int(table.index_on_page or 1), 0),
                caption=_clean(table.caption),
                header=[
                    _clean(cell) if isinstance(cell, str) else cell
                    for cell in (table.header or [])
                ],
                n_rows=max(int(table.n_rows or 0), 0),
                n_cols=max(int(table.n_cols or 0), 0),
                fidelity=_fit(table.fidelity, 40),
                markdown=_clean(table.markdown),
            )
            for ordinal, key, table in _ordered(bundle.tables, "t")
        ],
        batch_size=BULK_BATCH,
    )

    figure_rows = [
        (
            key,
            DocumentFigure(
                content=content,
                ordinal=ordinal,
                key=key,
                uid=_fit(_uid(figure.uid, bundle.doc_id, key), 160),
                page_no=max(int(figure.page_no or 0), 0),
                index_on_page=max(int(figure.index_on_page or 1), 0),
                title=_clean(figure.title),
                chart_type=_fit(figure.chart_type, 40),
                unit=_fit(figure.unit, 80),
                x_axis=_fit(figure.x_axis, 300),
                y_axis=_fit(figure.y_axis, 300),
                notes=_clean(figure.notes),
                verify_note=_clean(figure.verify_note),
                verified=figure.verified,
            ),
            figure,
        )
        for ordinal, key, figure in _ordered(bundle.figures, "f")
    ]
    DocumentFigure.objects.using("ngm").bulk_create(
        [row for _key, row, _src in figure_rows]
    )

    # bulk_create does not populate PKs on every backend, so re-read the ids by
    # key rather than trusting the in-memory objects. Keyed on ``key`` and not
    # ``uid``: the key is unique per document by construction and is not
    # truncated, where an over-long uid is — and pairing on a truncated string
    # would silently drop every point of the figure that was cut.
    figure_ids = dict(
        DocumentFigure.objects.using("ngm")
        .filter(content=content)
        .values_list("key", "id")
    )
    points = [
        DocumentFigurePoint(
            figure_id=figure_ids[key],
            point_index=max(int(point.point_index or 0), 0),
            label=_fit(point.label, 300),
            series=_fit(point.series, 300),
            value=point.value,
            is_estimated=bool(point.estimated),
        )
        for key, _row, source in figure_rows
        for point in source.points
    ]
    DocumentFigurePoint.objects.using("ngm").bulk_create(points, batch_size=BULK_BATCH)

    # Touch the material's own updated_at. Its detail payload carries the content
    # summary and its ETag — which is ALSO the If-Match token for a conditional
    # PATCH — is derived from updated_at alone. Without this a re-extract changes
    # the body and not the validator, and every cache on the path serves the old
    # counts indefinitely.
    #
    # .update(), not .save(): a save fires the post_save search indexer, and
    # nothing in the search document comes from this content, so that work would
    # be pure cost. auto_now does not fire on .update(), hence the explicit stamp.
    Material.objects.using("ngm").filter(pk=iri).update(updated_at=timezone.now())

    written = {
        "pages": len(bundle.pages),
        "tables": len(bundle.tables),
        "figures": len(figure_rows),
        "points": len(points),
    }
    logger.info("stored content for %s: %s", iri, written)
    return written
