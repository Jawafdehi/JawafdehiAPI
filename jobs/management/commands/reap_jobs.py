"""Safety-net reaper for the job queue.

Claims already reap lapsed leases opportunistically (jobs.queue.claim_next runs a
lazy sweep), so this command is only a backstop for idle periods when no claims
are happening. Run it on a slow timer (systemd timer / cron, e.g. every minute):

    manage.py reap_jobs            # one sweep, report count
    manage.py reap_jobs --loop 60  # sweep every 60s forever

A reaped job is re-queued with backoff if it has attempts left, else dead-lettered
(see jobs.queue.reap_expired).
"""

import time

from django.core.management.base import BaseCommand

from jobs import queue


class Command(BaseCommand):
    help = "Re-queue or dead-letter RUNNING jobs whose lease has expired."

    def add_arguments(self, parser):
        parser.add_argument(
            "--loop",
            type=float,
            default=None,
            metavar="SECONDS",
            help="Sweep repeatedly every SECONDS. Omit for a single sweep.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=50,
            help="Max jobs reaped per sweep (default 50).",
        )

    def handle(self, *args, **opts):
        loop = opts["loop"]
        limit = opts["limit"]
        if loop is None:
            n = queue.reap_expired(limit=limit)
            self.stdout.write(self.style.SUCCESS(f"reaped {n} expired job(s)"))
            return
        self.stdout.write(self.style.MIGRATE_HEADING(f"reaper up: every {loop}s"))
        while True:
            n = queue.reap_expired(limit=limit)
            if n:
                self.stdout.write(f"reaped {n} expired job(s)")
            time.sleep(loop)
