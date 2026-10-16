"""The text tier: a PDF's pages and tables, read off the document itself.

Deterministic — no network, no model, no cost per call — which is what makes it
usable both in a batch over a whole source and on demand for one document a
reader just asked for.

**Why this goes through likhit's fragment layer and not ``page.get_text()``.**
The archive's documents are Nepali, and a large share of them are typeset in
legacy encodings: on one CIAA annual report the fonts resolve to
``{'Kalimati': 'broken_cmap', 'Himalb': 'legacy_remap', 'Times New Roman':
'correct', …}``. PyMuPDF's own text layer for those pages is mojibake that still
*looks* like text — it has a length, it tokenises, it would pass any emptiness
check — so an extractor built on it would fill the archive with confident
nonsense. ``likhit`` exists to repair exactly that, and this reuses it rather
than re-deriving it.

**Why the fragment layer specifically**, rather than
``extract_repaired_text_blocks``, which is the published entry point: that
function attributes an assembled *paragraph* to a page by matching its text back
against the fragments, and paragraphs are built across page boundaries. Measured
on pages 30-36 of the FY2081/82 report, it put 7,546 characters on page 1 of the
slice and one table-only block on each of pages 2, 3, 5 and 7 — the prose of five
pages folded onto the first. Fragments carry their own ``page_number`` and
reproduce the per-page character counts PyMuPDF reports (1,342 against 1,404 on
that first page, the difference being table text), so pages are grouped from
them instead. ``folded`` is in the upstream's ``coverage_state`` vocabulary for
precisely this failure; we avoid it rather than record it.

The cost of that choice is a dependency on two names likhit does not export
(``_extract_fragments_and_tables`` and the font/cmap helpers it composes).
:func:`_repaired_fragments_and_tables` is the only place that reaches in, and it
fails as :class:`ExtractionUnavailable` with the missing name rather than an
AttributeError from somewhere deep in a batch run. ``test_extraction_pdf`` pins
the seam against a real PDF, so a likhit bump that moves it fails a test instead
of a corpus.
"""

from __future__ import annotations

import hashlib
import logging
import re

from .bundle import (
    PDF_EXTRACTOR_DATASET,
    ContentBundle,
    PageRecord,
    TableRecord,
)
from .charts import detect_chart_pages

logger = logging.getLogger("materials.extraction")

#: Lines that introduce a table, used to pick a caption off the page when the
#: extractor does not supply one. Deliberately narrow: a line is accepted only
#: when it names a table in Nepali or English, or ends in a colon the way an
#: introducing sentence does. Anything looser attaches the page's last sentence
#: to the table as though it were its title, which is worse than no caption —
#: a caption is read as the publisher's own label for the data.
_CAPTION_PATTERN = re.compile(
    r"(?:तालिका|सारणी|तालीका)\s*(?:नं\.?|न\.|सङ्ख्या|संख्या|:)?\s*[\d०-९]"
    r"|\b(?:Table|Tbl\.?)\s*[\d IVX]"
    r"|[:ः]\s*$",
    re.IGNORECASE,
)

#: Pages whose text layer decoded to something that does not read as Nepali are
#: still stored, flagged ``suspect`` — see ``_page_fidelity``. The threshold is
#: likhit's own (``PdfPageAnalysis.has_suspicious_text_layer``); we do not have a
#: second opinion and inventing one here would just be a different arbitrary line.


class ExtractionUnavailable(RuntimeError):
    """This document cannot be extracted at all — not "extracted to nothing".

    The distinction is the whole point of the class: a scanned report with no
    text layer extracts *successfully* to a document of ``unread`` pages, which
    is a useful, true answer and the worklist the vision tier needs. This is
    raised only when there is nothing to work with — the bytes are not a PDF, the
    file is encrypted, or likhit's seam has moved under us — so a caller can tell
    "we failed" from "we looked and the pages are images".
    """


def _open(data: bytes):
    import fitz

    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception as exc:  # noqa: BLE001 - fitz raises its own error types
        raise ExtractionUnavailable(f"not a readable PDF: {exc}") from exc
    if doc.needs_pass:
        doc.close()
        raise ExtractionUnavailable("PDF is password-protected")
    if doc.page_count == 0:
        doc.close()
        raise ExtractionUnavailable("PDF has no pages")
    return doc


def _repaired_fragments_and_tables(data: bytes):
    """likhit's page-exact fragments + detected tables for the whole document.

    Mirrors the first half of ``likhit.nepali_pdf_repair.extract_repaired_text_blocks``
    — scan the fonts, repair a broken Kalimati cmap, extract — and stops before
    the paragraph assembly that loses page attribution (see the module docstring).
    """
    try:
        from likhit.extractors.font_classifier import scan_pdf_fonts
        from likhit.extractors.kalimati import fix_kalimati_cmap
        from likhit.extractors.tables import merge_continuation_tables
        from likhit.nepali_pdf_repair import _extract_fragments_and_tables
    except ImportError as exc:  # pragma: no cover - a likhit bump, not a document
        raise ExtractionUnavailable(f"likhit's extraction seam has moved: {exc}") from exc

    import fitz

    doc = fitz.open(stream=data, filetype="pdf")
    try:
        strategies = scan_pdf_fonts(doc)
        needs_reorder = False
        if any(strategy == "broken_cmap" for strategy in strategies.values()):
            # Returns a NEW document with the cmap patched; the original is
            # superseded, so rebind rather than keeping both.
            doc, needs_reorder = fix_kalimati_cmap(doc)
        fragments, tables = _extract_fragments_and_tables(
            doc, strategies, needs_reorder=needs_reorder
        )
    except ExtractionUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 - likhit raises ExtractionError + others
        raise ExtractionUnavailable(f"could not read this PDF's text: {exc}") from exc
    finally:
        doc.close()
    # Continuation merging is likhit's: a table broken across a page boundary is
    # one table, and it keeps a region per page so we can still say where it
    # starts. Without it the second half of a long table arrives as a headerless
    # table of its own, which a reader would cite as a table in its own right.
    return fragments, strategies, merge_continuation_tables(tables)


def _legacy_fonts(strategies: dict[str, str]) -> set[str]:
    """Font names whose text only reads correctly because likhit remapped it."""
    return {
        name
        for name, strategy in (strategies or {}).items()
        if strategy in {"broken_cmap", "legacy_remap"}
    }


def _page_uses_legacy_font(page, legacy: set[str]) -> bool:
    if not legacy:
        return False
    try:
        names = {entry[3] for entry in page.get_fonts(full=False)}
    except Exception:  # noqa: BLE001 - a malformed font table is not fatal
        return False
    # PyMuPDF reports subset fonts as ``ABCDEF+Himalb``; the strategy map is keyed
    # on the base name, so compare on the part after the subset tag.
    return any(name.split("+")[-1] in legacy for name in names)


def _cell_grid(table) -> list[list[str]]:
    """Dense ``row × col`` grid of cell text, spans expanded.

    A merged header cell occupies several columns in the rendered table, and the
    extractor reports it once with a ``colspan``. Repeating its text across the
    span keeps every row the same width, which is what a markdown table requires;
    dropping the span instead silently shifts every later column left, so the
    numbers under a header stop being the numbers that header labels.
    """
    rows = max(int(getattr(table, "row_count", 0) or 0), 0)
    cols = max(int(getattr(table, "col_count", 0) or 0), 0)
    if not rows or not cols:
        return []
    grid = [["" for _ in range(cols)] for _ in range(rows)]
    for cell in getattr(table, "cells", None) or []:
        row = int(getattr(cell, "row", 0) or 0)
        col = int(getattr(cell, "col", 0) or 0)
        if not (0 <= row < rows and 0 <= col < cols):
            continue
        text = _one_line(getattr(cell, "text", "") or "")
        for dr in range(max(int(getattr(cell, "rowspan", 1) or 1), 1)):
            for dc in range(max(int(getattr(cell, "colspan", 1) or 1), 1)):
                if row + dr < rows and col + dc < cols:
                    grid[row + dr][col + dc] = text
    return grid


def _one_line(text: str) -> str:
    """Collapse a cell's internal line breaks.

    A cell's text legitimately wraps in the source (``क्र.\\nसं.``), and a literal
    newline inside a markdown pipe cell ends the row — the table would render as
    two broken ones.
    """
    return " ".join(text.split())


def _markdown_table(grid: list[list[str]]) -> str:
    """GFM pipe table from a dense grid, header row first.

    Markdown rather than a cell grid because the published artefact is a
    *rendered* table and that is what a reader is checking against; the grid is
    kept only long enough to build this.
    """
    if not grid:
        return ""

    def row(cells: list[str]) -> str:
        # Escape the delimiter: a cell containing "|" (common in a column header
        # that lists units) would otherwise introduce a column that is not there
        # and shift the whole row.
        return "| " + " | ".join(c.replace("|", "\\|") for c in cells) + " |"

    width = len(grid[0])
    lines = [row(grid[0]), "| " + " | ".join(["---"] * width) + " |"]
    lines.extend(row(r) for r in grid[1:])
    return "\n".join(lines)


def _in_region(fragment, region) -> bool:
    """Whether a text fragment sits inside a detected table's box.

    Compared on the fragment's CENTRE, with likhit's own 1.5pt tolerance
    (``handlers.content_blocks._fragment_in_region``) — a cell's text frequently
    overhangs its rule by a fraction of a point, and a corner-based test drops
    the first column of a tightly-set table. Re-stated here rather than imported
    because it is six lines of geometry and this is already reaching into one
    private name more than it would like.
    """
    center_x = (float(fragment.x0) + float(fragment.x1)) / 2
    center_y = (float(fragment.y0) + float(fragment.y1)) / 2
    return (
        float(region.x0) - 1.5 <= center_x <= float(region.x1) + 1.5
        and float(region.y0) - 1.5 <= center_y <= float(region.y1) + 1.5
    )


def _table_caption(table, page_lines: list[tuple[float, str]]) -> str:
    """The prose introducing this table, where the page prints any.

    Takes the nearest matching line ABOVE the table, because that is where a
    Nepali report prints a table's label. Returns "" rather than guessing — see
    ``_CAPTION_PATTERN``.
    """
    declared = (getattr(table, "caption", "") or "").strip()
    if declared:
        return declared
    regions = getattr(table, "regions", None) or []
    if not regions:
        return ""
    top = min(float(getattr(r, "y0", 0.0) or 0.0) for r in regions)
    candidates = [
        text for y0, text in page_lines if y0 < top and _CAPTION_PATTERN.search(text)
    ]
    return candidates[-1].strip() if candidates else ""


def extract_pdf(
    data: bytes,
    *,
    doc_id: str,
    source_url: str = "",
    detect_charts: bool = True,
) -> ContentBundle:
    """Extract one PDF's pages and tables into a :class:`ContentBundle`.

    ``dataset_revision`` is the SHA-256 of ``data``. The provenance field is
    documented as "dataset coordinates + a commit sha", and for a document we
    extracted ourselves the content-addressed revision *is* the document: it says
    which bytes these rows were read from, so a re-upload that changes the file
    is visible as a changed revision rather than as rows that silently no longer
    describe the PDF a reader can download.

    Raises :class:`ExtractionUnavailable` when the bytes cannot be opened at all.
    A PDF that opens and yields no text is NOT an error — it comes back as pages
    marked ``unread``.
    """
    from likhit.pdf_page_analysis import analyze_pdf_pages

    doc = _open(data)
    try:
        page_count = doc.page_count
        geometry = {}
        for index in range(page_count):
            page = doc[index]
            rect = page.rect
            geometry[index + 1] = {
                "landscape": rect.width > rect.height,
                "has_ink": bool(page.get_images(full=True)) or bool(page.get_drawings()),
            }
        legacy_by_page = {}
        fragments, strategies, tables = _repaired_fragments_and_tables(data)
        legacy = _legacy_fonts(strategies)
        for index in range(page_count):
            legacy_by_page[index + 1] = _page_uses_legacy_font(doc[index], legacy)
    finally:
        doc.close()

    analyses = {a.page_number: a for a in analyze_pdf_pages(data)}

    # ── group the repaired text by the page it was printed on ────────────────
    fragments_by_page: dict[int, list] = {}
    for fragment in fragments:
        page_no = int(getattr(fragment, "page_number", 0) or 0)
        if page_no <= 0:
            # A fragment whose page is unknown is dropped rather than attached to
            # page 1: it would make that page's text a mixture no citation could
            # be checked against, which is the failure this whole approach exists
            # to avoid.
            continue
        if (getattr(fragment, "text", "") or "").strip():
            fragments_by_page.setdefault(page_no, []).append(fragment)

    def page_lines(page_no: int) -> list[tuple[float, str]]:
        return [
            (float(getattr(f, "y0", 0.0) or 0.0), (f.text or "").strip())
            for f in fragments_by_page.get(page_no, [])
        ]

    # ── tables, numbered per page ────────────────────────────────────────────
    table_records: list[TableRecord] = []
    per_page_index: dict[int, int] = {}
    tables_by_page: dict[int, list[TableRecord]] = {}
    regions_by_page: dict[int, list] = {}
    for table in tables:
        page_no = int(getattr(table, "page_number", 0) or 0)
        if page_no <= 0:
            continue
        grid = _cell_grid(table)
        markdown = _markdown_table(grid)
        if not markdown.strip():
            # An empty frame — ruled lines with no recovered text. Storing it
            # would advertise a table a client cannot read anything out of.
            continue
        index_on_page = per_page_index.get(page_no, 0) + 1
        per_page_index[page_no] = index_on_page
        record = TableRecord(
            page_no=page_no,
            index_on_page=index_on_page,
            uid=f"{doc_id}#p{page_no:04d}-t{index_on_page}",
            caption=_table_caption(table, page_lines(page_no)),
            header=list(grid[0]) if grid else [],
            n_rows=len(grid),
            n_cols=len(grid[0]) if grid else 0,
            # The text layer decoded; where it needed a legacy remap to do so,
            # say that rather than calling it tight.
            fidelity="repaired" if legacy_by_page.get(page_no) else "tight",
            markdown=markdown,
        )
        table_records.append(record)
        tables_by_page.setdefault(page_no, []).append(record)
        regions_by_page.setdefault(page_no, []).extend(
            getattr(table, "regions", None) or []
        )

    # ── pages ────────────────────────────────────────────────────────────────
    pages: list[PageRecord] = []
    for page_no in range(1, page_count + 1):
        text = _page_text(
            fragments_by_page.get(page_no, []),
            regions_by_page.get(page_no, []),
            tables_by_page.get(page_no, []),
        )
        geo = geometry.get(page_no, {})
        pages.append(
            PageRecord(
                page_no=page_no,
                uid=f"{doc_id}#p{page_no:04d}",
                text=text,
                coverage_state=_coverage_state(text, geo.get("has_ink", False)),
                anchor_fidelity=_page_fidelity(
                    text, legacy_by_page.get(page_no, False), analyses.get(page_no)
                ),
                ocr_source="text_layer" if text else "",
                landscape=bool(geo.get("landscape")),
                n_table_blocks=per_page_index.get(page_no, 0),
                chars=len(text),
            )
        )

    chart_pages = (
        detect_chart_pages(
            data, {page_no: page_lines(page_no) for page_no in fragments_by_page}
        )
        if detect_charts
        else []
    )

    bundle = ContentBundle(
        doc_id=doc_id,
        dataset=PDF_EXTRACTOR_DATASET,
        dataset_revision=f"sha256:{hashlib.sha256(data).hexdigest()}",
        page_count=page_count,
        text_source="likhit",
        pages=pages,
        tables=table_records,
        chart_pages=chart_pages,
    )
    bundle.transcript_verdict = _transcript_verdict(pages)
    # Nothing here reads a chart. Say which of the two silences this is: no chart
    # pages at all, or chart pages nobody has read. A zero count would claim we
    # looked inside the images and found no data.
    bundle.figures_verdict = "not_attempted" if chart_pages else "none_detected"
    logger.info(
        "extracted %s: %s (source=%s)", doc_id, bundle.summary(), source_url or "bytes"
    )
    return bundle


def _page_text(fragments: list, regions: list, tables: list[TableRecord]) -> str:
    """One page as a reader meets it: its prose, then its tables, rendered once.

    A table's cell text arrives as ordinary fragments as well as in the detected
    table, so the two have to be reconciled or the page says everything twice —
    measured on a synthetic 3×3 table, the page's "prose" was the nine cells one
    per line, which reads as neither prose nor a table. Fragments inside a
    detected table's box are therefore dropped from the prose and the table's
    markdown is appended instead.

    **Appended, not dropped.** A page whose only content is a table is common in
    a statistical annex, and if its table went only to the table rows the page
    would report ``chars=0`` and come back ``unread`` — a page we read perfectly
    well, filed as one we failed to read.
    """
    prose = [
        (float(getattr(f, "y0", 0.0) or 0.0), (f.text or "").strip())
        for f in fragments
        if not any(_in_region(f, region) for region in regions)
    ]
    blocks = [line for _y0, line in sorted(prose, key=lambda item: item[0]) if line]
    blocks.extend(table.markdown for table in tables if table.markdown.strip())
    return "\n".join(blocks)


def _coverage_state(text: str, has_ink: bool) -> str:
    """What this page is, when its text is empty.

    ``blank`` and ``unread`` are both "no text" and are not the same fact: one is
    a page with nothing on it, the other a page with something on it we did not
    read. Collapsing them is how an untranscribed scan gets served as an empty
    page, and an absence of transcription gets reported as an absence of findings.
    """
    if text.strip():
        return "anchored"
    return "unread" if has_ink else "blank"


def _page_fidelity(text: str, legacy: bool, analysis) -> str:
    if not text.strip():
        return ""
    if analysis is not None and getattr(analysis, "has_suspicious_text_layer", False):
        # Stored, not discarded: a page we read badly is still evidence that the
        # page exists and roughly what is on it, and a reader told the read is
        # suspect can go to the PDF. Dropping it would leave a silent hole.
        return "suspect"
    return "repaired" if legacy else "tight"


def _transcript_verdict(pages: list[PageRecord]) -> str:
    if not any(page.text.strip() for page in pages):
        return "unread"
    if any(page.coverage_state == "unread" for page in pages):
        return "partial"
    return "clean"
