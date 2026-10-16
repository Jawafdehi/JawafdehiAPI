"""The text tier: a PDF in, pages and tables out.

Every PDF here is built in-process with PyMuPDF, so the suite needs no fixture
file and no network, and each case can be a document shaped to exactly the
question being asked.

What is pinned, in order of what would hurt most if it broke:

- **pages are page-exact.** This is the whole reason the extractor reads likhit's
  fragment layer instead of its published paragraph assembler (see
  ``materials.extraction.pdf``), and nothing else in the stack would notice if it
  regressed — a transcript attributed to the wrong page is still a transcript.
- **an unread page is not a blank one.** The distinction between *nothing on this
  page* and *something we could not read* is the honesty contract of the whole
  content plane.
- **the likhit seam exists.** The extractor reaches into two names likhit does
  not export; a version bump that moves them must fail here rather than in a
  batch run over a corpus.

    DATABASE_URL=sqlite:// NES_DB_URL=sqlite:// NGM_DATABASE_URL=sqlite:// \
        uv run pytest materials/tests/test_extraction_pdf.py
"""

from __future__ import annotations

import hashlib
from unittest import TestCase

import fitz

from materials.extraction import ExtractionUnavailable, extract_pdf
from materials.extraction.bundle import PDF_EXTRACTOR_DATASET
from materials.extraction.charts import page_chart_signals
from materials.extraction.pdf import _cell_grid, _markdown_table


def _table_page(page, rows: list[list[str]], *, top: float = 140.0) -> None:
    """Draw a ruled table — a grid of stroked cells, each with its text.

    Ruled, because that is what the detector keys on: a Nepali government report
    prints its statistics in a boxed grid, and a table laid out with whitespace
    alone is not a table anything here claims to recover.
    """
    cell_w, cell_h = 150.0, 24.0
    for r, row in enumerate(rows):
        for c, cell in enumerate(row):
            rect = fitz.Rect(
                72 + c * cell_w,
                top + r * cell_h,
                72 + (c + 1) * cell_w,
                top + (r + 1) * cell_h,
            )
            page.draw_rect(rect, color=(0, 0, 0), width=0.7)
            page.insert_text((rect.x0 + 4, rect.y0 + 16), cell, fontsize=10)


def _pdf(build) -> bytes:
    doc = fitz.open()
    build(doc)
    data = doc.tobytes()
    doc.close()
    return data


class ExtractPdfTests(TestCase):
    def test_pages_keep_the_text_that_was_printed_on_them(self):
        def build(doc):
            for index in range(3):
                page = doc.new_page()
                page.insert_text((72, 90), f"Page {index + 1} body text.", fontsize=11)

        bundle = extract_pdf(_pdf(build), doc_id="three")

        self.assertEqual(bundle.page_count, 3)
        self.assertEqual([p.page_no for p in bundle.pages], [1, 2, 3])
        # The failure this guards: likhit's paragraph assembler folds several
        # pages' prose onto the first page it can match, which would show up here
        # as page 1 holding all three sentences and pages 2-3 holding none.
        for index, page in enumerate(bundle.pages, start=1):
            self.assertIn(f"Page {index} body text.", page.text)
            for other in (1, 2, 3):
                if other != index:
                    self.assertNotIn(f"Page {other} body text.", page.text)

    def test_a_page_we_could_not_read_is_not_reported_as_blank(self):
        def build(doc):
            doc.new_page().insert_text((72, 90), "Readable.", fontsize=11)
            doc.new_page()  # genuinely empty leaf
            # Ink but no text: a scanned page, or a full-page chart image.
            drawn = doc.new_page()
            drawn.draw_circle(fitz.Point(300, 400), 120, fill=(0.2, 0.4, 0.9))

        bundle = extract_pdf(_pdf(build), doc_id="mixed")

        states = {p.page_no: p.coverage_state for p in bundle.pages}
        self.assertEqual(states, {1: "anchored", 2: "blank", 3: "unread"})
        # Both empty-text pages carry "", so the text alone cannot tell them
        # apart — which is exactly why coverage_state has to.
        self.assertEqual(bundle.pages[1].text, "")
        self.assertEqual(bundle.pages[2].text, "")
        # One page unread means the document is partially read, and says so.
        self.assertEqual(bundle.transcript_verdict, "partial")
        self.assertEqual(bundle.pages_with_text, 1)

    def test_a_document_with_no_text_at_all_is_unread_not_a_failure(self):
        def build(doc):
            for _ in range(2):
                doc.new_page().draw_circle(fitz.Point(300, 400), 120, fill=(0, 0, 0))

        bundle = extract_pdf(_pdf(build), doc_id="scan")

        # It extracted: we know how many pages there are and that we read none of
        # them. Raising instead would leave the material 404-ing as "never
        # looked at", which is a different and false claim.
        self.assertEqual(bundle.page_count, 2)
        self.assertEqual(bundle.transcript_verdict, "unread")
        self.assertTrue(all(p.coverage_state == "unread" for p in bundle.pages))

    def test_a_ruled_table_becomes_markdown_with_its_header(self):
        rows = [
            ["Office", "Count", "Percent"],
            ["Tangal", "3604", "52.8"],
            ["Itahari", "548", "6.3"],
        ]

        def build(doc):
            page = doc.new_page()
            page.insert_text((72, 110), "Table 1 of complaints:", fontsize=11)
            _table_page(page, rows)

        bundle = extract_pdf(_pdf(build), doc_id="tbl")

        self.assertEqual(len(bundle.tables), 1)
        table = bundle.tables[0]
        self.assertEqual(table.page_no, 1)
        self.assertEqual(table.index_on_page, 1)
        self.assertEqual(table.uid, "tbl#p0001-t1")
        self.assertEqual(table.header, ["Office", "Count", "Percent"])
        self.assertEqual((table.n_rows, table.n_cols), (3, 3))
        self.assertIn("| Tangal | 3604 | 52.8 |", table.markdown)
        # The introducing line ends in a colon, which is how a report announces a
        # table; anything that does not match is left blank rather than guessed.
        self.assertEqual(table.caption, "Table 1 of complaints:")
        self.assertEqual(bundle.pages[0].n_table_blocks, 1)

    def test_a_table_is_rendered_into_its_page_once(self):
        rows = [["Office", "Count"], ["Tangal", "3604"]]

        def build(doc):
            page = doc.new_page()
            page.insert_text((72, 110), "Prose above the table.", fontsize=11)
            _table_page(page, rows)

        bundle = extract_pdf(_pdf(build), doc_id="once")
        text = bundle.pages[0].text

        self.assertIn("Prose above the table.", text)
        # The cells arrive as fragments AND inside the detected table. Rendered
        # once, as a table: the count of "Tangal" is 1, not 2, and it appears in
        # a pipe row rather than as a line of prose.
        self.assertEqual(text.count("Tangal"), 1)
        self.assertIn("| Tangal | 3604 |", text)
        # A page whose only content is its table must still read as a page we
        # read — hence the table riding in the page text at all.
        self.assertGreater(bundle.pages[0].chars, len("Prose above the table."))

    def test_landscape_and_provenance_ride_on_the_result(self):
        def build(doc):
            doc.new_page(width=842, height=595).insert_text(
                (72, 90), "Wide page.", fontsize=11
            )
            doc.new_page().insert_text((72, 90), "Tall page.", fontsize=11)

        data = _pdf(build)
        bundle = extract_pdf(data, doc_id="geo")

        self.assertTrue(bundle.pages[0].landscape)
        self.assertFalse(bundle.pages[1].landscape)
        self.assertEqual(bundle.dataset, PDF_EXTRACTOR_DATASET)
        # The revision IS the document: these rows were read from these bytes,
        # and a re-upload that changes the file changes the revision.
        self.assertEqual(
            bundle.dataset_revision, f"sha256:{hashlib.sha256(data).hexdigest()}"
        )

    def test_bytes_that_are_not_a_pdf_are_refused(self):
        with self.assertRaises(ExtractionUnavailable):
            extract_pdf(b"this is not a pdf", doc_id="junk")

    def test_a_password_protected_pdf_is_refused_with_a_reason(self):
        doc = fitz.open()
        doc.new_page().insert_text((72, 90), "secret", fontsize=11)
        data = doc.tobytes(
            encryption=fitz.PDF_ENCRYPT_AES_256, owner_pw="o", user_pw="u"
        )
        doc.close()

        # An encrypted document OPENS — page_count is right — and yields no text,
        # so without this guard it would be stored as a document of ``unread``
        # pages and look like a scan nobody has OCR'd. It is not: nobody can read
        # it, and a re-run will not change that.
        with self.assertRaisesRegex(ExtractionUnavailable, "password"):
            extract_pdf(data, doc_id="locked")

    def test_the_likhit_seam_this_depends_on_still_exists(self):
        # The extractor reaches into two names likhit does not export. If a bump
        # moves them, fail HERE — with the name in the message — rather than
        # part-way through a corpus run.
        from likhit.extractors.font_classifier import scan_pdf_fonts  # noqa: F401
        from likhit.extractors.kalimati import fix_kalimati_cmap  # noqa: F401
        from likhit.extractors.tables import merge_continuation_tables  # noqa: F401
        from likhit.nepali_pdf_repair import _extract_fragments_and_tables

        import inspect

        signature = inspect.signature(_extract_fragments_and_tables)
        self.assertEqual(
            list(signature.parameters), ["doc", "font_strategies", "needs_reorder"]
        )


class GridRenderingTests(TestCase):
    """The grid → markdown step, which has to survive merged cells."""

    class _Cell:
        def __init__(self, row, col, text, rowspan=1, colspan=1):
            self.row, self.col, self.text = row, col, text
            self.rowspan, self.colspan = rowspan, colspan

    class _Table:
        def __init__(self, rows, cols, cells):
            self.row_count, self.col_count, self.cells = rows, cols, cells

    def test_a_merged_header_cell_covers_its_whole_span(self):
        table = self._Table(
            2,
            3,
            [
                self._Cell(0, 0, "Complaints", colspan=3),
                self._Cell(1, 0, "a"),
                self._Cell(1, 1, "b"),
                self._Cell(1, 2, "c"),
            ],
        )

        grid = _cell_grid(table)

        # Repeated, not left as a short row: markdown has no colspan, and a row
        # one cell wide where the next is three shifts every column under it.
        self.assertEqual(grid[0], ["Complaints", "Complaints", "Complaints"])
        self.assertEqual(grid[1], ["a", "b", "c"])

    def test_a_cell_keeps_its_row_when_it_contains_a_pipe_or_a_newline(self):
        table = self._Table(
            2, 2, [
                self._Cell(0, 0, "रु.\nकरोड"),
                self._Cell(0, 1, "a|b"),
                self._Cell(1, 0, "1"),
                self._Cell(1, 1, "2"),
            ]
        )

        markdown = _markdown_table(_cell_grid(table))
        header = markdown.splitlines()[0]

        # A literal newline would end the row; an unescaped pipe would add a
        # column that is not there. Both silently mis-align the whole table.
        self.assertEqual(header, "| रु. करोड | a\\|b |")
        self.assertEqual(len(markdown.splitlines()), 3)

    def test_a_table_with_no_dimensions_renders_to_nothing(self):
        self.assertEqual(_cell_grid(self._Table(0, 0, [])), [])
        self.assertEqual(_markdown_table([]), "")


class ChartDetectionTests(TestCase):
    def test_a_caption_names_the_figure_it_announces(self):
        doc = fitz.open()
        page = doc.new_page()
        try:
            signals = page_chart_signals(page, "चित्र २.१४\nउजुरीको विषय क्षेत्रगत विवरण")
        finally:
            doc.close()

        # The whole numeral, not just its first digit: the manifest shows this
        # string to whoever triages a false positive, and "चित्र २" points at a
        # chapter rather than a chart.
        self.assertEqual(signals.get("captions"), ["चित्र २.१४"])

    def test_an_ordinary_word_containing_the_caption_token_is_not_a_chart(self):
        doc = fitz.open()
        page = doc.new_page()
        try:
            signals = page_chart_signals(page, "चित्रकार र पार्श्वचित्र को विवरण")
        finally:
            doc.close()

        # `re` has no word boundary across Devanagari, so the numeral anchor is
        # the only thing keeping "painter" and "profile" out of the worklist.
        self.assertEqual(signals, {})
