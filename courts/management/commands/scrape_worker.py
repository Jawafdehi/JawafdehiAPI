"""In-process consumer for ``court_scrape`` jobs.

The court scraper writes court cases + hearings into the ngm lake via the courts
ORM, so — unlike the DB-free review/material poller — its consumer must run with
ngm DB access. This command claims ``court_scrape`` jobs straight off the queue
engine (``jobs.queue``, in-process: no HTTP/OIDC hop), runs the crawl, and
finalizes each job, inheriting the queue's lease/retry/backoff/dedup and the
``/api/jobs`` dashboard. Run it inside the platform image (has
``NGM_DATABASE_URL``).

Read-only by default (reports how many ``court_scrape`` jobs are queued);
``--apply`` claims, runs and finalizes.

    manage.py scrape_worker                     # READ-ONLY: report queued, exit
    manage.py scrape_worker --apply --once      # drain queued scrapes, exit (CronJob)
    manage.py scrape_worker --apply             # poll forever (daemon)
"""

import time
import traceback

from django.core.management.base import BaseCommand

from courts.job_handlers import HANDLERS, BadCourtScrapePayload
from jobs import queue as jobs_queue
from jobs.models import Job

KIND = "court_scrape"

#: The only non-retryable failure: a bad/unknown court in the payload (raised as
#: BadCourtScrapePayload up-front, before any crawl). Everything else — portal
#: flakes, parse hiccups, DB errors — is retried with backoff, then dead-lettered.
_NON_RETRYABLE = (BadCourtScrapePayload,)


class Command(BaseCommand):
    help = "Drain court_scrape jobs from the central queue (in-process, ngm DB)."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true",
                            help="claim/run/finalize (mutates the queue + ngm "
                                 "lake); without it the worker only reports the "
                                 "queued count")
        parser.add_argument("--once", action="store_true",
                            help="with --apply: drain currently-available jobs "
                                 "then exit (the CronJob mode)")
        parser.add_argument("--poll", type=float, default=5.0,
                            help="seconds between polls when idle (daemon mode)")
        parser.add_argument("--max-jobs", type=int, default=None,
                            help="finalize at most this many jobs, then exit")

    def handle(self, *args, **o):
        if not o["apply"]:
            queued = Job.objects.filter(kind=KIND, status=Job.QUEUED).count()
            self.stdout.write(
                f"scrape_worker (read-only): {queued} queued {KIND} job(s); "
                "use --apply to drain."
            )
            return

        once = o["once"]
        poll = float(o["poll"])
        max_jobs = o["max_jobs"]
        finalized = 0
        self.stdout.write(
            self.style.MIGRATE_HEADING(f"scrape_worker up: kind={KIND} once={once}")
        )
        # `max_jobs is None` = unlimited; the `< max_jobs` guard also makes
        # `--max-jobs 0` a no-op-and-exit rather than (falsy-zero) unlimited.
        while max_jobs is None or finalized < max_jobs:
            job = jobs_queue.claim_next([KIND])
            if job is None:
                if once:
                    break
                time.sleep(poll)
                continue
            self._run(job)
            finalized += 1
        self.stdout.write(
            self.style.SUCCESS(f"scrape_worker: finalized {finalized} job(s).")
        )

    def _run(self, job):
        self.stdout.write(f"  claimed job {job.pk} payload={job.payload}")
        handler = HANDLERS[KIND]
        try:
            result = handler(
                job.payload, on_stage=lambda s: jobs_queue.touch(job, stage=s)
            )
        except Exception as exc:  # noqa: BLE001 - report failure to the queue
            retryable = not isinstance(exc, _NON_RETRYABLE)
            err = f"{exc}\n{traceback.format_exc()[:2000]}"
            self._finalize_failed(job, err, retryable)
            self.stderr.write(f"  job {job.pk} failed (retryable={retryable}): {exc}")
            return
        try:
            jobs_queue.finalize(job, status=Job.DONE, result=result)
        except jobs_queue.JobNotRunning:
            self.stderr.write(f"  job {job.pk}: lease lost mid-run; result dropped.")
            return
        self.stdout.write(self.style.SUCCESS(
            f"  finished job {job.pk}: {result['cases']} cases / "
            f"{result['hearings']} hearings across {result['courts']} court(s)"
        ))

    def _finalize_failed(self, job, err, retryable):
        try:
            jobs_queue.finalize(
                job, status=Job.FAILED, error=err, retryable=retryable
            )
        except jobs_queue.JobNotRunning:
            self.stderr.write(f"  job {job.pk}: lease lost; not finalizing failure.")
