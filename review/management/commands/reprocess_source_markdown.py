"""Reprocess document-source markdown for published cases.

For every PUBLISHED case (or a given ``--slug`` set), pull the case, convert its
document sources to Markdown via likhit, and attach that Markdown back to each
source upstream (a MARKDOWN-role url on the DocumentSource). This is the on-
demand version of the maintenance fix the review poller performs as a side
effect of grading — useful for backfilling markdown across the whole published
corpus or refreshing it after a converter change (e.g. a new likhit OCR DPI).

This runs as a REMOTE HTTP CLIENT (no DB access), exactly like the poller:

  - Cases are READ from the public Jawafdehi API (``JAWAFDEHI_API_BASE``); the
    case list returns PUBLISHED cases for anonymous reads.
  - Markdown is WRITTEN to the casework API (``CASEWORK_API_BASE`` +
    ``CASEWORK_POLLER_TOKEN``) via the shared ``UpstreamClient``.

The conversion + attach-candidate logic is shared with the poller through
``review.converter.convert_case_to_attach_candidates``.

Idempotent + resumable: by default a source that already has a MARKDOWN url is
left alone (and is therefore skipped on re-runs). ``--overwrite`` forces
re-conversion and replaces the existing markdown.

Examples:
  # Dry run over all published cases (read + convert, no upstream writes)
  manage.py reprocess_source_markdown --dry-run

  # Reprocess two specific cases
  manage.py reprocess_source_markdown --slug case-a --slug case-b

  # Refresh markdown for the first 5 cases after a converter change
  manage.py reprocess_source_markdown --limit 5 --overwrite --sleep 1
"""

import time

from django.core.management.base import BaseCommand, CommandError

from review import converter, jds_client
from review.upstream_client import UpstreamClient, UpstreamError


class Command(BaseCommand):
    help = "Reprocess document-source markdown for published cases (convert via likhit, attach upstream)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--slug",
            action="append",
            dest="slugs",
            help="Reprocess a specific case slug (repeatable). Default: all published cases.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Only process the first N cases.",
        )
        parser.add_argument(
            "--overwrite",
            action="store_true",
            help="Re-convert and replace markdown even if the source already has a MARKDOWN url.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Convert and report candidates without writing anything upstream.",
        )
        parser.add_argument(
            "--sleep",
            type=float,
            default=0.0,
            help="Seconds to pause between cases (be gentle on prod).",
        )
        parser.add_argument(
            "--read-sleep",
            type=float,
            default=0.2,
            help=(
                "Seconds to pause between case-read API calls (listing + each "
                "case fetch) to avoid rate-limiting (HTTP 429). Default 0.2."
            ),
        )

    def handle(self, *args, **opts):
        slugs = opts.get("slugs")
        limit = opts.get("limit")
        overwrite = opts["overwrite"]
        dry_run = opts["dry_run"]
        sleep = float(opts["sleep"])
        self.read_sleep = float(opts["read_sleep"])

        # Upstream client is only needed for writes; --dry-run skips it (and
        # therefore does not require CASEWORK_POLLER_TOKEN).
        client = None
        if not dry_run:
            try:
                client = UpstreamClient(
                    on_log=self.stdout.write, on_err=self.stderr.write
                )
            except UpstreamError as e:
                raise CommandError(str(e))

        self.stdout.write(
            self.style.MIGRATE_HEADING(
                f"reprocess_source_markdown: dry_run={dry_run} overwrite={overwrite}"
            )
        )

        slugs = self._resolve_slugs(slugs, limit)
        self.stdout.write(f"cases to process: {len(slugs)}")

        totals = {
            "cases": 0,
            "cases_failed": 0,
            "converted": 0,
            "skipped": 0,
            "errored": 0,
            "attached": 0,
            "attach_skipped": 0,
            "attach_failed": 0,
        }

        for slug in slugs:
            try:
                self._process_case(slug, client, overwrite, dry_run, totals)
                totals["cases"] += 1
            except Exception as e:  # noqa: BLE001 - isolate per-case failures
                totals["cases_failed"] += 1
                self.stderr.write(f"  case {slug} failed: {e}")
            if sleep:
                time.sleep(sleep)

        self._print_summary(totals, dry_run)

    # ---- helpers -----------------------------------------------------

    def _resolve_slugs(self, slugs, limit):
        """Return the list of case slugs to process (explicit, or all published)."""
        if slugs:
            return slugs[:limit] if limit else slugs
        out = []
        for case in jds_client.iter_paginated("cases/"):
            slug = case.get("slug")
            if slug:
                out.append(slug)
            if limit and len(out) >= limit:
                break
        return out

    def _process_case(self, slug, client, overwrite, dry_run, totals):
        # Throttle the per-case read to avoid rate-limiting (HTTP 429) when
        # sweeping the whole published corpus. jds_client also retries 429/5xx
        # with backoff, but a steady pace avoids tripping the limiter at all.
        if self.read_sleep:
            time.sleep(self.read_sleep)
        case = jds_client.get_case(slug)
        converted, candidates = converter.convert_case_to_attach_candidates(
            case, overwrite=overwrite
        )
        # Per-source conversion outcome (for the summary + visibility).
        for s in converted:
            status = s.get("conversion_status")
            if status in ("converted", "attached"):
                totals["converted"] += 1
            elif status == "error":
                totals["errored"] += 1
                # Surface WHY a source failed (web-archive-only link, dead url,
                # OCR timeout, ...) so errors are explainable, not just counted.
                self.stderr.write(
                    f"    error source {s.get('source_id')}: "
                    f"{s.get('conversion_note') or 'unknown'}"
                )
            else:
                totals["skipped"] += 1
                self.stdout.write(
                    f"    skipped source {s.get('source_id')}: "
                    f"{s.get('conversion_note') or 'unknown'}"
                )

        self.stdout.write(
            f"  {slug}: {len(converted)} sources, {len(candidates)} to attach"
        )

        if dry_run:
            for c in candidates:
                self.stdout.write(f"    [dry-run] would attach source {c['source_id']}")
            return

        summary = client.attach_markdown(candidates, overwrite=overwrite)
        totals["attached"] += summary["attached"]
        totals["attach_skipped"] += summary["skipped"]
        totals["attach_failed"] += summary["failed"]

    def _print_summary(self, t, dry_run):
        self.stdout.write(self.style.MIGRATE_HEADING("summary"))
        self.stdout.write(
            f"  cases processed: {t['cases']}  failed: {t['cases_failed']}"
        )
        self.stdout.write(
            f"  sources converted: {t['converted']}  skipped: {t['skipped']}  errored: {t['errored']}"
        )
        if dry_run:
            self.stdout.write("  (dry run — nothing attached upstream)")
        else:
            self.stdout.write(
                f"  markdown attached: {t['attached']}  "
                f"skipped: {t['attach_skipped']}  failed: {t['attach_failed']}"
            )
        self.stdout.write(self.style.SUCCESS("reprocess_source_markdown: done."))
