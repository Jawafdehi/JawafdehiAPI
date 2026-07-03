"""``refresh_statistics`` — recompute the /api/statistics/ payload snapshot.

Runs the full NES/NGM/Jawafdehi aggregation (multi-second on prod) and upserts
the shared ``cases.StatisticsSnapshot`` row that ``StatisticsView`` serves with
an O(1) primary-key lookup. Meant to run on a schedule (k8s CronJob) so no web
request ever pays the aggregation cost.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from cases.services.statistics import refresh_statistics
from config.db_router import route_reads_to_replica


class Command(BaseCommand):
    help = "Recompute the /api/statistics/ payload and upsert the shared snapshot row."

    def handle(self, *args, **options):
        # The aggregation is read-only and tolerant of replica lag, so opt its
        # reads into the read replicas (where configured) to keep the heavy
        # counts off the primaries. The snapshot upsert is a pure write and is
        # always routed to the primary regardless of this flag.
        route_reads_to_replica(True)
        try:
            stats = refresh_statistics()
        finally:
            route_reads_to_replica(False)
        self.stdout.write(
            self.style.SUCCESS(
                f"Statistics snapshot refreshed (last_updated={stats['last_updated']})"
            )
        )
