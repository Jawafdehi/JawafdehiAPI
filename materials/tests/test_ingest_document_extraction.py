"""The ``ingest_document_extraction`` management command.

The command's contract is "resolve everything, then write, or write nothing":
a half-ingested corpus would put the data tab on some reports and not others
with no way to tell which from outside. These tests pin that, plus idempotency
(re-ingest must not duplicate or renumber) and the shrug-off handling of the
upstream's ragged fields.

Fixtures are real parquet files written by duckdb into a tmp dir, so the test
exercises the same read path production does.

    DATABASE_URL=sqlite:// NES_DB_URL=sqlite:// NGM_DATABASE_URL=sqlite:// \
        uv run pytest materials/tests/test_ingest_document_extraction.py
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

from materials.management.commands import ingest_document_extraction as mod
from materials.jsonld import MATERIAL_CONTEXT
from materials.models import (
    DocumentExtraction,
    ExtractedFigure,
    ExtractedFigurePoint,
    ExtractedTable,
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
        docs = [(DOC_ID, 255, "likhit", "clean", "trustworthy")]
        if extra_doc:
            docs.append(("ciaa-2075-76", 328, "likhit", "clean", ""))
        _write_parquet(self.dir, "documents", DOC_COLUMNS, docs)
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
            "ingest_document_extraction",
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
        extraction = DocumentExtraction.objects.get(pk=self.iri)
        self.assertEqual(extraction.doc_id, DOC_ID)
        self.assertEqual(extraction.dataset_revision, "abc1234")
        self.assertEqual(extraction.page_count, 255)
        self.assertEqual(extraction.tables.count(), 2)
        self.assertEqual(extraction.figures.count(), 1)
        self.assertEqual(ExtractedFigurePoint.objects.count(), 2)

    def test_ordinals_follow_document_order_not_file_order(self):
        self._run()
        ordered = list(
            ExtractedTable.objects.order_by("ordinal").values_list("ordinal", "page_no")
        )
        self.assertEqual(ordered, [(1, 18), (2, 19)])

    def test_points_keep_their_estimated_flag_and_order(self):
        self._run()
        points = list(
            ExtractedFigurePoint.objects.order_by("point_index").values_list(
                "point_index", "value", "is_estimated"
            )
        )
        self.assertEqual(points, [(1, 19580.0, False), (2, 19488.0, True)])

    def test_malformed_header_json_degrades_to_empty_not_an_error(self):
        self._run()
        by_page = {t.page_no: t for t in ExtractedTable.objects.all()}
        self.assertEqual(by_page[19].header, ["क", "ख"])
        self.assertEqual(by_page[18].header, [])

    def test_null_caption_becomes_empty_string(self):
        self._run()
        self.assertEqual(ExtractedTable.objects.get(page_no=18).caption, "")

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
        point = ExtractedFigurePoint.objects.get()
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
        self.assertFalse(ExtractedFigurePoint.objects.get().is_estimated)

    def test_null_verified_flag_stays_null(self):
        # "not checked" and "checked and found wanting" are different claims.
        _write_parquet(
            self.dir,
            "figures",
            FIGURE_COLUMNS,
            [(f"{DOC_ID}#p0018#f1", DOC_ID, 18, 1, "t", "bar", "", "", "", "", "", None)],
        )
        self._run()
        self.assertIsNone(ExtractedFigure.objects.get().verified)

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
        from materials.management.commands import ingest_document_extraction as mod

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
        self.assertEqual(DocumentExtraction.objects.count(), 0)
        self.assertEqual(ExtractedTable.objects.count(), 0)

    # ── idempotency ───────────────────────────────────────────────────────────

    def test_reingest_replaces_rather_than_duplicates(self):
        self._run()
        first = DocumentExtraction.objects.get(pk=self.iri).ingested_at
        self._run()
        self.assertEqual(DocumentExtraction.objects.count(), 1)
        self.assertEqual(ExtractedTable.objects.count(), 2)
        self.assertEqual(ExtractedFigurePoint.objects.count(), 2)
        self.assertGreaterEqual(
            DocumentExtraction.objects.get(pk=self.iri).ingested_at, first
        )

    def test_reingest_of_unchanged_input_is_byte_stable(self):
        self._run()
        before = list(
            ExtractedTable.objects.order_by("ordinal").values_list("key", "uid")
        )
        self._run()
        after = list(
            ExtractedTable.objects.order_by("ordinal").values_list("key", "uid")
        )
        self.assertEqual(before, after)

    def test_a_table_recovered_mid_document_shifts_ordinals_but_not_keys(self):
        # The bug this guards: ordinals are positional, so an upstream revision
        # that recovers a table missed in the middle of a report renumbers every
        # later one. If the public URL keyed on ordinal, a reader's saved link
        # would quietly start resolving to a DIFFERENT table — a 200 with the
        # wrong content, which is worse than a 404.
        self._run()
        original = dict(ExtractedTable.objects.values_list("key", "uid"))
        key_of_page_19 = ExtractedTable.objects.get(page_no=19).key
        ordinal_of_page_19 = ExtractedTable.objects.get(page_no=19).ordinal

        rows = [
            (f"{DOC_ID}#p0018#t1", DOC_ID, 18, 1, "", "[]", 9, 4, "tight", "| a |"),
            # newly recovered, sits BETWEEN the two existing tables
            (f"{DOC_ID}#p0018#t2", DOC_ID, 18, 2, "", "[]", 3, 2, "tight", "| new |"),
            (f"{DOC_ID}#p0019#t1", DOC_ID, 19, 1, "", "[]", 8, 5, "tight", "| c |"),
        ]
        _write_parquet(self.dir, "tables", TABLE_COLUMNS, rows)
        self._run()

        moved = ExtractedTable.objects.get(page_no=19)
        self.assertNotEqual(moved.ordinal, ordinal_of_page_19)  # ordinal shifted
        self.assertEqual(moved.key, key_of_page_19)  # key did not
        # Every pre-existing key still points at the table it always did.
        for key, uid in original.items():
            self.assertEqual(ExtractedTable.objects.get(key=key).uid, uid)

    def test_keys_encode_page_and_index(self):
        self._run()
        self.assertEqual(
            sorted(ExtractedTable.objects.values_list("key", flat=True)),
            ["p0018-t1", "p0019-t1"],
        )
        self.assertEqual(ExtractedFigure.objects.get().key, "p0018-f1")

    def test_reingest_drops_rows_the_upstream_removed(self):
        self._run()
        _write_parquet(
            self.dir,
            "tables",
            TABLE_COLUMNS,
            [(f"{DOC_ID}#p0018#t1", DOC_ID, 18, 1, "", "[]", 9, 4, "tight", "| x |")],
        )
        self._run()
        self.assertEqual(ExtractedTable.objects.count(), 1)

    # ── refuse-to-write cases ────────────────────────────────────────────────

    def test_missing_material_aborts_before_writing_anything(self):
        Material.objects.filter(pk=self.iri).delete()
        with self.assertRaises(CommandError) as ctx:
            self._run()
        self.assertIn("no material at", str(ctx.exception))
        self.assertEqual(DocumentExtraction.objects.count(), 0)

    def test_soft_deleted_material_is_skipped_not_fatal(self):
        # A takedown is a deliberate editorial act. It must not block the rest of
        # the corpus from receiving upstream corrections.
        Material.objects.filter(pk=self.iri).update(is_deleted=True)
        output = self._run()
        self.assertIn("soft-deleted", output)
        self.assertEqual(DocumentExtraction.objects.count(), 0)

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
            list(DocumentExtraction.objects.values_list("pk", flat=True)), [second_iri]
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
        self.assertEqual(DocumentExtraction.objects.count(), 0)

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
        self.assertEqual(DocumentExtraction.objects.count(), 0)

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
            self.dir, "documents", DOC_COLUMNS, [(DOC_ID, 255, "likhit", "clean", "")]
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
                "ingest_document_extraction",
                map=str(self.map_path),
                download=True,
                stdout=out,
                **kwargs,
            )
        return out.getvalue()

    def test_downloads_and_ingests(self):
        self._run()
        self.assertEqual(DocumentExtraction.objects.count(), 1)
        self.assertEqual(ExtractedTable.objects.count(), 1)

    def test_records_the_datasets_own_revision(self):
        # The whole point of --download: provenance without the operator having to
        # look a sha up and paste it correctly.
        self._run()
        self.assertEqual(DocumentExtraction.objects.get().dataset_revision, "abc123sha")

    def test_a_sharded_config_is_loaded_whole(self):
        # Hugging Face splits a large config across several parquet files. A
        # hardcoded 0.parquet would load a prefix and silently drop the rest.
        self._run(fake={"shards": 3})
        files = [u for u in self.requested if u.endswith(".parquet")]
        self.assertEqual(len(files), 12, f"expected 3 shards x 4 configs: {files}")

    def test_fetches_only_the_four_configs_it_reads(self):
        # `pages` and `table_cells` are an order of magnitude larger and unused.
        self._run()
        fetched = sorted({u.split("/")[-2] for u in self.requested if u.endswith(".parquet")})
        self.assertEqual(fetched, ["documents", "figure_data", "figures", "tables"])

    def test_a_download_failure_is_a_command_error_not_a_traceback(self):
        with self.assertRaises(CommandError) as ctx:
            self._run(fake={"fail_on": "hf.test/tables/"})
        self.assertIn("could not download dataset", str(ctx.exception))
        self.assertEqual(DocumentExtraction.objects.count(), 0)

    def test_the_downloaded_parquet_is_not_left_behind(self):
        # A long-lived pod must not accumulate a few MB per run.
        import tempfile

        before = set(Path(tempfile.gettempdir()).glob("extraction-*"))
        self._run()
        self.assertEqual(set(Path(tempfile.gettempdir()).glob("extraction-*")), before)

    def test_temp_dir_is_cleaned_up_even_when_the_ingest_fails(self):
        import tempfile

        Material.objects.filter(pk=self.iri).delete()
        before = set(Path(tempfile.gettempdir()).glob("extraction-*"))
        with self.assertRaises(CommandError):
            self._run()
        self.assertEqual(set(Path(tempfile.gettempdir()).glob("extraction-*")), before)

    def test_downloads_the_dataset_the_map_names(self):
        # The map's id is what lands in every row's `dataset` provenance field.
        # If the bytes could come from somewhere the command line named instead,
        # the two could disagree and the recorded provenance would be a lie —
        # which is the one thing that field must not be able to do.
        self._run()
        # The file URLs come from HF's own shard listing; what must be pinned to
        # the map is which DATASET we asked, i.e. the two api.huggingface calls.
        api_calls = [u for u in self.requested if u.startswith(mod.HF_API)]
        self.assertEqual(len(api_calls), 2, f"expected revision + listing: {api_calls}")
        self.assertTrue(
            all("damo-da/ciaa-annual-reports" in u for u in api_calls),
            f"asked a dataset the map does not name: {api_calls}",
        )
        self.assertEqual(
            DocumentExtraction.objects.get().dataset, "damo-da/ciaa-annual-reports"
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
        self.assertEqual(DocumentExtraction.objects.count(), 0)


class IngestSourceArgumentTests(TestCase):
    databases = "__all__"

    def test_neither_source_is_rejected(self):
        with self.assertRaises(CommandError):
            call_command("ingest_document_extraction", map="/nope.json")

    def test_both_sources_at_once_are_rejected(self):
        # Ambiguous: which one wins should not be a question anyone has to answer
        # by reading the implementation.
        with self.assertRaises(CommandError):
            call_command(
                "ingest_document_extraction",
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
                "ingest_document_extraction",
                map="/nope.json",
                download=True,
                revision="deadbeef",
            )
        self.assertIn("cannot be combined with --download", str(ctx.exception))
