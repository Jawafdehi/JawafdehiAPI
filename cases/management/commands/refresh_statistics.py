"""``refresh_statistics`` — recompute the /api/statistics/ payload snapshot.

Runs the full NES/NGM/Jawafdehi aggregation (multi-second on prod) and upserts
the shared ``cases.StatisticsSnapshot`` row that ``StatisticsView`` serves with
an O(1) primary-key lookup. Meant to run on a schedule (k8s CronJob) so no web
request ever pays the aggregation cost.
"""

from __future__ import annotations

import time

from django.core.management.base import BaseCommand
from django.db import OperationalError

from cases.services.statistics import refresh_statistics
from config.db_router import route_reads_to_replica

# The aggregation is read-only and idempotent, so a transient database error is
# safe to retry. The motivating failure is a hot-standby cancelling a multi-second
# scan mid-flight ("canceling statement due to conflict with recovery") when WAL
# replay must remove row versions the query is still reading — transient, and gone
# by the next attempt. On the FINAL attempt we read from the primary (never a
# standby), which cannot self-conflict, so the snapshot still refreshes even if the
# replica is under continuous replay pressure. Cold-start connection blips (the
# same class the CronJob pre-flight wait guards against) are covered too.
_MAX_ATTEMPTS = 3


class Command(BaseCommand):
    help = "Recompute the /api/statistics/ payload and upsert the shared snapshot row."

    def handle(self, *args, **options):
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            # Route the heavy counts onto the read replicas (where configured) to
            # keep them off the primaries — except on the last attempt, where we
            # fall back to the primary to guarantee the job completes. The snapshot
            # upsert is a pure write and is always routed to the primary regardless
            # of this flag.
            route_reads_to_replica(attempt < _MAX_ATTEMPTS)
            try:
                stats = refresh_statistics()
                break
            except OperationalError as exc:
                if attempt == _MAX_ATTEMPTS:
                    raise
                backoff = 2**attempt
                self.stderr.write(
                    f"refresh_statistics: transient database error on attempt "
                    f"{attempt}/{_MAX_ATTEMPTS} ({exc}); retrying in {backoff}s"
                )
                time.sleep(backoff)
            finally:
                route_reads_to_replica(False)
        self.stdout.write(
            self.style.SUCCESS(
                f"Statistics snapshot refreshed (last_updated={stats['last_updated']})"
            )
        )
