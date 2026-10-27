"""Load a corpus of document content (pages, tables, charts) for known materials.

Two ways in. In production, let the command fetch the dataset itself — one
command, nothing to stage, and the revision is captured automatically:

    uv run python manage.py ingest_document_content \
        --map materials/data/ciaa_annual_report_docmap.json \
        --download

Or point it at parquet already on disk, for offline work and the tests:

    uv run python manage.py ingest_document_content \
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
into is generic (see materials.models.DocumentContent). To onboard a second
corpus, write a second map file — no migration, no code change here.

**It holds the whole corpus in memory, inside one transaction.** Every config is
read into Python dicts up front so that nothing is written until everything
resolves, and the write is one atomic block. For the CIAA corpus that is ~8k rows
and 13 MB of page text, which is nothing. A corpus an order of magnitude larger
wants this restructured into a per-document read before it wants a bigger pod —
so measure before pointing the second map file at something much bigger.

**A dataset that gains a document fails this command until the map gains it too.**
That is the 1:1 check below doing its job, not a bug: an unmapped ``doc_id`` is
refused rather than skipped, because a report silently absent from the archive is
the failure this whole mapping exists to prevent. The practical consequence is
that the annual re-run after the CIAA publishes will abort on first attempt, and
the fix is to add the new fiscal year to the map file — not to loosen the check.

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

from ...document_content import store_document_content
from ...extraction.bundle import (
    ContentBundle,
    DeclaredCounts,
    FigurePointRecord,
    FigureRecord,
    PageRecord,
    TableRecord,
)
from ...models import Material

#: Parquet tables the command needs, by basename. ``documents`` carries one row
#: per document; the rest are keyed to it by ``doc_id`` (tables/figures) or by
#: ``figure_uid`` (figure_data).
REQUIRED_TABLES = ("documents", "pages", "tables", "figures", "figure_data")

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

    Only the five configs ``REQUIRED_TABLES`` names. A dataset of this shape also
    publishes ``table_cells`` and ``quality``, which stay unread: a table's
    content rides in its ``markdown``, so the 678k cell rows are a second
    representation of what we already store, and ``quality`` repeats per-document
    verdicts that ``documents`` already carries.

    ``pages`` IS read, and that is a change from the first version of this
    command, which skipped it on the grounds that "the transcript is not this
    command's to load". The result was 41 documents holding 1,735 tables and no
    readable text at all — the richest structure in the archive sitting on top of
    the least readable corpus. It is 10k rows for this dataset.

    Each config is written to its own subdirectory, one file per shard. Hugging
    Face splits a large config across several parquet files and publishes the
    list; reading a hardcoded ``0.parquet`` would silently load a prefix of a
    sharded table and quietly drop the rest. Today every config here is one
    shard, which is exactly why this has to be handled before it is not.
    """
    meta = _get_json(f"{HF_API}/{dataset}/revision/{PARQUET_REF}")
    revision = (meta or {}).get("sha") or "" if isinstance(meta, dict) else ""
    if not revision:
        # Abort rather than stamp every row with a blank revision. --download
        # exists so provenance comes from the bytes actually fetched; proceeding
        # here would quietly REPLACE a previously recorded good sha with nothing,
        # and "ingested from an unknown revision" is indistinguishable afterwards
        # from "ingested before we recorded revisions at all".
        raise OSError(
            f"dataset {dataset!r} published no revision sha for {PARQUET_REF}"
        )
    log(f"  dataset {dataset} @ {revision} (parquet branch)")

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

    # Re-read the branch sha and refuse if it moved while we were downloading.
    # The shard listing and the file URLs are NOT pinned to a revision — Hugging
    # Face's parquet endpoints serve whatever the branch points at now — so a
    # dataset republished mid-run would leave us with bytes from one commit
    # stamped with the sha of another. That is precisely the provenance-can-lie
    # failure PARQUET_REF exists to prevent, so it is an abort and not a warning:
    # re-running is cheap, and a wrong revision on 35 documents is not detectable
    # after the fact.
    after = _get_json(f"{HF_API}/{dataset}/revision/{PARQUET_REF}")
    moved = (after or {}).get("sha") if isinstance(after, dict) else None
    if moved != revision:
        raise OSError(
            f"dataset {dataset!r} was republished during the download "
            f"({revision} → {moved or 'unknown'}); re-run to get a consistent set"
        )
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
    except (TypeError, ValueError, OverflowError):
        # OverflowError for a non-finite float: int(float("inf")) raises, and an
        # uncaught one here aborts the whole corpus transaction with a traceback.
        # Parquet's int64 columns cannot hold one, so this is insurance against
        # the next corpus rather than an observed failure — but it is one word.
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


def _tristate(value: Any) -> bool | None:
    """``verified_clean`` is a nullable flag. None stays None; everything else is
    truth-tested — but NaN is filtered first, because ``bool(nan)`` is True and
    would turn "not checked" into "verified"."""
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    return bool(value)


#: Which stored counter each of our own row counts should match.
#:
#: **Pages are deliberately absent.** ``n_pages`` is the PDF's page count, not a
#: count of rows the dataset publishes in its ``pages`` config — a 255-page
#: report can legitimately publish fewer page rows than that, and
#: ``pages_with_text`` is a sub-count of the text we recovered rather than of the
#: rows either. Comparing either against our row count would fire on every run,
#: and a warning that always fires is one nobody reads on the day it means
#: something. The three below each count the same set we do.
_COUNTER_COLUMNS = (
    ("tables", "n_tables"),
    ("figures", "n_figures"),
    ("points", "n_figure_data_points"),
)


def _shortfalls(doc_id: str, doc: dict, counts: dict[str, int]) -> list[str]:
    """Where the upstream's counters and the rows we wrote disagree.

    Only a SHORTFALL is reported — upstream claiming more than we stored, which
    means we failed to ingest part of what the dataset says exists. The opposite
    (we hold more than upstream counted) is upstream's own bookkeeping being
    behind its data, which costs a reader nothing: every row we hold is still
    fetchable, the counter just undersells it.
    """
    notes = []
    for ours, column in _COUNTER_COLUMNS:
        expected = _int(doc.get(column))
        actual = counts[ours]
        if expected > actual:
            notes.append(
                f"{doc_id}: upstream says {expected} {ours}, we stored {actual} "
                f"({expected - actual} missing)"
            )
    return notes


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
    help = "Ingest document content for the materials named in a map file."

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
            with tempfile.TemporaryDirectory(prefix="content-") as tmp:
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
        pages_by_doc: dict[str, list[dict]] = {}
        for row in _rows(con, paths["pages"], "doc_id, page_no"):
            pages_by_doc.setdefault(row["doc_id"], []).append(row)
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

        totals = {"docs": 0, "pages": 0, "tables": 0, "figures": 0, "points": 0}
        shortfalls: list[str] = []
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
                    pages=pages_by_doc.get(doc_id, []),
                    tables=tables_by_doc.get(doc_id, []),
                    figures=figures_by_doc.get(doc_id, []),
                    points_by_figure=points_by_figure,
                )
                totals["docs"] += 1
                for key in ("pages", "tables", "figures", "points"):
                    totals[key] += counts[key]
                shortfalls.extend(_shortfalls(doc_id, doc, counts))
                self.stdout.write(
                    f"  {doc_id}: {counts['pages']} pages, {counts['tables']} tables, "
                    f"{counts['figures']} figures, {counts['points']} points"
                )

        summary = (
            f"ingested {totals['docs']} document(s): {totals['pages']} pages, "
            f"{totals['tables']} tables, "
            f"{totals['figures']} figures, {totals['points']} points"
        )
        if not shortfalls:
            self.stdout.write(self.style.SUCCESS(summary))
            return

        # The stored counters are the UPSTREAM's, by design — see _ingest_one. The
        # point of that choice is that a shortfall in our ingest cannot redefine
        # itself as the truth; it is NOT that the shortfall is acceptable. So say
        # so here, or the API goes on advertising rows a client cannot fetch and
        # the only record of the gap is a log line nobody re-reads.
        #
        # A warning rather than an abort, for the same reason a soft-deleted
        # material is skipped rather than fatal: these counters are another
        # project's bookkeeping, and letting their arithmetic error block the
        # whole corpus would be handing them a veto over our ingest.
        self.stdout.write(self.style.WARNING(summary))
        self.stdout.write(
            self.style.WARNING(
                f"  {len(shortfalls)} counter(s) disagree with the rows we wrote. "
                "The API will report the upstream figure, so a client may see "
                "items it cannot fetch:"
            )
        )
        for note in shortfalls:
            self.stdout.write(self.style.WARNING(f"    {note}"))

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
        pages: list[dict],
        tables: list[dict],
        figures: list[dict],
        points_by_figure: dict[str, list[dict]],
    ) -> dict[str, int]:
        """Map this dataset's parquet columns onto a bundle, then store it.

        Everything below the mapping — delete-then-insert, the ``key`` a client
        addresses a table by, the material's ETag — belongs to
        ``materials.document_content`` and is shared with the PDF extractor. What
        stays here is the part only this dataset has: its column names, and the
        NaN/JSON-string conventions of the parquet it publishes.
        """
        bundle = ContentBundle(
            doc_id=doc_id,
            dataset=dataset,
            dataset_revision=revision,
            page_count=_int(doc.get("n_pages")),
            text_source=_text(doc.get("text_source")),
            transcript_verdict=_text(doc.get("transcript_quality_verdict")),
            figures_verdict=_text(doc.get("figures_quality_verdict")),
            # The upstream's own counters, which the writer stores in preference
            # to a count of the rows we managed to write — see DeclaredCounts.
            declared=DeclaredCounts(
                tables=_int(doc.get("n_tables")),
                figures=_int(doc.get("n_figures")),
                points=_int(doc.get("n_figure_data_points")),
                pages_with_text=_int(doc.get("pages_with_text")),
            ),
            pages=[
                PageRecord(
                    page_no=_int(row.get("page_no")),
                    uid=_text(row.get("page_uid")),
                    text=_text(row.get("text")),
                    coverage_state=_text(row.get("coverage_state")),
                    anchor_fidelity=_text(row.get("anchor_fidelity")),
                    ocr_source=_text(row.get("ocr_source")),
                    landscape=_bool(row.get("landscape")),
                    n_table_blocks=_int(row.get("n_table_blocks")),
                    chars=_int(row.get("chars")),
                )
                for row in pages
            ],
            tables=[
                TableRecord(
                    page_no=_int(row.get("page_no")),
                    index_on_page=_int(row.get("table_index_on_page"), 1),
                    uid=_text(row.get("table_uid")),
                    caption=_text(row.get("intro_text")),
                    header=_header(row.get("header_json")),
                    n_rows=_int(row.get("n_rows")),
                    n_cols=_int(row.get("n_cols")),
                    fidelity=_text(row.get("anchor_fidelity")),
                    markdown=_text(row.get("markdown")),
                )
                for row in tables
            ],
            figures=[
                FigureRecord(
                    page_no=_int(row.get("page_no")),
                    index_on_page=_int(row.get("figure_index_on_page"), 1),
                    uid=_text(row.get("figure_uid")),
                    title=_text(row.get("title_ne")),
                    chart_type=_text(row.get("chart_type")),
                    unit=_text(row.get("unit")),
                    x_axis=_text(row.get("x_axis")),
                    y_axis=_text(row.get("y_axis")),
                    notes=_text(row.get("notes")),
                    verify_note=_text(row.get("verify_note")),
                    verified=_tristate(row.get("verified_clean")),
                    points=[
                        FigurePointRecord(
                            point_index=_int(point.get("point_index")),
                            label=_text(point.get("label")),
                            series=_text(point.get("series")),
                            value=_float(point.get("value")),
                            estimated=_bool(point.get("value_estimated")),
                        )
                        # Keyed on the UNTRUNCATED upstream uid: the stored uid is
                        # cut to the column width, so looking points up off the
                        # stored string would silently drop every point of an
                        # over-long figure uid.
                        for point in points_by_figure.get(
                            _text(row.get("figure_uid")), []
                        )
                    ],
                )
                for row in figures
            ],
        )
        return store_document_content(iri, bundle)
