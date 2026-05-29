"""Health check view for enrichment pipeline observability.

Exposes:
  GET /api/health/enrichment/ — pipeline status + metric snapshot
  GET /api/health/enrichment/metrics — Prometheus textfile export
"""

from __future__ import annotations

from django.http import HttpRequest, HttpResponse, JsonResponse
from django.views.decorators.http import require_http_methods

from cases.observability import (
    circuit_breaker_trips,
    export_textfile_to_string,
    likhit_failures,
    llm_call,
    nepali_script_coverage,
    null_source_type,
    pipeline_duration,
)


@require_http_methods(["GET"])
def enrichment_health(request: HttpRequest) -> JsonResponse:
    """Return enrichment pipeline health status and current metric snapshots."""
    pipeline_snap = pipeline_duration.snapshot()
    llm_snap = llm_call.snapshot()
    cb_snap = circuit_breaker_trips.snapshot()

    total_llm_calls = llm_snap["total"]
    llm_failures = sum(
        val for label, val in llm_snap.get("by_label", {}).items()
        if 'outcome="failure"' in label
    )

    # Circuit is unhealthy if any trips have been recorded
    circuit_ok = cb_snap["total"] == 0

    # LLM success rate
    if total_llm_calls > 0:
        llm_success_rate = (total_llm_calls - llm_failures) / total_llm_calls
    else:
        llm_success_rate = 1.0

    status = "healthy"
    if not circuit_ok:
        status = "degraded"
    if llm_success_rate < 0.5 and total_llm_calls > 5:
        status = "unhealthy"

    return JsonResponse(
        {
            "status": status,
            "checks": {
                "circuit_breaker": {"ok": circuit_ok, "trips": int(cb_snap["total"])},
                "llm_success_rate": {
                    "ok": llm_success_rate >= 0.9,
                    "rate": round(llm_success_rate, 4),
                },
            },
            "metrics": {
                "pipeline_cases": pipeline_snap["count"],
                "pipeline_avg_seconds": round(
                    pipeline_snap["sum"] / max(1, pipeline_snap["count"]), 2
                ),
                "llm_calls_total": int(total_llm_calls),
                "likhit_failures_total": int(likhit_failures.snapshot()["total"]),
                "null_source_type_ratio": null_source_type.snapshot(),
                "nepali_script_coverage": nepali_script_coverage.snapshot(),
            },
        }
    )


@require_http_methods(["GET"])
def enrichment_metrics(request: HttpRequest) -> HttpResponse:
    """Prometheus textfile export endpoint for node_exporter scraping."""
    return HttpResponse(
        export_textfile_to_string(), content_type="text/plain; charset=utf-8"
    )
