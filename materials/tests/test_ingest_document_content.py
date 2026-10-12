"""The ``ingest_document_content`` management command.

The command's contract is "resolve everything, then write, or write nothing":
a half-ingested corpus would put the data tab on some reports and not others
with no way to tell which from outside. These tests pin that, plus idempotency
(re-ingest must not duplicate or renumber) and the shrug-off handling of the
upstream's ragged fields.

Fixtures are real parquet files written by duckdb into a tmp dir, so the test
exercises the same read path production does.

    DATABASE_URL=sqlite:// NES_DB_URL=sqlite:// NGM_DATABASE_URL=sqlite:// \
        uv run pytest materials/tests/test_ingest_document_content.py
"""

from __future__ import annotations

import json
import math
from io import StringIO
from unittest.mock import patch
from pathlib import Path

import duckdb
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from materials.management.commands import ingest_document_content as mod
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
IDENT = "8._28th_ciaa_annual_report_2074_75"
DOC_ID = "ciaa-2074-75"


def _write_parquet(directory: Path, name: str, columns: list[str], rows: list[tuple]):
    """Materialise ``rows`` as ``<name>.parquet`` via duckdb.

    Real parquet, not a stub, so the command's duckdb read path is the one under
    test. Values are inlined as SQL literals — these are fixtures the test owns,
    not input from anywhere.
    """
    con = duckdb.connect(":memory:")
    names = ", ".join(f'"{c}"' for c in columns)
    if rows:
        values = ", ".join(
            "(" + ", ".join(_literal(v) for v in row) + ")" for row in rows
        )
        con.execute(f"CREATE TABLE out AS SELECT * FROM (VALUES {values}) AS t({names})")
    else:
        empty = ", ".join(f'NULL AS "{c}"' for c in columns)
        con.execute(f"CREATE TABLE out AS SELECT {empty} WHERE 1=0")
    con.execute(f"COPY out TO '{directory / f'{name}.parquet'}' (FORMAT PARQUET)")


def _literal(value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        # duckdb has no bare NaN/Infinity literal; it wants the cast spelling.
        return f"CAST('{value}' AS DOUBLE)"
    if isinstance(value, (int, float)):
        return str(value)
    escaped = str(value).replace("'", "''")
    return f"'{escaped}'"


DOC_COLUMNS = [
    "doc_id",
    "n_pages",
    "text_source",
    "transcript_quality_verdict",
    "figures_quality_verdict",
    # The counters the material detail payload reports from. They are the
    # upstream's own numbers, deliberately NOT len() over the rows we write —
    # see the note in the command.
    "n_tables",
    "n_figures",
    "n_figure_data_points",
    "pages_with_text",
]
PAGE_COLUMNS = [
    "page_uid",
    "doc_id",
    "page_no",
    "text",
    "coverage_state",
    "anchor_fidelity",
    "ocr_source",
    "landscape",
    "n_table_blocks",
    "chars",
]
TABLE_COLUMNS = [
    "table_uid",
    "doc_id",
    "page_no",
    "table_index_on_page",
    "intro_text",
    "header_json",
    "n_rows",
    "n_cols",
    "anchor_fidelity",
    "markdown",
]
FIGURE_COLUMNS = [
    "figure_uid",
    "doc_id",
    "page_no",
    "figure_index_on_page",
    "title_ne",
    "chart_type",
    "unit",
    "x_axis",
    "y_axis",
    "notes",
    "verify_note",
    "verified_clean",
]
POINT_COLUMNS = ["figure_uid", "point_index", "label", "series", "value", "value_estimated"]


class IngestCommandTests(TestCase):
    databases = "__all__"

    def setUp(self):
        self.dir = Path(self.mk_tmp())
        self.iri = f"https://jawafdehi.org/material/{SOURCE}/{IDENT}"
        Material.objects.create(
            iri=self.iri,
            material_type="document",
            source=SOURCE,
            ident=IDENT,
            data={
                "@context": MATERIAL_CONTEXT,
                "@type": "DigitalDocument",
                "@id": self.iri,
                "name": {"ne": "२८औं वार्षिक प्रतिवेदन"},
            },
        )
        self._write_dataset()
        self.map_path = self.dir / "map.json"
        self.map_path.write_text(
            json.dumps(
                {
                    "dataset": "damo-da/ciaa-annual-reports",
                    "source": SOURCE,
                    "documents": {DOC_ID: IDENT},
                }
            )
        )

    def mk_tmp(self) -> str:
        import tempfile

        tmp = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, tmp, True)
        return tmp

    def _write_dataset(self, *, extra_doc: bool = False):
        docs = [(DOC_ID, 255, "likhit", "clean", "trustworthy", 2, 1, 2, 2)]
        if extra_doc:
            docs.append(("ciaa-2075-76", 328, "likhit", "clean", "", 0, 0, 0, 0))
        _write_parquet(self.dir, "documents", DOC_COLUMNS, docs)
        _write_parquet(
            self.dir,
            "pages",
            PAGE_COLUMNS,
            [
                # Page order is asserted from the data, so write them out of it.
                (
                    f"{DOC_ID}#p0019",
                    DOC_ID,
                    19,
                    "",  # held but unread — empty text is about US, not the page
                    "ocr_unread",
                    "",
                    "",
                    False,
                    0,
                    0,
                ),
                (
                    f"{DOC_ID}#p0018",
                    DOC_ID,
                    18,
                    "आयोगमा दर्ता भएका उजुरी १९४८८",
                    "anchored",
                    "tight",
                    "pdf_text_layer",
                    False,
                    1,
                    29,
                ),
            ],
        )
        _write_parquet(
            self.dir,
            "tables",
            TABLE_COLUMNS,
            [
                # Page order, not uid order: ordinal must follow the document.
                (
                    f"{DOC_ID}#p0019#t1",
                    DOC_ID,
                    19,
                    1,
                    "दोस्रो तालिका",
                    '["क", "ख"]',
                    8,
                    5,
                    "tight",
                    "| क |\n| --- |",
                ),
                (
                    f"{DOC_ID}#p0018#t1",
                    DOC_ID,
                    18,
                    1,
                    None,  # a table with no intro prose at all
                    "not valid json",  # a header we must shrug off, not die on
                    9,
                    4,
                    "ocr_vision",
                    "| आ.व. |\n| --- |\n| १९४८८ |",
                ),
            ],
        )
        _write_parquet(
            self.dir,
            "figures",
            FIGURE_COLUMNS,
            [
                (
                    f"{DOC_ID}#p0018#f1",
                    DOC_ID,
                    18,
                    1,
                    "उजुरीको संख्या",
                    "bar",
                    "संख्या",
                    "",
                    "",
                    "a note",
                    "",
                    True,
                )
            ],
        )
        _write_parquet(
            self.dir,
            "figure_data",
            POINT_COLUMNS,
            [
                (f"{DOC_ID}#p0018#f1", 2, "२०७४/७५", "उजुरी", 19488.0, True),
                (f"{DOC_ID}#p0018#f1", 1, "२०७३/७४", "उजुरी", 19580.0, False),
            ],
        )

    def _run(self, **kwargs):
        out = StringIO()
        call_command(
            "ingest_document_content",
            map=str(self.map_path),
            parquet_dir=str(self.dir),
            revision="abc1234",
            stdout=out,
            **kwargs,
        )
        return out.getvalue()

    # ── happy path ────────────────────────────────────────────────────────────

    def test_ingests_tables_figures_and_points(self):
        self._run()
        content = DocumentContent.objects.get(pk=self.iri)
        self.assertEqual(content.doc_id, DOC_ID)
        self.assertEqual(content.dataset_revision, "abc1234")
        self.assertEqual(content.page_count, 255)
        self.assertEqual(content.tables.count(), 2)
        self.assertEqual(content.figures.count(), 1)
        self.assertEqual(DocumentFigurePoint.objects.count(), 2)

    def test_ingests_pages_with_their_text(self):
        self._run()
        content = DocumentContent.objects.get(pk=self.iri)
        self.assertEqual(content.pages.count(), 2)
        page = content.pages.get(page_no=18)
        self.assertIn("१९४८८", page.text)
        self.assertEqual(page.coverage_state, "anchored")
        self.assertEqual(page.ocr_source, "pdf_text_layer")

    def test_an_unread_page_is_stored_as_unread_not_as_blank(self):
        # A page we hold but could not transcribe is not a page with nothing on
        # it. Losing that distinction at ingest means no amount of care in the
        # API can recover it, and the absence of OCR gets read as an absence of
        # findings.
        self._run()
        page = DocumentPage.objects.get(page_no=19)
        self.assertEqual(page.text, "")
        self.assertEqual(page.coverage_state, "ocr_unread")

    def test_pages_are_addressed_by_their_printed_folio(self):
        # Not by insertion order: the fixture writes page 19 before page 18.
        self._run()
        self.assertEqual(
            list(DocumentPage.objects.values_list("page_no", flat=True)), [18, 19]
        )

    def test_counts_come_from_the_upstream_not_from_our_own_rows(self):
        # The fixture's document row claims 2 tables / 1 figure / 2 points, which
        # is what it writes. Make the upstream disagree and the stored counters
        # must follow the UPSTREAM: a mismatch means we failed to ingest part of
        # what the dataset says exists, and a count derived from our own rows
        # would report the shortfall as the truth and hide it.
        # setUp already wrote the dataset; replace only the document row.
        _write_parquet(
            self.dir,
            "documents",
            DOC_COLUMNS,
            [(DOC_ID, 255, "likhit", "clean", "trustworthy", 99, 98, 97, 96)],
        )
        self._run()
        content = DocumentContent.objects.get(pk=self.iri)
        self.assertEqual(content.n_tables, 99)
        self.assertEqual(content.n_figures, 98)
        self.assertEqual(content.n_points, 97)
        self.assertEqual(content.pages_with_text, 96)
        # ...while the rows we actually wrote are unchanged.
        self.assertEqual(content.tables.count(), 2)

    def test_a_shortfall_against_the_upstream_is_reported(self):
        # Storing the upstream's counter keeps our shortfall from redefining
        # itself as the truth. It does not make the shortfall acceptable — if
        # nobody is told, the API advertises 99 tables a client cannot fetch and
        # the only trace is a log line nobody re-reads.
        _write_parquet(
            self.dir,
            "documents",
            DOC_COLUMNS,
            [(DOC_ID, 255, "likhit", "clean", "trustworthy", 99, 98, 97, 96)],
        )
        out = self._run()
        self.assertIn("disagree with the rows we wrote", out)
        self.assertIn("upstream says 99 tables, we stored 2", out)
        self.assertIn("97 missing", out)

    def test_the_happy_path_says_nothing_about_counters(self):
        # The fixture agrees with itself, so a clean run must stay quiet — a
        # warning that fires every time is one nobody reads when it matters.
        out = self._run()
        self.assertNotIn("disagree", out)

    def test_holding_more_than_upstream_counted_is_not_a_shortfall(self):
        # Upstream undercounting its own data costs a reader nothing: every row
        # we hold is still fetchable. Only the other direction hides something.
        _write_parquet(
            self.dir,
            "documents",
            DOC_COLUMNS,
            [(DOC_ID, 255, "likhit", "clean", "trustworthy", 1, 0, 0, 96)],
        )
        out = self._run()
        self.assertNotIn("disagree", out)

    def test_ordinals_follow_document_order_not_file_order(self):
        self._run()
        ordered = list(
            DocumentTable.objects.order_by("ordinal").values_list("ordinal", "page_no")
        )
        self.assertEqual(ordered, [(1, 18), (2, 19)])

    def test_points_keep_their_estimated_flag_and_order(self):
        self._run()
        points = list(
            DocumentFigurePoint.objects.order_by("point_index").values_list(
                "point_index", "value", "is_estimated"
            )
        )
        self.assertEqual(points, [(1, 19580.0, False), (2, 19488.0, True)])

    def test_malformed_header_json_degrades_to_empty_not_an_error(self):
        self._run()
        by_page = {t.page_no: t for t in DocumentTable.objects.all()}
        self.assertEqual(by_page[19].header, ["क", "ख"])
        self.assertEqual(by_page[18].header, [])

    def test_null_caption_becomes_empty_string(self):
        self._run()
        self.assertEqual(DocumentTable.objects.get(page_no=18).caption, "")

    def test_nan_chart_value_becomes_null_not_a_nan_float(self):
        # A NaN survives float(), stores in a FloatField, and then renders as a
        # bare ``NaN`` token — which is not valid JSON and which a strict client
        # parser rejects. It has to land as null.
        _write_parquet(
            self.dir,
            "figure_data",
            POINT_COLUMNS,
            [(f"{DOC_ID}#p0018#f1", 1, "x", "s", float("nan"), False)],
        )
        self._run()
        point = DocumentFigurePoint.objects.get()
        self.assertIsNone(point.value)
        self.assertIn('"value": null', json.dumps({"value": point.value}))

    def test_estimated_flag_is_not_invented_from_a_null(self):
        # A bare bool() would make this True (bool(nan) is True), presenting a
        # printed figure as a value guessed off a chart image — the exact
        # misreport the flag exists to prevent.
        _write_parquet(
            self.dir,
            "figure_data",
            POINT_COLUMNS,
            [(f"{DOC_ID}#p0018#f1", 1, "x", "s", 1.0, None)],
        )
        self._run()
        self.assertFalse(DocumentFigurePoint.objects.get().is_estimated)

    def test_null_verified_flag_stays_null(self):
        # "not checked" and "checked and found wanting" are different claims.
        _write_parquet(
            self.dir,
            "figures",
            FIGURE_COLUMNS,
            [(f"{DOC_ID}#p0018#f1", DOC_ID, 18, 1, "t", "bar", "", "", "", "", "", None)],
        )
        self._run()
        self.assertIsNone(DocumentFigure.objects.get().verified)

    def test_a_failure_mid_corpus_leaves_no_partial_ingest(self):
        # The whole write phase is one transaction; a document that blows up must
        # not leave the earlier ones behind, or the tab appears on some reports
        # and not others with nothing to say which.
        second_ident = "second_report"
        second_iri = f"https://jawafdehi.org/material/{SOURCE}/{second_ident}"
        Material.objects.create(
            iri=second_iri,
            material_type="document",
            source=SOURCE,
            ident=second_ident,
            data={
                "@context": MATERIAL_CONTEXT,
                "@type": "DigitalDocument",
                "@id": second_iri,
                "name": {"ne": "दोस्रो"},
            },
        )
        self._write_dataset(extra_doc=True)
        self.map_path.write_text(
            json.dumps(
                {
                    "dataset": "d",
                    "source": SOURCE,
                    "documents": {DOC_ID: IDENT, "ciaa-2075-76": second_ident},
                }
            )
        )
        from materials.management.commands import ingest_document_content as mod

        original = mod.Command._ingest_one
        calls = {"n": 0}

        def exploding(self, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("boom on the second document")
            return original(self, **kwargs)

        mod.Command._ingest_one = exploding
        self.addCleanup(setattr, mod.Command, "_ingest_one", original)

        with self.assertRaises(RuntimeError):
            self._run()
        self.assertEqual(DocumentContent.objects.count(), 0)
        self.assertEqual(DocumentTable.objects.count(), 0)

    # ── idempotency ───────────────────────────────────────────────────────────

    def test_reingest_replaces_rather_than_duplicates(self):
        self._run()
        first = DocumentContent.objects.get(pk=self.iri).ingested_at
        self._run()
        self.assertEqual(DocumentContent.objects.count(), 1)
        self.assertEqual(DocumentTable.objects.count(), 2)
        self.assertEqual(DocumentFigurePoint.objects.count(), 2)
        self.assertGreaterEqual(
            DocumentContent.objects.get(pk=self.iri).ingested_at, first
        )

    def test_reingest_of_unchanged_input_is_byte_stable(self):
        self._run()
        before = list(
            DocumentTable.objects.order_by("ordinal").values_list("key", "uid")
        )
        self._run()
        after = list(
            DocumentTable.objects.order_by("ordinal").values_list("key", "uid")
        )
        self.assertEqual(before, after)

    def test_a_table_recovered_mid_document_shifts_ordinals_but_not_keys(self):
        # The bug this guards: ordinals are positional, so an upstream revision
        # that recovers a table missed in the middle of a report renumbers every
        # later one. If the public URL keyed on ordinal, a reader's saved link
        # would quietly start resolving to a DIFFERENT table — a 200 with the
        # wrong content, which is worse than a 404.
        self._run()
        original = dict(DocumentTable.objects.values_list("key", "uid"))
        key_of_page_19 = DocumentTable.objects.get(page_no=19).key
        ordinal_of_page_19 = DocumentTable.objects.get(page_no=19).ordinal

        rows = [
            (f"{DOC_ID}#p0018#t1", DOC_ID, 18, 1, "", "[]", 9, 4, "tight", "| a |"),
            # newly recovered, sits BETWEEN the two existing tables
            (f"{DOC_ID}#p0018#t2", DOC_ID, 18, 2, "", "[]", 3, 2, "tight", "| new |"),
            (f"{DOC_ID}#p0019#t1", DOC_ID, 19, 1, "", "[]", 8, 5, "tight", "| c |"),
        ]
        _write_parquet(self.dir, "tables", TABLE_COLUMNS, rows)
        self._run()

        moved = DocumentTable.objects.get(page_no=19)
        self.assertNotEqual(moved.ordinal, ordinal_of_page_19)  # ordinal shifted
        self.assertEqual(moved.key, key_of_page_19)  # key did not
        # Every pre-existing key still points at the table it always did.
        for key, uid in original.items():
            self.assertEqual(DocumentTable.objects.get(key=key).uid, uid)

    def test_keys_encode_page_and_index(self):
        self._run()
        self.assertEqual(
            sorted(DocumentTable.objects.values_list("key", flat=True)),
            ["p0018-t1", "p0019-t1"],
        )
        self.assertEqual(DocumentFigure.objects.get().key, "p0018-f1")

    def test_reingest_drops_rows_the_upstream_removed(self):
        self._run()
        _write_parquet(
            self.dir,
            "tables",
            TABLE_COLUMNS,
            [(f"{DOC_ID}#p0018#t1", DOC_ID, 18, 1, "", "[]", 9, 4, "tight", "| x |")],
        )
        self._run()
        self.assertEqual(DocumentTable.objects.count(), 1)

    # ── refuse-to-write cases ────────────────────────────────────────────────

    def test_missing_material_aborts_before_writing_anything(self):
        Material.objects.filter(pk=self.iri).delete()
        with self.assertRaises(CommandError) as ctx:
            self._run()
        self.assertIn("no material at", str(ctx.exception))
        self.assertEqual(DocumentContent.objects.count(), 0)

    def test_soft_deleted_material_is_skipped_not_fatal(self):
        # A takedown is a deliberate editorial act. It must not block the rest of
        # the corpus from receiving upstream corrections.
        Material.objects.filter(pk=self.iri).update(is_deleted=True)
        output = self._run()
        self.assertIn("soft-deleted", output)
        self.assertEqual(DocumentContent.objects.count(), 0)

    def test_a_takedown_does_not_stop_the_other_reports(self):
        second_ident = "second_report"
        second_iri = f"https://jawafdehi.org/material/{SOURCE}/{second_ident}"
        Material.objects.create(
            iri=second_iri,
            material_type="document",
            source=SOURCE,
            ident=second_ident,
            data={
                "@context": MATERIAL_CONTEXT,
                "@type": "DigitalDocument",
                "@id": second_iri,
                "name": {"ne": "दोस्रो"},
            },
        )
        self._write_dataset(extra_doc=True)
        self.map_path.write_text(
            json.dumps(
                {
                    "dataset": "d",
                    "source": SOURCE,
                    "documents": {DOC_ID: IDENT, "ciaa-2075-76": second_ident},
                }
            )
        )
        Material.objects.filter(pk=self.iri).update(is_deleted=True)
        self._run()
        self.assertEqual(
            list(DocumentContent.objects.values_list("pk", flat=True)), [second_iri]
        )

    def test_a_map_entry_with_no_material_at_all_still_aborts(self):
        # Absent is a broken map, not an editorial decision — fail loudly.
        Material.objects.filter(pk=self.iri).delete()
        with self.assertRaises(CommandError) as ctx:
            self._run()
        self.assertIn("no material at", str(ctx.exception))

    def test_dataset_document_absent_from_the_map_aborts(self):
        # Silence here is the dangerous outcome: a new report would land in the
        # dataset and quietly never appear on the site.
        self._write_dataset(extra_doc=True)
        with self.assertRaises(CommandError) as ctx:
            self._run()
        self.assertIn("absent from the map", str(ctx.exception))
        self.assertEqual(DocumentContent.objects.count(), 0)

    def test_map_pointing_two_doc_ids_at_one_material_aborts(self):
        self.map_path.write_text(
            json.dumps(
                {
                    "dataset": "d",
                    "source": SOURCE,
                    "documents": {DOC_ID: IDENT, "ciaa-2075-76": IDENT},
                }
            )
        )
        with self.assertRaises(CommandError) as ctx:
            self._run()
        self.assertIn("same material ident", str(ctx.exception))

    def test_missing_parquet_file_aborts(self):
        (self.dir / "figures.parquet").unlink()
        with self.assertRaises(CommandError) as ctx:
            self._run()
        self.assertIn("does not hold a parquet file", str(ctx.exception))

    def test_dry_run_writes_nothing(self):
        output = self._run(dry_run=True)
        self.assertIn("dry run OK", output)
        self.assertEqual(DocumentContent.objects.count(), 0)

    def test_dry_run_still_reports_an_unresolvable_map(self):
        Material.objects.filter(pk=self.iri).delete()
        with self.assertRaises(CommandError):
            self._run(dry_run=True)


class ShippedDocMapTests(TestCase):
    """The checked-in CIAA map is a reviewed artefact; keep it honest."""

    def test_map_is_well_formed_and_one_to_one(self):
        path = (
            Path(__file__).resolve().parents[1]
            / "data"
            / "ciaa_annual_report_docmap.json"
        )
        docmap = json.loads(path.read_text())
        self.assertEqual(docmap["source"], SOURCE)
        documents = docmap["documents"]
        self.assertEqual(len(documents), 35, "the corpus is 35 annual reports")
        self.assertEqual(
            len(set(documents.values())), 35, "two doc_ids share a material ident"
        )
        # The six executive summaries must stay excluded: they share a fiscal
        # year with a mapped report but are a different, shorter PDF.
        overlap = set(documents.values()) & set(docmap["excluded"])
        self.assertEqual(overlap, set())

    def test_the_counterintuitive_entries_keep_their_explanation(self):
        # `"ciaa-2048-49": "40._2nd_annual_report_2050_051_…"` reads like a bug —
        # the doc_id and the ident disagree about the year. It is correct, and the
        # note beside it is the only thing standing between a future reader and a
        # well-meaning "fix" that silently mis-attaches a report's tables.
        path = (
            Path(__file__).resolve().parents[1]
            / "data"
            / "ciaa_annual_report_docmap.json"
        )
        docmap = json.loads(path.read_text())
        notes = docmap.get("notes", {})
        for doc_id in ("ciaa-2048-49", "ciaa-2070-71"):
            self.assertIn(doc_id, docmap["documents"])
            self.assertTrue(
                notes.get(doc_id, "").strip(),
                f"{doc_id} maps to an ident whose title disagrees with it; "
                "it must carry a note saying why",
            )


class IngestFromDatasetTests(TestCase):
    """``--download``: the command fetches its own parquet.

    This is what makes running the ingest in the cluster a plain `kubectl exec`
    of one command rather than a Job with a ConfigMap'd staging script.
    """

    databases = "__all__"

    def setUp(self):
        import shutil
        import tempfile

        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.iri = f"https://jawafdehi.org/material/{SOURCE}/{IDENT}"
        Material.objects.create(
            iri=self.iri,
            material_type="document",
            source=SOURCE,
            ident=IDENT,
            data={
                "@context": MATERIAL_CONTEXT,
                "@type": "DigitalDocument",
                "@id": self.iri,
                "name": {"ne": "२८औं"},
            },
        )
        # Build the parquet once, then serve its bytes from the fake download.
        _write_parquet(
            self.dir,
            "documents",
            DOC_COLUMNS,
            [(DOC_ID, 255, "likhit", "clean", "", 1, 0, 0, 1)],
        )
        _write_parquet(
            self.dir,
            "pages",
            PAGE_COLUMNS,
            [(f"{DOC_ID}#p0018", DOC_ID, 18, "पृष्ठ", "anchored", "tight", "", False, 0, 5)],
        )
        _write_parquet(
            self.dir,
            "tables",
            TABLE_COLUMNS,
            [(f"{DOC_ID}#p0018#t1", DOC_ID, 18, 1, "", "[]", 9, 4, "tight", "| x |")],
        )
        _write_parquet(self.dir, "figures", FIGURE_COLUMNS, [])
        _write_parquet(self.dir, "figure_data", POINT_COLUMNS, [])
        for name, cols in (
            ("documents", DOC_COLUMNS),
            ("pages", PAGE_COLUMNS),
            ("tables", TABLE_COLUMNS),
            ("figures", FIGURE_COLUMNS),
            ("figure_data", POINT_COLUMNS),
        ):
            _write_parquet(self.dir, f"{name}-empty", cols, [])
        self.map_path = self.dir / "map.json"
        self.map_path.write_text(
            json.dumps(
                {
                    "dataset": "damo-da/ciaa-annual-reports",
                    "source": SOURCE,
                    "documents": {DOC_ID: IDENT},
                }
            )
        )
        self.requested = []

    def _fake_urlopen(self, sha="abc123sha", fail_on=None, shards=1):
        """Stand in for the three Hugging Face endpoints the command touches:
        the parquet branch's revision, the shard listing, and the files."""
        import contextlib
        import io

        outer = self

        def listing():
            return {
                name: {
                    "train": [
                        f"https://hf.test/{name}/{i}.parquet" for i in range(shards)
                    ]
                }
                for name in mod.REQUIRED_TABLES
            }

        @contextlib.contextmanager
        def opener(url, timeout=None):
            outer.requested.append(url)
            if fail_on and fail_on in url:
                raise OSError("network unreachable")
            if "/revision/" in url:
                yield io.BytesIO(json.dumps({"sha": sha}).encode())
            elif url.endswith("/parquet"):
                yield io.BytesIO(json.dumps(listing()).encode())
            else:
                name, shard = url.split("/")[-2], url.split("/")[-1]
                # Real shards hold DIFFERENT rows; only shard 0 carries data here
                # so a multi-shard read does not re-insert the same uid.
                suffix = "" if shard == "0.parquet" else "-empty"
                yield io.BytesIO((outer.dir / f"{name}{suffix}.parquet").read_bytes())

        return opener

    def _run(self, **kwargs):
        out = StringIO()
        with patch.object(mod.urllib.request, "urlopen", self._fake_urlopen(**kwargs.pop("fake", {}))):
            call_command(
                "ingest_document_content",
                map=str(self.map_path),
                download=True,
                stdout=out,
                **kwargs,
            )
        return out.getvalue()

    def test_downloads_and_ingests(self):
        self._run()
        self.assertEqual(DocumentContent.objects.count(), 1)
        self.assertEqual(DocumentTable.objects.count(), 1)

    def test_records_the_datasets_own_revision(self):
        # The whole point of --download: provenance without the operator having to
        # look a sha up and paste it correctly.
        self._run()
        self.assertEqual(DocumentContent.objects.get().dataset_revision, "abc123sha")

    def test_a_missing_revision_sha_aborts_rather_than_recording_a_blank(self):
        # --download exists so provenance comes from the bytes actually fetched.
        # Proceeding with an empty revision would quietly replace a previously
        # recorded good sha with nothing, and "ingested from an unknown revision"
        # is indistinguishable afterwards from "ingested before we recorded them".
        with self.assertRaises(CommandError) as ctx:
            self._run(fake={"sha": ""})
        self.assertIn("no revision sha", str(ctx.exception))
        self.assertEqual(DocumentContent.objects.count(), 0)

    def test_a_republish_during_the_download_aborts(self):
        # The shard listing and file URLs are not pinned to a revision, so a
        # dataset republished mid-run leaves bytes from one commit stamped with
        # the sha of another. Re-running is cheap; a wrong revision on 35
        # documents is not detectable after the fact.
        shas = iter(["sha-before", "sha-after"])

        with patch.object(mod, "_get_json", side_effect=self._moving_sha(shas)):
            with self.assertRaises(CommandError) as ctx:
                self._run()
        self.assertIn("republished during the download", str(ctx.exception))
        self.assertEqual(DocumentContent.objects.count(), 0)

    def _moving_sha(self, shas):
        """``_get_json`` that reports a different sha on each revision call."""
        real = mod._get_json

        def fake(url):
            if "/revision/" in url:
                return {"sha": next(shas)}
            return real(url)

        return fake

    def test_a_sharded_config_is_loaded_whole(self):
        # Hugging Face splits a large config across several parquet files. A
        # hardcoded 0.parquet would load a prefix and silently drop the rest.
        self._run(fake={"shards": 3})
        files = [u for u in self.requested if u.endswith(".parquet")]
        self.assertEqual(len(files), 15, f"expected 3 shards x 5 configs: {files}")

    def test_fetches_only_the_five_configs_it_reads(self):
        # `table_cells` is a second representation of content the markdown
        # already carries, and `quality` repeats per-document verdicts that
        # `documents` holds. Neither is downloaded.
        self._run()
        fetched = sorted({u.split("/")[-2] for u in self.requested if u.endswith(".parquet")})
        self.assertEqual(
            fetched, ["documents", "figure_data", "figures", "pages", "tables"]
        )

    def test_a_download_failure_is_a_command_error_not_a_traceback(self):
        with self.assertRaises(CommandError) as ctx:
            self._run(fake={"fail_on": "hf.test/tables/"})
        self.assertIn("could not download dataset", str(ctx.exception))
        self.assertEqual(DocumentContent.objects.count(), 0)

    def test_the_downloaded_parquet_is_not_left_behind(self):
        # A long-lived pod must not accumulate a few MB per run.
        import tempfile

        before = set(Path(tempfile.gettempdir()).glob("content-*"))
        self._run()
        self.assertEqual(set(Path(tempfile.gettempdir()).glob("content-*")), before)

    def test_temp_dir_is_cleaned_up_even_when_the_ingest_fails(self):
        import tempfile

        Material.objects.filter(pk=self.iri).delete()
        before = set(Path(tempfile.gettempdir()).glob("content-*"))
        with self.assertRaises(CommandError):
            self._run()
        self.assertEqual(set(Path(tempfile.gettempdir()).glob("content-*")), before)

    def test_downloads_the_dataset_the_map_names(self):
        # The map's id is what lands in every row's `dataset` provenance field.
        # If the bytes could come from somewhere the command line named instead,
        # the two could disagree and the recorded provenance would be a lie —
        # which is the one thing that field must not be able to do.
        self._run()
        # The file URLs come from HF's own shard listing; what must be pinned to
        # the map is which DATASET we asked. Three api.huggingface calls: the
        # revision, the shard listing, and the revision again afterwards to prove
        # the branch did not move under the download.
        api_calls = [u for u in self.requested if u.startswith(mod.HF_API)]
        self.assertEqual(
            len(api_calls), 3, f"expected revision + listing + recheck: {api_calls}"
        )
        self.assertTrue(
            all("damo-da/ciaa-annual-reports" in u for u in api_calls),
            f"asked a dataset the map does not name: {api_calls}",
        )
        self.assertEqual(
            DocumentContent.objects.get().dataset, "damo-da/ciaa-annual-reports"
        )

    def test_revision_comes_from_the_parquet_branch_not_the_default_branch(self):
        # Hugging Face auto-converts a dataset to parquet on a SEPARATE git ref
        # with its own sha. For this corpus main is eafef6d7 and
        # refs/convert/parquet is c3c6c673, a minute apart. Reading main's sha
        # would stamp every row with a revision whose bytes were never loaded —
        # the exact failure the provenance field exists to prevent.
        self._run()
        revision_calls = [u for u in self.requested if "/revision/" in u]
        self.assertTrue(revision_calls, f"never asked a ref for its sha: {self.requested}")
        self.assertIn("refs%2Fconvert%2Fparquet", revision_calls[0])
        self.assertNotIn(
            f"{mod.HF_API}/damo-da/ciaa-annual-reports\n",
            "\n".join(self.requested) + "\n",
        )

    def test_dry_run_downloads_but_writes_nothing(self):
        output = self._run(dry_run=True)
        self.assertIn("dry run OK", output)
        self.assertEqual(DocumentContent.objects.count(), 0)


class IngestSourceArgumentTests(TestCase):
    databases = "__all__"

    def test_neither_source_is_rejected(self):
        with self.assertRaises(CommandError):
            call_command("ingest_document_content", map="/nope.json")

    def test_both_sources_at_once_are_rejected(self):
        # Ambiguous: which one wins should not be a question anyone has to answer
        # by reading the implementation.
        with self.assertRaises(CommandError):
            call_command(
                "ingest_document_content",
                map="/nope.json",
                download=True,
                parquet_dir="/tmp",
            )


class IngestRevisionArgumentTests(TestCase):
    databases = "__all__"

    def test_revision_cannot_be_combined_with_download(self):
        # A hand-pasted sha silently winning over the one read from the bytes
        # actually downloaded is the same provenance-can-lie failure --download
        # exists to close.
        with self.assertRaises(CommandError) as ctx:
            call_command(
                "ingest_document_content",
                map="/nope.json",
                download=True,
                revision="deadbeef",
            )
        self.assertIn("cannot be combined with --download", str(ctx.exception))
