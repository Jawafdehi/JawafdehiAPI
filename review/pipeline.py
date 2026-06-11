"""Pipeline: pull case -> convert sources via likhit -> score rules.

Ported into jawafdehi-api. The only change from the standalone casework system
is that the case is pulled through ``case_provider`` (local DB by default, or
the live JDS API when REVIEW_CASE_SOURCE="remote") instead of always hitting
the network.
"""

import time
import traceback

from django.conf import settings
from django.utils import timezone

from . import (
    bedrock_judge,
    case_provider,
    casetype,
    code_rules,
    converter,
    jds_client,
    scorer,
)
from .models import CaseReview, ReviewConfig


def run_review(review_id):
    """Execute the full review pipeline for a CaseReview row (blocking)."""
    review = CaseReview.objects.get(pk=review_id)
    review.status = CaseReview.STATUS_RUNNING
    review.stage = "fetching_case"
    review.started_at = timezone.now()
    review.duration_seconds = None
    review.save(
        update_fields=[
            "status",
            "stage",
            "started_at",
            "duration_seconds",
            "updated_at",
        ]
    )
    t0 = time.monotonic()

    try:
        # 1. Pull case (local DB by default; live JDS API when configured).
        case = case_provider.get_case(review.slug)
        case.setdefault("slug", review.slug)
        review.case_title = case.get("title", "") or ""
        review.case_state = case.get("state", "") or ""

        # 2. Extract + convert sources
        review.stage = "converting_sources"
        review.save(update_fields=["case_title", "case_state", "stage", "updated_at"])
        sources = jds_client.extract_sources(case)
        review.source_count = len(sources)
        review.save(update_fields=["source_count", "updated_at"])

        converted = converter.convert_all(sources)
        review.sources_converted = sum(
            1
            for s in converted
            if s.get("conversion_status") in ("converted", "attached")
        )
        review.save(update_fields=["sources_converted", "updated_at"])

        # 3. Analyze each source: summarise the likhit-converted text and
        #    determine how this source contributes to the case (description,
        #    timeline, key allegations, entities). Runs before rule scoring.
        review.stage = "analyzing_sources"
        review.save(update_fields=["stage", "updated_at"])
        ctype = casetype.detect(case)
        source_analyses = bedrock_judge.analyze_sources(
            scorer.build_case_summary(case), converted, ctype["label"]
        )

        # 4. Score (rule-centered), reusing the per-source analyses.
        review.stage = "scoring"
        review.save(update_fields=["stage", "updated_at"])
        rules = code_rules.get_enabled_rules()
        config = ReviewConfig.get_active()
        result = scorer.score_case(
            case, converted, rules, config, source_analyses=source_analyses
        )
        result["model_id_used"] = settings.BEDROCK_MODEL_ID

        review.case_type = (result.get("case_type") or {}).get("type", "")
        review.result = result
        review.status = CaseReview.STATUS_DONE
        review.stage = "complete"
        review.completed_at = timezone.now()
        review.duration_seconds = round(time.monotonic() - t0, 1)
        review.save()
    except Exception as e:  # noqa: BLE001
        review.status = CaseReview.STATUS_FAILED
        review.stage = "failed"
        review.duration_seconds = round(time.monotonic() - t0, 1)
        review.error = f"{e}\n{traceback.format_exc()[:2000]}"
        review.save(
            update_fields=["status", "stage", "error", "duration_seconds", "updated_at"]
        )


def run_review_async(review_id):
    """Enqueue a review for the global dispatcher.

    Dispatch is NOT done in-process here (gunicorn runs multiple workers, so an
    in-process pool would multiply concurrency by the worker count). Instead the
    review row is left status=pending and a single dedicated dispatcher daemon
    (manage.py review_dispatcher) picks pending rows up and runs at most
    settings.REVIEW_MAX_PARALLEL of them at once — a true GLOBAL queue of N.
    """
    return review_id


def run_many_async(review_ids):
    """No-op dispatch: rows stay pending for the global dispatcher to pick up."""
    return list(review_ids)
