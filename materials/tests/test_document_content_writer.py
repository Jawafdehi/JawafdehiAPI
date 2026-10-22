"""The one writer: a bundle becomes a document's stored rows.

``materials.document_content.store_document_content`` is shared by the parquet
corpus ingest and the PDF extractor, so what is pinned here holds for both:

- the public ``key`` a client addresses a table by, and the fact that it is
  derived from the page rather than from the ordinal;
- wholesale replacement, so a re-extract cannot leave a dropped table behind;
- whose counters get stored — the producer's own witness where there is one, our
  rows where there is not;
- the material's ``updated_at`` moving, because its ETag is derived from it.

    DATABASE_URL=sqlite:// NES_DB_URL=sqlite:// NGM_DATABASE_URL=sqlite:// \
        uv run pytest materials/tests/test_document_content_writer.py
"""

from __future__ import annotations

from django.test import TestCase

from materials.document_content import store_document_content
from materials.extraction.bundle import (
    ContentBundle,
    DeclaredCounts,
    FigurePointRecord,
    FigureRecord,
    PageRecord,
    TableRecord,
)
from materials.jsonld import MATERIAL_CONTEXT
from materials.models import (
    DocumentContent,
    DocumentFigure,
    DocumentFigurePoint,
    DocumentPage,
    DocumentTable,
    Material,
)

SOURCE = "ciaa_annual_report"
IDENT = "ee0b4f80b24b8665"
IRI = f"https://jawafdehi.org/material/{SOURCE}/{IDENT}"


def _material() -> Material:
    return Material.objects.create(
        iri=IRI,
        material_type="document",
        source=SOURCE,
        ident=IDENT,
        data={
            "@context": MATERIAL_CONTEXT,
            "@type": "DigitalDocument",
            "@id": IRI,
            "name": {"ne": "पैँतिसौँ वार्षिक प्रतिवेदन"},
        },
    )


def _bundle(**kwargs) -> ContentBundle:
    defaults = dict(
        doc_id=IDENT,
        dataset="jawafdehi:pdf-extract/v1",
        dataset_revision="sha256:abc",
        page_count=2,
        text_source="likhit",
        transcript_verdict="clean",
        figures_verdict="none_detected",
        pages=[
            PageRecord(
                page_no=1,
                uid=f"{IDENT}#p0001",
                text="पहिलो पृष्ठ",
                coverage_state="anchored",
                anchor_fidelity="repaired",
                ocr_source="text_layer",
                n_table_blocks=1,
                chars=11,
            ),
            PageRecord(
                page_no=2,
                uid=f"{IDENT}#p0002",
                text="",
                coverage_state="unread",
            ),
        ],
        tables=[
            TableRecord(
                page_no=30,
                index_on_page=1,
                uid=f"{IDENT}#p0030-t1",
                caption="तालिका २.१",
                header=["आ.व.", "संख्या"],
                n_rows=3,
                n_cols=2,
                fidelity="repaired",
                markdown="| आ.व. | संख्या |\n| --- | --- |\n| २०८१/८२ | १९४८८ |",
            )
        ],
    )
    defaults.update(kwargs)
    return ContentBundle(**defaults)


class StoreDocumentContentTests(TestCase):
    databases = {"default", "ngm"}

    def setUp(self):
        self.material = _material()

    def test_rows_land_with_the_public_key_derived_from_the_page(self):
        written = store_document_content(IRI, _bundle())

        self.assertEqual(
            written, {"pages": 2, "tables": 1, "figures": 0, "points": 0}
        )
        table = DocumentTable.objects.get(content_id=IRI)
        # The key is where the table SITS, so a later revision that recovers an
        # earlier table does not silently move a reader's saved link onto a
        # different table — which is what addressing by ordinal would do.
        self.assertEqual(table.key, "p0030-t1")
        self.assertEqual(table.ordinal, 1)
        self.assertEqual(DocumentPage.objects.filter(content_id=IRI).count(), 2)
        self.assertEqual(
            list(
                DocumentPage.objects.filter(content_id=IRI)
                .order_by("page_no")
                .values_list("coverage_state", flat=True)
            ),
            ["anchored", "unread"],
        )

    def test_ordinals_follow_the_document_whatever_order_the_producer_emitted(self):
        bundle = _bundle(
            tables=[
                TableRecord(page_no=40, index_on_page=1, markdown="| b |"),
                TableRecord(page_no=12, index_on_page=2, markdown="| a2 |"),
                TableRecord(page_no=12, index_on_page=1, markdown="| a1 |"),
            ]
        )

        store_document_content(IRI, bundle)

        rows = list(
            DocumentTable.objects.filter(content_id=IRI)
            .order_by("ordinal")
            .values_list("ordinal", "key")
        )
        # Reading order, not emission order: a producer that yields its rows by
        # some internal ordering still gets presentation ordinals a client can
        # page through.
        self.assertEqual(rows, [(1, "p0012-t1"), (2, "p0012-t2"), (3, "p0040-t1")])

    def test_a_re_extract_replaces_the_document_wholesale(self):
        store_document_content(IRI, _bundle())
        first = DocumentContent.objects.get(pk=IRI).ingested_at

        store_document_content(
            IRI,
            _bundle(
                dataset_revision="sha256:def",
                tables=[],  # the new revision lost the table
                pages=[PageRecord(page_no=1, uid=f"{IDENT}#p0001", text="नयाँ")],
            ),
        )

        # Delete-then-insert: the dropped table is GONE. An upsert keyed on uid
        # would have left it behind forever, advertising a table the current
        # document does not contain.
        self.assertEqual(DocumentTable.objects.filter(content_id=IRI).count(), 0)
        self.assertEqual(DocumentPage.objects.filter(content_id=IRI).count(), 1)
        content = DocumentContent.objects.get(pk=IRI)
        self.assertEqual(content.dataset_revision, "sha256:def")
        self.assertGreater(content.ingested_at, first)

    def test_figure_points_stay_with_their_figure(self):
        bundle = _bundle(
            figures=[
                FigureRecord(
                    page_no=30,
                    index_on_page=1,
                    title="उजुरी",
                    chart_type="bar",
                    verified=True,
                    points=[
                        FigurePointRecord(point_index=1, label="२०८०/८१", value=19580.0),
                        FigurePointRecord(
                            point_index=2, label="२०८१/८२", value=19488.0, estimated=True
                        ),
                    ],
                ),
                FigureRecord(
                    page_no=31,
                    index_on_page=1,
                    title="फर्छ्यौट",
                    points=[FigurePointRecord(point_index=1, label="क", value=1.0)],
                ),
            ],
            figures_verdict="read",
        )

        written = store_document_content(IRI, bundle)

        self.assertEqual((written["figures"], written["points"]), (2, 3))
        first = DocumentFigure.objects.get(content_id=IRI, key="p0030-f1")
        self.assertEqual(first.points.count(), 2)
        # ``estimated`` rides per point: a chart mixes printed values with ones
        # read off the axis, and a per-figure flag would misreport both.
        self.assertEqual(
            sorted(first.points.values_list("point_index", "is_estimated")),
            [(1, False), (2, True)],
        )
        self.assertEqual(
            DocumentFigure.objects.get(content_id=IRI, key="p0031-f1").points.count(), 1
        )
        self.assertEqual(DocumentFigurePoint.objects.count(), 3)

    def test_an_unverified_figure_is_null_not_false(self):
        store_document_content(
            IRI, _bundle(figures=[FigureRecord(page_no=1, title="x")])
        )

        # "not checked" and "checked and found wanting" are different claims and
        # the UI renders them differently.
        self.assertIsNone(DocumentFigure.objects.get(content_id=IRI).verified)

    def test_declared_counts_win_over_the_rows_we_wrote(self):
        bundle = _bundle(
            declared=DeclaredCounts(
                tables=2, figures=1, points=2, pages_with_text=1
            )
        )

        store_document_content(IRI, bundle)

        content = DocumentContent.objects.get(pk=IRI)
        # The producer had an independent witness that says two tables; we wrote
        # one. Storing OUR number would report the shortfall as the truth and
        # make the gap invisible — the caller compares and reports it instead.
        self.assertEqual((content.n_tables, content.n_figures), (2, 1))
        self.assertEqual(content.n_points, 2)

    def test_without_a_witness_the_counters_come_from_the_rows(self):
        store_document_content(IRI, _bundle())

        content = DocumentContent.objects.get(pk=IRI)
        # The PDF extractor is the only thing that has looked, so its row count
        # is not a self-report — it is the measurement.
        self.assertEqual(content.n_tables, 1)
        self.assertEqual(content.pages_with_text, 1)  # page 2 is unread
        self.assertEqual(content.page_count, 2)

    def test_the_material_is_touched_so_its_etag_moves(self):
        before = Material.objects.get(pk=IRI).updated_at

        store_document_content(IRI, _bundle())

        # The detail payload carries the content summary and its ETag comes from
        # updated_at alone; without this the body changes and the validator does
        # not, and every cache on the path serves the old counts indefinitely.
        self.assertGreater(Material.objects.get(pk=IRI).updated_at, before)

    def test_over_long_vocabulary_values_are_cut_rather_than_fatal(self):
        bundle = _bundle(
            text_source="x" * 80,
            pages=[
                PageRecord(
                    page_no=1,
                    uid="u" * 300,
                    text="t",
                    coverage_state="c" * 80,
                )
            ],
        )

        store_document_content(IRI, bundle)

        # These fields carry another project's enum values. A corpus must not
        # fail on a verdict string longer than we guessed.
        self.assertEqual(len(DocumentContent.objects.get(pk=IRI).text_source), 40)
        page = DocumentPage.objects.get(content_id=IRI)
        self.assertEqual((len(page.uid), len(page.coverage_state)), (160, 40))
