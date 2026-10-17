"""Extract a batch of materials' documents into pages, tables and figures.

The batch half of the pipeline. ``ingest_document_content`` loads a corpus
somebody else extracted; this opens the documents we hold.

    # what WOULD be extracted, and why each candidate was picked or skipped
    uv run python manage.py extract_document_content --source ciaa_annual_report --dry-run

    # queue them for the worker (the normal path)
    uv run python manage.py extract_document_content --source ciaa_annual_report --limit 20

    # extract here, in this process, writing as each document finishes
    uv run python manage.py extract_document_content --iri <iri> --inline

    # the chart worklist: render every candidate page for a vision read
    uv run python manage.py extract_document_content --iri <iri> --inline \
        --render-charts work/2026-10-16-charts/

**Queue by default, not inline.** Measured on the FY2081/82 CIAA annual report
(17.7 MB, 446 pages): 160 s and 433 MiB peak RSS for one document. A source with
a thousand of those is a two-day run that a disconnected terminal kills halfway
through, where the queue gives each document its own lease, retry and record.
``--inline`` exists for one document, for a fixture, and for a cluster Job that
deliberately wants no worker in the loop.

**Nothing here is reindexed.** Stored content does not feed the search index —
the indexer reads ``data["text"]``, which ``material_convert`` writes — so an
extraction changes what ``…/content`` serves and nothing a search can see. The
two pipelines are independent on purpose; do not "fix" one by running the other.
"""

from __future__ import annotations

from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from ...document_content import store_document_content
from ...extract import (
    EXTRACT_KIND,
    PRIORITY_BACKFILL,
    DocumentFetchError,
    NothingToExtract,
    content_is_stale,
    document_urls,
    enqueue_material_extract,
    fetch_and_extract,
)
from ...models import DocumentContent, Material

#: How many rows to look at when hunting for candidates. A source filter plus a
#: limit is the normal case and stops long before this; the cap is here so an
#: unfiltered run over 346k materials cannot sit in a loop reading JSONB for an
#: hour before printing anything.
SCAN_CEILING = 20_000


class Command(BaseCommand):
    help = "Extract (or queue extraction of) document content for a set of materials."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--source",
            action="append",
            default=[],
            help="restrict to this material source token; repeatable",
        )
        parser.add_argument(
            "--iri",
            action="append",
            default=[],
            help="extract exactly this material IRI; repeatable",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=50,
            help="at most this many documents (default 50)",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help=(
                "re-extract documents we already hold content for (the default "
                "is to leave them alone)"
            ),
        )
        parser.add_argument(
            "--inline",
            action="store_true",
            help="extract in THIS process instead of queueing for the worker",
        )
        parser.add_argument(
            "--render-charts",
            help=(
                "with --inline: write a PNG per chart-candidate page plus a "
                "manifest under this directory, for a vision read"
            ),
        )
        parser.add_argument(
            "--recheck",
            action="store_true",
            help=(
                "also re-read documents whose stored content is older than the "
                "file it was read from (a re-upload, a corrected scan)"
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="resolve and report candidates, change nothing",
        )

    def handle(self, *args, **options) -> None:
        if options["render_charts"] and not options["inline"]:
            # Rendering needs the document's bytes in hand, which only the
            # inline path has. Queueing with --render-charts would silently
            # render nothing and report success.
            raise CommandError("--render-charts only applies with --inline.")
        if options["limit"] < 1:
            raise CommandError("--limit must be at least 1.")

        candidates, skipped = self._candidates(options)
        for note in skipped:
            self.stdout.write(self.style.WARNING(f"  skipped {note}"))
        if not candidates:
            self.stdout.write(self.style.WARNING("no candidates matched."))
            return

        if options["dry_run"]:
            for iri, urls in candidates:
                self.stdout.write(f"  would extract {iri} from {urls[0]}")
            self.stdout.write(
                self.style.SUCCESS(
                    f"dry run OK — {len(candidates)} document(s) would be extracted"
                )
            )
            return

        if options["inline"]:
            self._run_inline(candidates, options)
        else:
            self._enqueue(candidates, options)

    # ── selection ────────────────────────────────────────────────────────────

    def _candidates(self, options: dict) -> tuple[list[tuple[str, list[str]]], list[str]]:
        """``[(iri, source_urls)]`` plus a note per material that cannot be done.

        A material is a candidate when it is live, carries a document this
        pipeline can open, and (unless ``--force``) has no stored content yet.
        Everything else is REPORTED rather than silently dropped: "this source
        has no extractable documents" and "this source is already done" look
        identical from an empty run, and they call for opposite next steps.
        """
        rows = Material.objects.using("ngm").filter(is_deleted=False)
        if options["iri"]:
            rows = rows.filter(pk__in=options["iri"])
        if options["source"]:
            rows = rows.filter(source__in=options["source"])
        # Empty unless --recheck asks us to look at documents we already hold,
        # and defined unconditionally so --force (which skips the branch below
        # entirely) does not reach the loop with it unbound.
        fresh: set[str] = set()
        if not options["force"]:
            # Materials we hold content for are excluded in SQL rather than in
            # Python, because they are the cheap majority once a source is done
            # and they would otherwise eat the scan budget. STALENESS is checked
            # per candidate below — it needs the document's own provenance, which
            # is JSONB, and a row whose file has changed is rare enough to pay
            # for individually.
            if options["recheck"]:
                fresh = set(
                    DocumentContent.objects.using("ngm").values_list(
                        "material_id", flat=True
                    )
                )
            else:
                rows = rows.exclude(
                    pk__in=DocumentContent.objects.using("ngm").values("material_id")
                )
        cursor = "" if options["iri"] else self._cursor(options)
        if cursor:
            rows = rows.filter(pk__gt=cursor)
        rows = rows.order_by("pk").only("iri", "data")

        wanted = set(options["iri"])
        found: list[tuple[str, list[str]]] = []
        skipped: list[str] = []
        seen = 0
        stale = 0
        with_document = 0
        for row in rows.iterator(chunk_size=200):
            seen += 1
            if seen > SCAN_CEILING:
                skipped.append(
                    f"stopped after scanning {SCAN_CEILING} materials (from "
                    f"{cursor or 'the start'}); the next run resumes after the "
                    "last material queued"
                )
                break
            urls = document_urls(row.data or {})
            # Counted here, not where candidates are: "carries a document" is a
            # fact about the ARCHIVE, and reporting only the ones we queued
            # would say "0 carry a document" for a source that is fully read —
            # the one answer that must not look like the one where the scan
            # never reached it.
            if urls:
                with_document += 1
            if urls and row.pk in fresh:
                # --recheck: we hold content for this one, so the only reason to
                # queue it is that the FILE changed under us.
                if not content_is_stale(row.pk, row.data or {}):
                    continue
                stale += 1
            if not urls:
                # Only worth a line when the caller named this exact document.
                # Over a whole source it is the common case (most materials are a
                # scraped page, not a PDF) and would bury the real output.
                if row.pk in wanted:
                    skipped.append(f"{row.pk}: carries no extractable document")
                continue
            found.append((row.pk, urls))
            if len(found) >= options["limit"]:
                break

        # The coverage statement. An automatic pipeline that reports only what
        # it queued cannot tell "everything is read" from "the scan never got
        # there" — which is the whole failure mode of a trigger plus a sweep.
        self.stdout.write(
            f"  scanned {seen} material(s): {with_document} carry a document, "
            f"{stale} have content older than their file"
        )
        for iri in sorted(wanted - {iri for iri, _urls in found}):
            if not any(note.startswith(f"{iri}:") for note in skipped):
                # Named but absent from the queryset: gone, soft-deleted, or
                # already extracted and --force was not passed.
                skipped.append(
                    f"{iri}: not selected — no live material, or content already "
                    "stored (use --force to replace it)"
                )
        return found, skipped

    def _cursor(self, options: dict) -> str:
        """Where the last run got to: the highest IRI we have queued a job for.

        **Without this, a repeating batch over the whole archive never finishes.**
        Selection walks materials in IRI order and stops at ``SCAN_CEILING``;
        already-extracted ones are excluded in SQL, but the ~94% that carry no
        PDF are not, and they are what the scan budget is spent on. So every tick
        re-scans the same prefix of the archive, finds the same nothing, and the
        long tail is never reached — a job that looks healthy and makes no
        progress, which is worse than one that fails.
        
        The cursor is the job history itself rather than a new table, the way the
        court sweep's rotation is: the jobs carry the material IRI in their
        payload, and the highest one is where to start. If job rows are ever
        pruned the cursor resets and the walk repeats — wasteful, not wrong,
        since an extracted document is skipped in SQL anyway.
        """
        from jobs.models import Job

        iris = [
            (job.payload or {}).get("material_iri") or ""
            for job in Job.objects.filter(kind=EXTRACT_KIND).only("payload")
        ]
        highest = max(iris, default="")
        if highest:
            self.stdout.write(f"  resuming after {highest}")
        return highest

    # ── the two ways to run ──────────────────────────────────────────────────

    def _enqueue(self, candidates, options: dict) -> None:
        queued = 0
        for iri, _urls in candidates:
            job = enqueue_material_extract(
                iri,
                force=options["force"] or options["recheck"],
                # Behind both the on-write trigger and a reader asking by name:
                # a backfill is work nobody is waiting on.
                priority=PRIORITY_BACKFILL,
            )
            if job is None:  # pragma: no cover - only when the jobs app is absent
                raise CommandError("the jobs app is unavailable; use --inline")
            queued += 1
            self.stdout.write(f"  job {job.pk} [{job.status}] {iri}")
        self.stdout.write(
            self.style.SUCCESS(
                f"queued {queued} extraction(s). Run them with: "
                "manage.py extract_worker --apply --once"
            )
        )

    def _run_inline(self, candidates, options: dict) -> None:
        from ...extraction.charts import write_chart_renders

        render_root = Path(options["render_charts"]) if options["render_charts"] else None
        totals = {"docs": 0, "pages": 0, "tables": 0, "figures": 0, "charts": 0}
        failures: list[str] = []
        for iri, urls in candidates:
            doc_id = iri.rstrip("/").rsplit("/", 1)[-1]
            try:
                # The same fetch-and-read the worker runs, so an inline
                # extraction and a queued one cannot produce different content.
                _url, data, bundle = fetch_and_extract(iri, urls, doc_id=doc_id)
            except (DocumentFetchError, NothingToExtract) as exc:
                # One document's failure is reported and the run carries on: a
                # batch over a source routinely meets a dead link, and aborting
                # would make the rest of the source hostage to it.
                failures.append(f"{iri}: {exc}")
                continue
            # One transaction per document, not one per run: a long inline run
            # that dies at document 400 should leave the first 399 extracted —
            # each is independently complete and separately addressable, which is
            # not true of one corpus loaded from a single dataset revision.
            written = store_document_content(iri, bundle)
            totals["docs"] += 1
            for key in ("pages", "tables", "figures"):
                totals[key] += written[key]
            totals["charts"] += len(bundle.chart_pages)
            self.stdout.write(
                f"  {iri}: {written['pages']} pages, {written['tables']} tables, "
                f"{len(bundle.chart_pages)} chart page(s) [{bundle.transcript_verdict}]"
            )
            if render_root is not None:
                manifest = write_chart_renders(
                    data,
                    render_root / doc_id,
                    doc_id=doc_id,
                    pages=bundle.chart_pages,
                )
                self.stdout.write(
                    f"    rendered {len(manifest['candidates'])} page(s) → "
                    f"{render_root / doc_id}"
                )

        summary = (
            f"extracted {totals['docs']} document(s): {totals['pages']} pages, "
            f"{totals['tables']} tables, {totals['figures']} figures; "
            f"{totals['charts']} chart page(s) detected and NOT read"
        )
        if failures:
            self.stdout.write(self.style.WARNING(summary))
            for note in failures:
                self.stdout.write(self.style.WARNING(f"  failed {note}"))
            return
        self.stdout.write(self.style.SUCCESS(summary))
