"""In-process consumer for ``material_extract`` jobs.

Extraction writes a document's pages, tables and figures through the materials
ORM, so — unlike the DB-free review/convert poller — its consumer runs with ngm
database access. This command claims ``material_extract`` jobs straight off the
queue engine (``jobs.queue``, in-process: no HTTP/OIDC hop), extracts, and
finalizes each job, inheriting the queue's lease/retry/backoff/dedup and the
``/api/jobs`` dashboard. Run it inside the platform image (it needs
``NGM_DATABASE_URL``). Mirrors ``courts.management.commands.scrape_worker``,
which is the same pattern for the same reason.

Read-only by default (reports how many extractions are queued); ``--apply``
claims, runs and finalizes.

    manage.py extract_worker                      # READ-ONLY: report queued, exit
    manage.py extract_worker --apply --once       # drain, then exit (CronJob mode)
    manage.py extract_worker --apply              # poll forever (Deployment mode)

**Which of those two you run decides what "on demand" means.** A reader who asks
for a document's content waits for a worker to pick the job up: on a five-minute
CronJob that is up to five minutes before extraction *starts*, and with a
long-poll Deployment it is seconds. The queue is identical either way — this is a
deployment choice, and it is deliberately not made here.
"""

from __future__ import annotations

import time
import traceback

from django.core.management.base import BaseCommand

from jobs import queue as jobs_queue
from jobs.models import Job
from materials.extract import EXTRACT_KIND, NothingToExtract, handle_material_extract

#: Failures that will never succeed on a re-run: a material with no document, or
#: bytes that are not a readable PDF. Everything else — a download timeout, an
#: object store 503, a transient DB error — is retried with backoff and then
#: dead-lettered.
_NON_RETRYABLE = (NothingToExtract,)


class Command(BaseCommand):
    help = "Drain material_extract jobs from the central queue (in-process, ngm DB)."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--apply",
            action="store_true",
            help=(
                "claim/run/finalize (mutates the queue + stored content); without "
                "it the worker only reports the queued count"
            ),
        )
        parser.add_argument(
            "--once",
            action="store_true",
            help="with --apply: drain currently-available jobs then exit",
        )
        parser.add_argument(
            "--poll",
            type=float,
            default=5.0,
            help="seconds between polls when idle (daemon mode)",
        )
        parser.add_argument(
            "--max-jobs",
            type=int,
            default=None,
            help="finalize at most this many jobs, then exit",
        )

    def handle(self, *args, **options) -> None:
        if not options["apply"]:
            queued = Job.objects.filter(kind=EXTRACT_KIND, status=Job.QUEUED).count()
            self.stdout.write(
                f"extract_worker (read-only): {queued} queued {EXTRACT_KIND} job(s); "
                "use --apply to drain."
            )
            return

        once = options["once"]
        poll = float(options["poll"])
        max_jobs = options["max_jobs"]
        finalized = 0
        self.stdout.write(
            self.style.MIGRATE_HEADING(
                f"extract_worker up: kind={EXTRACT_KIND} once={once}"
            )
        )
        # ``max_jobs is None`` = unlimited; the ``< max_jobs`` guard also makes
        # ``--max-jobs 0`` a no-op-and-exit rather than (falsy-zero) unlimited.
        while max_jobs is None or finalized < max_jobs:
            job = jobs_queue.claim_next([EXTRACT_KIND])
            if job is None:
                if once:
                    break
                time.sleep(poll)
                continue
            self._run(job)
            finalized += 1
        self.stdout.write(
            self.style.SUCCESS(f"extract_worker: finalized {finalized} job(s).")
        )

    def _run(self, job) -> None:
        iri = (job.payload or {}).get("material_iri")
        self.stdout.write(f"  claimed job {job.pk} for {iri}")
        try:
            result = handle_material_extract(
                job.payload, on_stage=lambda s: jobs_queue.touch(job, stage=s)
            )
        except Exception as exc:  # noqa: BLE001 - report failure to the queue
            retryable = not isinstance(exc, _NON_RETRYABLE)
            self._finalize_failed(
                job, f"{exc}\n{traceback.format_exc()[:2000]}", retryable
            )
            self.stderr.write(f"  job {job.pk} failed (retryable={retryable}): {exc}")
            return
        try:
            jobs_queue.finalize(job, status=Job.DONE, result=result)
        except jobs_queue.JobNotRunning:
            # The lease lapsed and the reaper re-queued this job while we worked.
            # The CONTENT is already written and is correct — the writer replaces
            # a document wholesale — so this is a dropped result, not lost work;
            # the re-run will find it stored and skip.
            self.stderr.write(f"  job {job.pk}: lease lost mid-run; result dropped.")
            return
        if result.get("skipped"):
            self.stdout.write(f"  job {job.pk}: {result['skipped']}")
            return
        self.stdout.write(
            self.style.SUCCESS(
                f"  finished job {job.pk}: {result['pages']} pages, "
                f"{result['tables']} tables, {result['figures']} figures "
                f"({len(result.get('chart_pages') or [])} chart page(s) unread)"
            )
        )

    def _finalize_failed(self, job, error: str, retryable: bool) -> None:
        try:
            jobs_queue.finalize(
                job, status=Job.FAILED, error=error, retryable=retryable
            )
        except jobs_queue.JobNotRunning:
            self.stderr.write(f"  job {job.pk}: lease lost; not finalizing failure.")
