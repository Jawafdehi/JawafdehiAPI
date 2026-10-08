"""Load extracted tables and charts for a corpus of already-ingested materials.

Two ways in. In production, let the command fetch the dataset itself — one
command, nothing to stage, and the revision is captured automatically:

    uv run python manage.py ingest_document_extraction \
        --map materials/data/ciaa_annual_report_docmap.json \
        --dataset damo-da/ciaa-annual-reports

Or point it at parquet already on disk, for offline work and the tests:

    uv run python manage.py ingest_document_extraction \
        --map materials/data/ciaa_annual_report_docmap.json \
        --parquet-dir /path/to/dataset \
        --revision <upstream commit sha>

``--dataset`` exists so that running this in the cluster is a plain
``kubectl exec`` of ONE command. The alternative — staging ~3 MB of parquet into
a pod first — needs a Job, a ConfigMap'd shell script and a manifest to review,
which is a lot of apparatus for an operation that runs about once a year when the
CIAA publishes. The pod already has egress to Hugging Face; this just uses it.

The command is the ONLY CIAA-aware piece of this feature; the schema it writes
into is generic (see materials.models.DocumentExtraction). To onboard a second
corpus, write a second map file — no migration, no code change here.

**Why a map file rather than a parser.** The upstream dataset keys documents on
fiscal year (``ciaa-2074-75``); our materials are keyed on a hash of the original
PDF filename. Deriving one from the other looks easy and is not: one archived
title carries the wrong fiscal year, one carries none at all, and six records are
executive *summaries* that share a year with the full report they summarise.
A parser would mis-attach silently. The map is written by hand, reviewed, and
checked here for being total and 1:1 before a single row is written.

Reading parquet uses duckdb, which is already a dependency (see lakehouse/).
"""

from __future__ import annotations

import json
import math
import tempfile
import urllib.request
from pathlib import Path
from typing import Any

import duckdb
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from jawafdehi_shared.entities.ids import build_material_iri

from ...models import (
    DocumentExtraction,
    ExtractedFigure,
    ExtractedFigurePoint,
    ExtractedTable,
    Material,
)

#: Parquet tables the command needs, by basename. ``documents`` carries one row
#: per document; the rest are keyed to it by ``doc_id`` (tables/figures) or by
#: ``figure_uid`` (figure_data).
REQUIRED_TABLES = ("documents", "tables", "figures", "figure_data")

#: Chunk size for the point bulk_create. Points are the only table that can run
#: to tens of thousands of rows per corpus, and sqlite (the test gate) caps the
#: number of bound parameters per statement.
POINT_BATCH = 500

#: Hugging Face dataset API. The parquet endpoint serves the auto-converted copy
#: of the dataset's current default branch and takes no revision parameter, so
#: the revision is read separately from the dataset's own metadata.
HF_API = "https://huggingface.co/api/datasets"

#: Per-file download timeout. The largest table in the CIAA corpus is ~2.5 MB.
DOWNLOAD_TIMEOUT = 300


def _fetch_dataset(dataset: str, dest: Path, log) -> str:
    """Download the parquet this command reads into ``dest``; return the revision.

    Only the four configs ``REQUIRED_TABLES`` names. A dataset of this shape also
    publishes ``pages`` and ``table_cells``, which are an order of magnitude
    larger and unused — the transcript is not this command's to load, and a
    table's content rides in its ``markdown``.
    """
    with urllib.request.urlopen(f"{HF_API}/{dataset}", timeout=60) as response:
        revision = json.load(response).get("sha") or ""
    log(f"  dataset {dataset} @ {revision or 'unknown revision'}")

    for name in REQUIRED_TABLES:
        url = f"{HF_API}/{dataset}/parquet/{name}/train/0.parquet"
        with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT) as response:
            body = response.read()
        (dest / f"{name}.parquet").write_bytes(body)
        log(f"  {name}.parquet  {len(body) / 1024:.0f} KiB")
    return revision


def _rows(con: duckdb.DuckDBPyConnection, path: Path, order_by: str) -> list[dict]:
    """Read a parquet file into a list of dicts, deterministically ordered.

    The order matters: ``ordinal`` is assigned from it and becomes the public URL
    key, so an unordered read would renumber every table on re-ingest.
    """
    cur = con.execute(
        f"SELECT * FROM read_parquet(?) ORDER BY {order_by}", [str(path)]
    )
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _text(value: Any) -> str:
    """Upstream blanks arrive as None, NaN or ''. Normalise to a plain string."""
    if value is None:
        return ""
    text = str(value)
    return "" if text.lower() == "nan" else text


def _int(value: Any, default: int = 0) -> int:
    """Coerce to a non-negative int. The columns this feeds are all
    ``PositiveIntegerField``, so a negative would be a hard IntegrityError on
    Postgres (and silently stored on sqlite) rather than a visible data problem."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(parsed, 0)


def _float(value: Any) -> float | None:
    """Coerce a chart value, mapping absent AND non-finite to None.

    NaN matters: it survives ``float()``, stores happily in a FloatField, and is
    then rendered by the JSON encoder as a bare ``NaN`` token — which is not
    valid JSON and which a strict client parser rejects outright. A missing value
    is what the data actually means, so say that instead.
    """
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(parsed) or math.isinf(parsed) else parsed


def _bool(value: Any) -> bool:
    """Coerce a non-nullable upstream flag, treating absent as False.

    Not ``bool(value)``: a null in a numeric parquet column arrives as NaN and
    ``bool(nan)`` is True, and a flag that arrives as the *string* ``"False"`` is
    truthy too. Either would flip ``is_estimated`` the wrong way and present a
    printed figure as a value guessed off a chart image.
    """
    if value is None:
        return False
    if isinstance(value, float):
        return False if math.isnan(value) else bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"true", "t", "yes", "1"}
    return bool(value)


def _key(page_no: int, index_on_page: int, kind: str) -> str:
    """The stable, URL-safe handle for a table/figure: ``p0018-t1``.

    Built from where the thing physically sits in the document, so it survives
    the upstream gaining or losing items elsewhere — unlike ``ordinal``, which is
    recomputed each ingest. ``(page_no, index_on_page)`` is unique within a
    document (verified across the whole CIAA corpus), and the DB holds a unique
    constraint on it so a corpus that broke that assumption would fail loudly.
    """
    return f"p{page_no:04d}-{kind}{index_on_page}"


def _tristate(value: Any) -> bool | None:
    """``verified_clean`` is a nullable flag. None stays None; everything else is
    truth-tested — but NaN is filtered first, because ``bool(nan)`` is True and
    would turn "not checked" into "verified"."""
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    return bool(value)


def _header(value: Any) -> list:
    """``header_json`` is a JSON *string* in the source. Never fail ingest on it —
    a malformed header costs a column caption, not the table."""
    raw = _text(value)
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return []
    return parsed if isinstance(parsed, list) else []


class Command(BaseCommand):
    help = "Ingest extracted tables/figures for materials named in a map file."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--map", required=True, help="Path to the docmap JSON.")
        # Exactly one source. argparse enforces it so the command cannot be run
        # ambiguously — pointing at both a download and a directory would leave
        # which one won up to reading the implementation.
        source = parser.add_mutually_exclusive_group(required=True)
        source.add_argument(
            "--dataset",
            help=(
                "Hugging Face dataset id to download the parquet from, e.g. "
                "damo-da/ciaa-annual-reports. Records its revision automatically."
            ),
        )
        source.add_argument(
            "--parquet-dir",
            help="Directory holding documents/tables/figures/figure_data parquet.",
        )
        parser.add_argument(
            "--revision",
            default="",
            help=(
                "Upstream dataset revision recorded as provenance. Required only "
                "with --parquet-dir; --dataset reads it from the dataset itself."
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Resolve and validate everything, write nothing.",
        )

    def handle(self, *args, **options) -> None:
        if options["dataset"]:
            # The temp dir is torn down on the way out, including when the ingest
            # raises: a failed run must not leave a few MB of parquet behind in a
            # long-lived pod.
            with tempfile.TemporaryDirectory(prefix="extraction-") as tmp:
                self._run(options, Path(tmp))
        else:
            self._run(options, Path(options["parquet_dir"]))

    def _run(self, options: dict, parquet_dir: Path) -> None:
        docmap = self._load_map(Path(options["map"]))

        revision = options["revision"]
        if options["dataset"]:
            try:
                fetched = _fetch_dataset(
                    options["dataset"], parquet_dir, self.stdout.write
                )
            except OSError as exc:
                # Urllib's failures (DNS, TLS, timeout, HTTP error) are all OSError
                # subclasses. A network problem is not a traceback-worthy bug.
                raise CommandError(
                    f"could not download dataset {options['dataset']!r}: {exc}"
                ) from exc
            # An explicit --revision still wins: it lets an operator label a run
            # when the dataset's own metadata is unavailable or wrong.
            revision = revision or fetched
        options = {**options, "revision": revision}

        paths = {name: parquet_dir / f"{name}.parquet" for name in REQUIRED_TABLES}
        missing = [str(p) for p in paths.values() if not p.is_file()]
        if missing:
            raise CommandError("missing parquet file(s): " + ", ".join(missing))

        source = docmap["source"]
        dataset = docmap["dataset"]
        mapping: dict[str, str] = docmap["documents"]

        con = duckdb.connect(":memory:")
        documents = {r["doc_id"]: r for r in _rows(con, paths["documents"], "doc_id")}
        tables_by_doc: dict[str, list[dict]] = {}
        for row in _rows(con, paths["tables"], "doc_id, page_no, table_index_on_page"):
            tables_by_doc.setdefault(row["doc_id"], []).append(row)
        figures_by_doc: dict[str, list[dict]] = {}
        for row in _rows(con, paths["figures"], "doc_id, page_no, figure_index_on_page"):
            figures_by_doc.setdefault(row["doc_id"], []).append(row)
        points_by_figure: dict[str, list[dict]] = {}
        for row in _rows(con, paths["figure_data"], "figure_uid, point_index"):
            points_by_figure.setdefault(row["figure_uid"], []).append(row)

        # Resolve EVERYTHING before writing ANYTHING. A half-ingested corpus is
        # worse than a failed one: the tab would appear on some reports and not
        # others with no way to tell which from the outside.
        plan: list[tuple[str, str, dict]] = []
        problems: list[str] = []
        skipped: list[str] = []
        for doc_id, ident in sorted(mapping.items()):
            if doc_id not in documents:
                problems.append(f"{doc_id}: not present in documents.parquet")
                continue
            try:
                iri = build_material_iri(source, ident)
            except ValueError as exc:
                problems.append(f"{doc_id}: {ident!r} is not a valid ident ({exc})")
                continue
            row = (
                Material.objects.using("ngm")
                .filter(pk=iri)
                .values_list("is_deleted", flat=True)
                .first()
            )
            if row is None:
                problems.append(f"{doc_id}: no material at {iri}")
                continue
            if row:
                # Soft-deleted is a takedown — a deliberate editorial act, not a
                # broken map. Skip it and carry on: aborting here would mean one
                # withdrawn report permanently blocks upstream corrections to the
                # other thirty-four until somebody hand-edits a reviewed file.
                skipped.append(f"{doc_id}: material is soft-deleted, leaving it alone")
                continue
            plan.append((doc_id, iri, documents[doc_id]))

        unmapped = sorted(set(documents) - set(mapping))
        if unmapped:
            problems.append(
                f"{len(unmapped)} document(s) in the dataset are absent from the map: "
                + ", ".join(unmapped)
            )
        if problems:
            raise CommandError(
                "refusing to ingest; resolve these first:\n  - " + "\n  - ".join(problems)
            )
        for note in skipped:
            self.stdout.write(self.style.WARNING(f"  skipped {note}"))

        if options["dry_run"]:
            self.stdout.write(
                self.style.SUCCESS(f"dry run OK — {len(plan)} document(s) would be ingested")
            )
            return

        totals = {"docs": 0, "tables": 0, "figures": 0, "points": 0}
        # ONE transaction for the whole corpus, not one per document. A crash
        # partway through a per-document loop would leave the data tab present on
        # some reports and absent on others, with nothing in the response to say
        # which — the corpus is ~8k rows, so the long transaction is cheap
        # insurance against an inconsistency nobody would notice.
        with transaction.atomic(using="ngm"):
            for doc_id, iri, doc in plan:
                counts = self._ingest_one(
                    iri=iri,
                    doc_id=doc_id,
                    doc=doc,
                    dataset=dataset,
                    revision=options["revision"],
                    tables=tables_by_doc.get(doc_id, []),
                    figures=figures_by_doc.get(doc_id, []),
                    points_by_figure=points_by_figure,
                )
                totals["docs"] += 1
                for key in ("tables", "figures", "points"):
                    totals[key] += counts[key]
                self.stdout.write(
                    f"  {doc_id}: {counts['tables']} tables, "
                    f"{counts['figures']} figures, {counts['points']} points"
                )

        self.stdout.write(
            self.style.SUCCESS(
                f"ingested {totals['docs']} document(s): {totals['tables']} tables, "
                f"{totals['figures']} figures, {totals['points']} points"
            )
        )

    def _load_map(self, path: Path) -> dict:
        if not path.is_file():
            raise CommandError(f"map file not found: {path}")
        try:
            docmap = json.loads(path.read_text())
        except ValueError as exc:
            raise CommandError(f"map file is not valid JSON: {exc}") from exc
        for key in ("dataset", "source", "documents"):
            if not docmap.get(key):
                raise CommandError(f"map file is missing {key!r}")
        idents = list(docmap["documents"].values())
        if len(set(idents)) != len(idents):
            raise CommandError("map file points two doc_ids at the same material ident")
        return docmap

    def _ingest_one(
        self,
        *,
        iri: str,
        doc_id: str,
        doc: dict,
        dataset: str,
        revision: str,
        tables: list[dict],
        figures: list[dict],
        points_by_figure: dict[str, list[dict]],
    ) -> dict[str, int]:
        """Replace one material's extraction wholesale. Runs inside the caller's
        transaction.

        Delete-then-insert rather than update-in-place: the upstream can drop a
        table between revisions, and an upsert keyed on ``uid`` would leave the
        dropped row behind forever. The cascade from DocumentExtraction takes the
        tables, figures and points with it.
        """
        DocumentExtraction.objects.using("ngm").filter(pk=iri).delete()
        extraction = DocumentExtraction.objects.using("ngm").create(
            material_id=iri,
            doc_id=doc_id,
            dataset=dataset,
            dataset_revision=revision,
            page_count=_int(doc.get("n_pages")),
            text_source=_text(doc.get("text_source"))[:40],
            transcript_verdict=_text(doc.get("transcript_quality_verdict"))[:40],
            figures_verdict=_text(doc.get("figures_quality_verdict"))[:40],
        )

        ExtractedTable.objects.using("ngm").bulk_create(
            [
                ExtractedTable(
                    extraction=extraction,
                    ordinal=ordinal,
                    key=_key(
                        _int(row.get("page_no")),
                        _int(row.get("table_index_on_page"), 1),
                        "t",
                    ),
                    uid=_text(row.get("table_uid"))[:160],
                    page_no=_int(row.get("page_no")),
                    index_on_page=_int(row.get("table_index_on_page"), 1),
                    caption=_text(row.get("intro_text")),
                    header=_header(row.get("header_json")),
                    n_rows=_int(row.get("n_rows")),
                    n_cols=_int(row.get("n_cols")),
                    fidelity=_text(row.get("anchor_fidelity"))[:40],
                    markdown=_text(row.get("markdown")),
                )
                for ordinal, row in enumerate(tables, start=1)
            ]
        )

        figure_rows = [
            ExtractedFigure(
                extraction=extraction,
                ordinal=ordinal,
                key=_key(
                    _int(row.get("page_no")),
                    _int(row.get("figure_index_on_page"), 1),
                    "f",
                ),
                uid=_text(row.get("figure_uid"))[:160],
                page_no=_int(row.get("page_no")),
                index_on_page=_int(row.get("figure_index_on_page"), 1),
                title=_text(row.get("title_ne")),
                chart_type=_text(row.get("chart_type"))[:40],
                unit=_text(row.get("unit"))[:80],
                x_axis=_text(row.get("x_axis"))[:300],
                y_axis=_text(row.get("y_axis"))[:300],
                notes=_text(row.get("notes")),
                verify_note=_text(row.get("verify_note")),
                verified=_tristate(row.get("verified_clean")),
            )
            for ordinal, row in enumerate(figures, start=1)
        ]
        ExtractedFigure.objects.using("ngm").bulk_create(figure_rows)

        # bulk_create does not populate PKs on every backend, so re-read the ids
        # by uid rather than trusting the in-memory objects.
        figure_ids = dict(
            ExtractedFigure.objects.using("ngm")
            .filter(extraction=extraction)
            .values_list("uid", "id")
        )
        # Pair each stored row back to the source row it came from. Points are
        # keyed on the UNTRUNCATED upstream uid while ``figure_ids`` is keyed on
        # the stored (possibly truncated) one, so looking both up off the same
        # string would silently drop every point of an over-long uid.
        points = [
            ExtractedFigurePoint(
                figure_id=figure_ids[stored.uid],
                point_index=_int(p.get("point_index")),
                label=_text(p.get("label"))[:300],
                series=_text(p.get("series"))[:300],
                value=_float(p.get("value")),
                is_estimated=_bool(p.get("value_estimated")),
            )
            for stored, source_row in zip(figure_rows, figures)
            for p in points_by_figure.get(_text(source_row.get("figure_uid")), [])
        ]
        ExtractedFigurePoint.objects.using("ngm").bulk_create(
            points, batch_size=POINT_BATCH
        )

        return {"tables": len(tables), "figures": len(figure_rows), "points": len(points)}
