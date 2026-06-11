"""Global review dispatcher: run the pending-review queue at most N at a time.

The API's submit endpoint only creates a CaseReview row with status=pending and
does NOT run it in-process (gunicorn has multiple workers, so an in-process pool
would multiply concurrency by the worker count). This single long-lived daemon is
the ONE place reviews actually execute, so settings.REVIEW_MAX_PARALLEL is a true
GLOBAL cap on concurrent reviews (operator directive: queue of 3, env-configurable
via REVIEW_MAX_PARALLEL).

Behaviour:
  - Polls for status=pending rows (FIFO by id) and submits them to a
    ThreadPoolExecutor of size REVIEW_MAX_PARALLEL.
  - Never lets more than REVIEW_MAX_PARALLEL reviews run at once.
  - Resilient: a crashed review is recorded failed by run_review itself.
  - Idempotent claim: marks a row 'running' under a row-lock-ish guarded update
    before handing to the pool so two dispatcher instances can't double-run it.
  - Runs forever (poll loop) by default; --once drains the current queue then
    exits (handy for the bulk regrade batch run under systemd).

Run durably under systemd (forces local sqlite + orion-admin like serve.sh):
  manage.py review_dispatcher            # daemon, poll forever
  manage.py review_dispatcher --once     # drain pending then exit
"""

import concurrent.futures
import time

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import close_old_connections, transaction

from review import pipeline
from review.models import CaseReview, ReviewConfig


class Command(BaseCommand):
    help = "Run the global pending-review queue at most REVIEW_MAX_PARALLEL at a time."

    def add_arguments(self, parser):
        parser.add_argument(
            "--once",
            action="store_true",
            help="Drain the currently-pending reviews then exit (no infinite poll).",
        )
        parser.add_argument(
            "--poll",
            type=float,
            default=3.0,
            help="Seconds between polls for new pending reviews (daemon mode).",
        )

    def _claim(self, review_id):
        """Atomically flip a pending row to running; return True if we claimed it."""
        with transaction.atomic():
            updated = CaseReview.objects.filter(
                pk=review_id, status=CaseReview.STATUS_PENDING
            ).update(status=CaseReview.STATUS_RUNNING, stage="queued")
        return updated == 1

    def _run_one(self, review_id):
        try:
            pipeline.run_review(review_id)
        except Exception as e:  # noqa: BLE001 - run_review records its own failures
            self.stderr.write(f"review {review_id} raised: {e}")
        finally:
            close_old_connections()

    def handle(self, *args, **opts):
        ReviewConfig.get_active()
        max_parallel = max(1, int(getattr(settings, "REVIEW_MAX_PARALLEL", 3)))
        poll = float(opts["poll"])
        once = opts["once"]

        self.stdout.write(
            self.style.MIGRATE_HEADING(
                f"review_dispatcher up: max_parallel={max_parallel} once={once}"
            )
        )

        ex = concurrent.futures.ThreadPoolExecutor(max_workers=max_parallel)
        inflight = {}  # future -> review_id
        idle_polls = 0

        try:
            while True:
                # Top up free slots from the pending queue (FIFO).
                free = max_parallel - len(inflight)
                if free > 0:
                    pending_ids = list(
                        CaseReview.objects.filter(status=CaseReview.STATUS_PENDING)
                        .order_by("id")
                        .values_list("id", flat=True)[
                            : free * 3
                        ]  # over-fetch; claim guards races
                    )
                    for rid in pending_ids:
                        if len(inflight) >= max_parallel:
                            break
                        if self._claim(rid):
                            fut = ex.submit(self._run_one, rid)
                            inflight[fut] = rid
                            self.stdout.write(
                                f"  start review {rid} (inflight={len(inflight)})"
                            )

                if inflight:
                    idle_polls = 0
                    done, _ = concurrent.futures.wait(
                        list(inflight.keys()),
                        timeout=poll,
                        return_when=concurrent.futures.FIRST_COMPLETED,
                    )
                    for fut in done:
                        rid = inflight.pop(fut)
                        self.stdout.write(
                            f"  finished review {rid} (inflight={len(inflight)})"
                        )
                    close_old_connections()
                else:
                    # No inflight work.
                    if once:
                        # drained
                        if not CaseReview.objects.filter(
                            status=CaseReview.STATUS_PENDING
                        ).exists():
                            break
                    idle_polls += 1
                    time.sleep(poll)
        finally:
            ex.shutdown(wait=True)

        self.stdout.write(self.style.SUCCESS("review_dispatcher: queue drained."))
