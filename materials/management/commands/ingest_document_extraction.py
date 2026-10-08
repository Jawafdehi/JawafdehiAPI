"""Load extracted tables and charts for a corpus of already-ingested materials.

Two ways in. In production, let the command fetch the dataset itself — one
command, nothing to stage, and the revision is captured automatically:

    uv run python manage.py ingest_document_extraction \
        --map materials/data/ciaa_annual_report_docmap.json \
        --download

Or point it at parquet already on disk, for offline work and the tests:

    uv run python manage.py ingest_document_extraction \
        --map materials/data/ciaa_annual_report_docmap.json \
        --parquet-dir /path/to/dataset \
        --revision <upstream commit sha>

``--download`` exists so that running this in the cluster is a plain
``kubectl exec`` of ONE command. The alternative — staging ~3 MB of parquet into
a pod first — needs a Job, a ConfigMap'd shell script and a manifest to review,
which is a lot of apparatus for an operation that runs about once a year when the
CIAA publishes. The pod already has egress to Hugging Face; this just uses it.

Note that it is a FLAG, not an id: the dataset downloaded is always the one the
map names. The map's id is what lands in every row's ``dataset`` provenance
field, so letting the command line name a different source could only ever make
the two disagree — and on this platform a provenance field that can lie is worse
than no field at all.

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

HF_API = "https://huggingface.co/api/datasets"

#: The git ref the parquet actually lives on. Hugging Face auto-converts a
#: dataset to parquet on a SEPARATE branch, and that branch has its own sha —
#: for this corpus, main is eafef6d7 (16:11:22Z) while refs/convert/parquet is
#: c3c6c673 (16:12:25Z). Recording main's sha would stamp every row with a
#: revision whose bytes were never loaded, which is the failure this field
#: exists to prevent. Percent-encoded because it is a URL path segment.
PARQUET_REF = "refs%2Fconvert%2Fparquet"

#: Per-file download timeout. The largest table in the CIAA corpus is ~2.5 MB.
DOWNLOAD_TIMEOUT = 300


def _get_json(url: str, timeout: int = 60) -> Any:
    """GET and parse JSON, mapping every failure mode onto OSError.

    ``urlopen`` raises OSError subclasses for the network, but a proxy or WAF
    that answers 200 with an HTML interstitial gets past that and blows up in
    ``json.load`` as a ValueError instead. The caller turns OSError into a
    CommandError, so normalise here rather than leaking a traceback.
    """
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.load(response)
    except ValueError as exc:  # JSONDecodeError is a ValueError
        raise OSError(f"{url} did not return JSON: {exc}") from exc


def _fetch_dataset(dataset: str, dest: Path, log) -> str:
    """Download the parquet this command reads into ``dest``; return its revision.

    Only the four configs ``REQUIRED_TABLES`` names. A dataset of this shape also
    publishes ``pages`` and ``table_cells``, which are an order of magnitude
    larger and unused — the transcript is not this command's to load, and a
    table's content rides in its ``markdown``.

    Each config is written to its own subdirectory, one file per shard. Hugging
    Face splits a large config across several parquet files and publishes the
    list; reading a hardcoded ``0.parquet`` would silently load a prefix of a
    sharded table and quietly drop the rest. Today every config here is one
    shard, which is exactly why this has to be handled before it is not.
    """
    meta = _get_json(f"{HF_API}/{dataset}/revision/{PARQUET_REF}")
    revision = (meta or {}).get("sha") or "" if isinstance(meta, dict) else ""
    log(f"  dataset {dataset} @ {revision or 'unknown revision'} (parquet branch)")

    shards = _get_json(f"{HF_API}/{dataset}/parquet")
    if not isinstance(shards, dict):
        raise OSError("parquet shard listing was not an object")

    for name in REQUIRED_TABLES:
        urls = (shards.get(name) or {}).get("train") or []
        if not urls:
            raise OSError(f"dataset publishes no parquet for config {name!r}")
        config_dir = dest / name
        config_dir.mkdir(parents=True, exist_ok=True)
        total = 0
        for index, url in enumerate(urls):
            with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT) as response:
                body = response.read()
            (config_dir / f"{index}.parquet").write_bytes(body)
            total += len(body)
        shard_note = "" if len(urls) == 1 else f" in {len(urls)} shards"
        log(f"  {name}  {total / 1024:.0f} KiB{shard_note}")
    return revision


def _config_sources(parquet_dir: Path) -> dict[str, list[str]] | None:
    """Locate each required config's parquet, or None if any is missing.

    Two layouts are supported, because the two ways in produce different ones:
    ``--parquet-dir`` points at a directory of flat ``<config>.parquet`` files,
    while ``--download`` writes ``<config>/<shard>.parquet`` so a sharded config
    stays whole. A config found in both places prefers the sharded directory.
    """
    found: dict[str, list[str]] = {}
    for name in REQUIRED_TABLES:
        sharded = sorted((parquet_dir / name).glob("*.parquet"))
        flat = parquet_dir / f"{name}.parquet"
        if sharded:
            found[name] = [str(p) for p in sharded]
        elif flat.is_file():
            found[name] = [str(flat)]
        else:
            return None
    return found


def _rows(
    con: duckdb.DuckDBPyConnection, sources: list[str], order_by: str
) -> list[dict]:
    """Read one config's parquet into a list of dicts, deterministically ordered.

    The order matters: ``ordinal`` is assigned from it and becomes the public URL
    key, so an unordered read would renumber every table on re-ingest. Passing
    the shard list to a single ``read_parquet`` makes duckdb sort ACROSS shards,
    which reading them one at a time and concatenating would not.
    """
    cur = con.execute(
        f"SELECT * FROM read_parquet(?) ORDER BY {order_by}", [sources]
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
            "--download",
            action="store_true",
            help=(
                "Fetch the parquet from the Hugging Face dataset the map names, "
                "and record its revision automatically."
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
                "Upstream dataset revision recorded as provenance. For use with "
                "--parquet-dir, where nothing can determine it; rejected with "
                "--download, which reads the real one from the dataset. May be "
                "left blank, in which case no revision is recorded."
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Resolve and validate everything, write nothing.",
        )

    def handle(self, *args, **options) -> None:
        if options["download"] and options["revision"]:
            # Not merely redundant: a hand-pasted sha silently winning over the
            # one read from the bytes actually downloaded is the same
            # provenance-can-lie failure --download exists to close.
            raise CommandError(
                "--revision cannot be combined with --download; the revision is "
                "read from the dataset. Use it only with --parquet-dir."
            )
        if options["download"]:
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
        if options["download"]:
            # Download the dataset the MAP names, never one named separately on
            # the command line. The map's id is what gets written to every row's
            # `dataset` provenance field, so a second, independent source for
            # "where the bytes came from" could only ever let the two disagree —
            # and a provenance field that can lie is worse than no field.
            try:
                fetched = _fetch_dataset(
                    docmap["dataset"], parquet_dir, self.stdout.write
                )
            except OSError as exc:
                # Urllib's failures (DNS, TLS, timeout, HTTP error) are all OSError
                # subclasses. A network problem is not a traceback-worthy bug.
                raise CommandError(
                    f"could not download dataset {docmap['dataset']!r}: {exc}"
                ) from exc
            revision = fetched
        options = {**options, "revision": revision}

        paths = _config_sources(parquet_dir)
        if paths is None:
            raise CommandError(
                f"{parquet_dir} does not hold a parquet file for each of: "
                + ", ".join(REQUIRED_TABLES)
            )

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
