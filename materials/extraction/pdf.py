"""The text tier: a PDF's pages and tables, read off the document itself.

Deterministic — no network, no model, no cost per call — which is what makes it
usable both in a batch over a whole source and on demand for one document a
reader just asked for.

**Why this goes through likhit and not ``page.get_text()``.** The archive's
documents are Nepali, and a large share are typeset in legacy encodings: on one
CIAA annual report the fonts resolve to ``{'Kalimati': 'broken_cmap', 'Himalb':
'legacy_remap', 'Times New Roman': 'correct', …}``, and the Auditor General's
56th annual report is Preeti throughout. PyMuPDF's own text layer for those pages
is mojibake that still *looks* like text — it has a length, it tokenises, it
would pass any emptiness check — so an extractor built on it would fill the
archive with confident nonsense. ``likhit`` exists to repair exactly that.

**Why ``FontBasedStrategy`` and not ``extract_repaired_text_blocks``**, which is
the obvious published entry point: that function attributes an assembled
*paragraph* to a page by matching its text back against the fragments, and
paragraphs are built across page boundaries. Measured on pages 30-36 of the
FY2081/82 CIAA report, it put 7,546 characters on page 1 of the slice and one
table-only block on each of pages 2, 3, 5 and 7 — the prose of five pages folded
onto the first. ``folded`` is in the upstream's ``coverage_state`` vocabulary for
precisely this failure; pages are grouped from fragments instead, which carry
their own ``page_number`` and reproduce PyMuPDF's per-page character counts.

An earlier version of this module composed that function's first half by hand,
out of ``nepali_pdf_repair._extract_fragments_and_tables``. That reached into a
private name AND skipped two things the strategy does — see :func:`_read_document`
— one of which cost 1.7 MB of mojibake on a real Auditor General report.

**The text is only as good as the installed likhit, and that is measurable.**
The same 517-page report, same code path, two revisions: the pinned
``5ae527711c`` yields 0.1% Devanagari (mojibake), ``main`` yields 78.6%
(readable Nepali) — the v19 extractor line is what reads that font family. So
fidelity is judged on the text we PRODUCED rather than on the fonts we found
(:func:`_fidelity`), which is what lets a reader see the difference instead of
inheriting it.
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
from .redact import redact_bundle, redaction_summary

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

#: Pages whose text decoded to something that does not read as Nepali are stored
#: anyway, flagged ``suspect`` — see :func:`_fidelity`, where the ORDER of that
#: judgement matters more than the threshold. The threshold itself is likhit's
#: (``PdfPageAnalysis.has_suspicious_text_layer``); we have no second opinion, and
#: inventing one here would just be a different arbitrary line.


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


def _read_document(data: bytes):
    """likhit's page-exact fragments, tables, and the pages it could not read.

    ``FontBasedStrategy.extract_text`` is the strategy the Nepali-PDF converter
    itself runs, reached through the ``ExtractionStrategy`` interface rather than
    through any private name. Two things it does that the ``nepali_pdf_repair``
    helper does not, both of which we were missing:

    - **content-based legacy detection.** A legacy font that the name table has
      no opinion about is reinterpreted from what its spans actually spell. Some
      faces are routed this way *deliberately* — likhit's own name table carries
      a "DO NOT add spins" note explaining that content detection picks a better
      map for that family than any single name-table entry could — so a path that
      skips it cannot read those documents at all.
    - **``needs_ocr_pages``**, which includes pages carrying a *decoy* text layer:
      a scanned raster with a junk core-font layer over it. Inferring "unread"
      from "no text" alone would store that junk as the page's transcript.

    Continuation merging is already applied by the strategy, so a table broken
    across a page boundary arrives as one table with a region per page rather
    than as a headerless second table a reader would cite on its own.

    Takes a path, hence the temp file: a cheap write against minutes of parsing.
    """
    try:
        from likhit.extractors.font_based import FontBasedStrategy
    except ImportError as exc:  # pragma: no cover - a likhit bump, not a document
        raise ExtractionUnavailable(
            f"likhit's extraction strategy has moved: {exc}"
        ) from exc

    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".pdf") as handle:
        handle.write(data)
        handle.flush()
        try:
            raw = FontBasedStrategy().extract_text(handle.name)
        except ExtractionUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - likhit raises ExtractionError + others
            # The strategy refuses a document with no text layer at all
            # ("No text content found in document"). For us that is not a
            # failure — it is a scan, and the right answer is a document of
            # ``unread`` pages plus the chart worklist, which is what the vision
            # tier needs. Decided by looking at the PDF rather than by matching
            # the message: if NO page has a text layer it is a scan, and if some
            # page does then the strategy fell over on something real and we must
            # not file a readable document as unreadable.
            if _has_any_text_layer(data):
                raise ExtractionUnavailable(
                    f"could not read this PDF's text: {exc}"
                ) from exc
            return _NothingRead()
    return raw


class _NothingRead:
    """A document whose every page is a scan: no fragments, no tables.

    ``needs_ocr_pages`` is left empty rather than filled with every page number —
    :func:`_coverage_state` reaches the same verdict from "no text and there is
    ink on the page", and a list asserting we checked each page individually
    would be a claim this path did not make.
    """

    fragments: list = []
    tables: list = []
    needs_ocr_pages: list = []


def _has_any_text_layer(data: bytes) -> bool:
    import fitz

    doc = fitz.open(stream=data, filetype="pdf")
    try:
        return any(doc[index].get_text().strip() for index in range(doc.page_count))
    finally:
        doc.close()


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
    redact: bool = True,
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
    from likhit.pdf_page_analysis import analyze_pdf_pages, page_max_image_coverage

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
                # A picture big enough to be the page's content rather than a
                # logo. Carried per page because of the one omission nothing
                # else here can see — see _ocr_source.
                "image_cover": page_max_image_coverage(page),
            }
    finally:
        doc.close()

    raw = _read_document(data)
    fragments, tables = raw.fragments, raw.tables
    # likhit's own verdict on which pages carry no born-digital text — including
    # a scanned page wearing a decoy text layer, which no amount of looking at
    # the text we got back would reveal.
    needs_ocr = {int(p) for p in (raw.needs_ocr_pages or [])}

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
            # Same judgement as the page's, from one function and on the same
            # evidence — the table's own cells. Two notions of fidelity on one
            # page would eventually disagree about the same characters.
            fidelity=_fidelity(markdown, analyses.get(page_no)),
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
                coverage_state=_coverage_state(
                    # Coerced: the geometry dict became heterogeneous when
                    # image_cover (a float) joined it, so the inferred type of
                    # any .get() off it widened past bool.
                    text,
                    bool(geo.get("has_ink", False)),
                    page_no in needs_ocr,
                ),
                anchor_fidelity=_fidelity(text, analyses.get(page_no)),
                ocr_source=_ocr_source(text, float(geo.get("image_cover", 0.0) or 0.0)),
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
    if redact:
        # AFTER the pages are judged, never before. A placeholder is ASCII, so
        # redacting first would drag a page's Devanagari share down and could
        # report a removal as a failed decode — the same confusion likhit's own
        # quality module strips placeholders to avoid. Fidelity describes the
        # read; redaction is what happened to the text afterwards.
        bundle.redactions = redaction_summary(redact_bundle(bundle))
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


#: Share of a page one image must cover before the page is treated as carrying
#: a picture that IS content. likhit uses 0.85 to call a page image-dominant
#: (i.e. a scan); this is far lower because the case it catches is different — a
#: scanned table pasted into an otherwise born-digital page, which is a quarter
#: of the paper and all of the numbers.
_EMBEDDED_IMAGE_COVER = 0.25


def _ocr_source(text: str, image_cover: float) -> str:
    """Where this page's text came from — and whether that is all of the page.

    ``text_layer+image`` is the one verdict here that reports an OMISSION rather
    than a source, and it exists because that omission is otherwise invisible by
    construction: the page has a text layer, so it is ``anchored`` and every
    other field says it was read, while a scan pasted into it carries numbers
    nobody transcribed. A reader who sees ``anchored`` on such a page has been
    told the page was read. It was not — only the part of it that was typed.

    No judgement is passed on what the image contains; we cannot know without
    reading it. The flag says there is something here we did not read, which is
    the same thing ``unread`` says about a whole page.
    """
    if not text.strip():
        return ""
    return "text_layer+image" if image_cover >= _EMBEDDED_IMAGE_COVER else "text_layer"


def _coverage_state(text: str, has_ink: bool, needs_ocr: bool) -> str:
    """What this page is — and, when its text is empty, which kind of empty.

    ``blank`` and ``unread`` are both "no text" and are not the same fact: one is
    a page with nothing on it, the other a page with something on it we did not
    read. Collapsing them is how an untranscribed scan gets served as an empty
    page, and an absence of transcription gets reported as an absence of findings.

    ``needs_ocr`` is likhit's own verdict and outranks the text, because of the
    one case the text cannot reveal: a scanned page wearing a *decoy* core-font
    layer. That page yields text, and the text is junk — "anchored" would file a
    scan nobody has read as a page we read.
    """
    if needs_ocr:
        return "unread"
    if text.strip():
        return "anchored"
    return "unread" if has_ink else "blank"


def _fidelity(text: str, analysis) -> str:
    """How much to trust this page's text — judged on the text we PRODUCED.

    The tempting version of this reads the input: flag the page when likhit's
    ``has_suspicious_text_layer`` fires. Both ways of wiring that are wrong, and
    each was measured on the Auditor General shelf:

    - **Flag on suspicion first** and a repaired page is called ``suspect``. The
      56th annual report is typeset in Preeti, so all 608 pages report
      ``devanagari_char_count=0`` with a suspicious Latin ratio while the
      repaired text is clean Nepali — 554 of its 586 readable pages would carry
      a warning telling a reader that a correct audit finding is probably junk.
    - **Flag only fonts the name table knows** and the opposite happens. The 48th
      report is typeset in Spins, which likhit deliberately leaves out of that
      table, and under the pinned revision it extracts to 0.1% Devanagari — 1.7 MB
      of mojibake that this function would have called ``tight``.

    So neither the font nor the raw layer decides it: the output does. Text that
    came out as Devanagari was read, whatever it took to get there; text that was
    mojibake going in and is still not Devanagari coming out was not. A page of
    genuinely Latin text — the shelf has an English-language report — is clean,
    and is separated from mojibake by the raw layer's own suspicion flag.

    Either way the text is stored. A page we read badly still says the page
    exists and roughly what is on it, and a reader told the read is suspect can
    go to the PDF; dropping it would leave a silent hole instead.
    """
    if not text.strip():
        return ""
    suspicious_input = analysis is not None and getattr(
        analysis, "has_suspicious_text_layer", False
    )
    if _devanagari_share(text) >= _DEVANAGARI_FLOOR:
        # Read. ``repaired`` where it took a remap to get here, so a reader can
        # tell a page that needed work from one that did not.
        return "repaired" if suspicious_input else "tight"
    if suspicious_input:
        return "suspect"
    return "tight"


#: Share of a page's characters that must be Devanagari for the page to count as
#: read. Deliberately low: a page of a Nepali audit report is full of Western
#: digits, rupee figures and English agency names, and the two documents measured
#: here sit at 0.1% (mojibake) and 72-80% (read) — three orders of magnitude
#: apart, so nothing near this line is a close call.
_DEVANAGARI_FLOOR = 0.15

_DEVANAGARI = re.compile(r"[ऀ-ॿ]")


def _devanagari_share(text: str) -> float:
    stripped = "".join(text.split())
    if not stripped:
        return 0.0
    return len(_DEVANAGARI.findall(stripped)) / len(stripped)


def _transcript_verdict(pages: list[PageRecord]) -> str:
    if not any(page.text.strip() for page in pages):
        return "unread"
    if any(page.coverage_state == "unread" for page in pages):
        return "partial"
    return "clean"
