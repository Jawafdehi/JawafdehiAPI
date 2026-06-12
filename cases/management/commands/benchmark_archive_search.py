"""Benchmark the PostgreSQL archive search path against real archive data."""

from __future__ import annotations

import json
import math
import statistics
import time

from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from rest_framework.test import APIRequestFactory

from cases.models import Case, CaseState, DocumentSource, JawafEntity
from cases.services.postgres_search import PostgresUnifiedSearchService

DEFAULT_QUERIES = (
    "",
    "भ्रष्टाचार",
    "procurement",
    "काठमाडौं",
    "entity:person",
    "procuremnt",
)


class Command(BaseCommand):
    help = "Measure archive-search p50/p95 latency on PostgreSQL."

    def add_arguments(self, parser):
        parser.add_argument("--iterations", type=int, default=20)
        parser.add_argument("--warmup", type=int, default=3)
        parser.add_argument("--max-p95-ms", type=float, default=500.0)
        parser.add_argument("--min-records", type=int, default=0)
        parser.add_argument("--fail-on-target", action="store_true")
        parser.add_argument("--query", action="append", dest="queries")

    def handle(self, *args, **options):
        if options["iterations"] < 1:
            raise CommandError("--iterations must be >= 1.")
        if options["warmup"] < 0:
            raise CommandError("--warmup must be >= 0.")

        if connection.vendor != "postgresql":
            raise CommandError("Archive search benchmarking requires PostgreSQL.")

        record_count = (
            Case.objects.filter(state=CaseState.PUBLISHED).count()
            + JawafEntity.objects.filter(
                case_relationships__case__state=CaseState.PUBLISHED
            )
            .distinct()
            .count()
            + DocumentSource.objects.filter(
                is_deleted=False,
                case_links__case__state=CaseState.PUBLISHED,
            )
            .distinct()
            .count()
        )
        minimum = options["min_records"]
        if record_count < minimum:
            raise CommandError(
                f"Expected at least {minimum} records; found {record_count}."
            )

        queries = options["queries"] or DEFAULT_QUERIES
        service = PostgresUnifiedSearchService()
        request = APIRequestFactory().get("/api/search/")

        for _ in range(options["warmup"]):
            for query in queries:
                self._search(service, request, query)

        samples = []
        for _ in range(options["iterations"]):
            for query in queries:
                started = time.perf_counter()
                self._search(service, request, query)
                samples.append((time.perf_counter() - started) * 1000)
        if not samples:
            raise CommandError("Archive search benchmark did not collect samples.")

        sorted_samples = sorted(samples)
        p95_index = max(0, math.ceil(0.95 * len(sorted_samples)) - 1)
        report = {
            "records": record_count,
            "queries": list(queries),
            "samples": len(samples),
            "p50_ms": round(statistics.median(samples), 2),
            "p95_ms": round(sorted_samples[p95_index], 2),
            "max_ms": round(max(samples), 2),
            "target_p95_ms": options["max_p95_ms"],
        }
        self.stdout.write(json.dumps(report, ensure_ascii=False))

        if options["fail_on_target"] and report["p95_ms"] > options["max_p95_ms"]:
            raise CommandError(
                f"p95 {report['p95_ms']} ms exceeds " f"{options['max_p95_ms']} ms."
            )

    def _search(self, service, request, query):
        return service.search(
            request=request,
            q=query,
            type=[],
            entity_type=[],
            role=[],
            case_type=[],
            tags=[],
            sort="relevance",
            page=1,
            page_size=4,
        )
