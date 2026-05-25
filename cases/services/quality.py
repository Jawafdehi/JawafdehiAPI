"""Continuous improvement services for the enrichment pipeline (Phase 6)."""

from __future__ import annotations

import hashlib
import uuid
from datetime import date
from typing import TYPE_CHECKING

from django.db.models import Avg, Count, Q, Sum
from django.utils import timezone

if TYPE_CHECKING:
    from cases.models import (
        ABTestConfig,
        EditorFeedback,
        EnrichmentRun,
        FewShotExample,
        PromptVariant,
    )


class QualityMetricsCollector:
    """Collect and store enrichment run metrics during pipeline execution."""

    def start_run(
        self,
        run_type: str,
        prompt_variant: "PromptVariant | None" = None,
        ab_test: "ABTestConfig | None" = None,
    ) -> "EnrichmentRun":
        from cases.models import EnrichmentRun

        run = EnrichmentRun.objects.create(
            run_id=uuid.uuid4().hex,
            run_type=run_type,
            prompt_variant=prompt_variant,
            ab_test=ab_test,
            started_at=timezone.now(),
        )
        return run

    def record_case_result(
        self,
        run: "EnrichmentRun",
        tier: str,
        tokens_in: int,
        tokens_out: int,
        latency_ms: float,
        success: bool,
    ) -> None:
        run.llm_call_count += 1
        run.total_input_tokens += tokens_in
        run.total_output_tokens += tokens_out

        breakdown = dict(run.tier_breakdown or {})
        breakdown[tier] = breakdown.get(tier, 0) + 1
        run.tier_breakdown = breakdown

        if run.avg_latency_ms is None:
            run.avg_latency_ms = latency_ms
        else:
            n = run.llm_call_count
            run.avg_latency_ms = (
                run.avg_latency_ms * (n - 1) + latency_ms
            ) / n

        run.total_cases += 1
        if success:
            run.enriched_count += 1
        else:
            run.failed_count += 1

        run.save(
            update_fields=[
                "llm_call_count",
                "total_input_tokens",
                "total_output_tokens",
                "tier_breakdown",
                "avg_latency_ms",
                "total_cases",
                "enriched_count",
                "failed_count",
            ]
        )

    def record_skip(self, run: "EnrichmentRun") -> None:
        run.total_cases += 1
        run.skipped_count += 1
        run.save(update_fields=["total_cases", "skipped_count"])

    def complete_run(self, run: "EnrichmentRun") -> "EnrichmentRun":
        run.completed_at = timezone.now()
        run.save(update_fields=["completed_at"])
        return run

    def compute_trend(
        self, run_type: str, window_start: date, window_end: date
    ) -> dict:
        from cases.models import EnrichmentRun, EditorFeedback

        runs = EnrichmentRun.objects.filter(
            run_type=run_type,
            completed_at__date__gte=window_start,
            completed_at__date__lte=window_end,
        )
        feedbacks = EditorFeedback.objects.filter(
            run_type=run_type,
            created_at__date__gte=window_start,
            created_at__date__lte=window_end,
        )

        agg = runs.aggregate(
            total_runs=Count("id"),
            total_enriched=Sum("enriched_count"),
            total_failed=Sum("failed_count"),
            total_tokens=Sum("total_input_tokens") + Sum("total_output_tokens"),
            total_cost=Sum("total_cost_usd"),
        )
        avg_latency = runs.aggregate(
            avg_lat=Avg("avg_latency_ms")
        )["avg_lat"]

        fb_agg = feedbacks.aggregate(avg_score=Avg("quality_score"))

        return {
            "run_type": run_type,
            "window_start": window_start,
            "window_end": window_end,
            "total_runs": agg["total_runs"] or 0,
            "total_enriched": agg["total_enriched"] or 0,
            "total_failed": agg["total_failed"] or 0,
            "total_feedback": feedbacks.count(),
            "avg_quality_score": round(fb_agg["avg_score"], 2) if fb_agg["avg_score"] else None,
            "total_tokens": agg["total_tokens"] or 0,
            "total_cost_usd": str(agg["total_cost"] or 0),
            "avg_latency_ms": round(avg_latency, 1) if avg_latency else None,
        }

    def get_trend_report(
        self, run_type: str, num_weeks: int = 4
    ) -> list[dict]:
        today = date.today()
        results = []
        for i in range(num_weeks):
            end = today - date.resolution * (i * 7)
            start = end - date.resolution * 6
            results.append(self.compute_trend(run_type, start, end))
        return list(reversed(results))


class ABTestService:
    """A/B test lifecycle manager: routing, result comparison, completion."""

    def get_active_test(self, run_type: str) -> "ABTestConfig | None":
        from cases.models import ABTestConfig

        return (
            ABTestConfig.objects.filter(
                run_type=run_type, is_active=True
            )
            .select_related("variant_a", "variant_b")
            .first()
        )

    def select_variant(self, test: "ABTestConfig") -> "PromptVariant":
        import random

        return test.variant_a if random.random() < test.traffic_split else test.variant_b

    def compare_results(self, test: "ABTestConfig") -> dict:
        from cases.models import EnrichmentRun

        runs_a = EnrichmentRun.objects.filter(
            ab_test=test, prompt_variant=test.variant_a, completed_at__isnull=False
        )
        runs_b = EnrichmentRun.objects.filter(
            ab_test=test, prompt_variant=test.variant_b, completed_at__isnull=False
        )

        def _summarize(qs, variant):
            agg = qs.aggregate(
                runs=Count("id"),
                enriched=Sum("enriched_count"),
                failed=Sum("failed_count"),
                total_tokens=Sum("total_input_tokens") + Sum("total_output_tokens"),
                total_cost=Sum("total_cost_usd"),
            )
            avg_lat = qs.aggregate(lat=Avg("avg_latency_ms"))["lat"]
            return {
                "variant": variant.name,
                "runs": agg["runs"] or 0,
                "enriched": agg["enriched"] or 0,
                "failed": agg["failed"] or 0,
                "total_tokens": agg["total_tokens"] or 0,
                "total_cost_usd": str(agg["total_cost"] or 0),
                "avg_latency_ms": round(avg_lat, 1) if avg_lat else None,
            }

        summary_a = _summarize(runs_a, test.variant_a)
        summary_b = _summarize(runs_b, test.variant_b)

        total_runs = summary_a["runs"] + summary_b["runs"]
        if total_runs < 2:
            winner = "insufficient_data"
            confidence = 0.0
        else:
            # Simple heuristic: compare enriched / (enriched + failed) ratios
            def _rate(s):
                denom = s["enriched"] + s["failed"]
                return s["enriched"] / denom if denom > 0 else 0.0

            rate_a = _rate(summary_a)
            rate_b = _rate(summary_b)
            diff = abs(rate_a - rate_b)

            if diff < 0.02:
                winner = "tie"
            elif rate_a > rate_b:
                winner = "a"
            else:
                winner = "b"
            confidence = min(diff * 10, 0.95)

        return {
            "variant_a": summary_a,
            "variant_b": summary_b,
            "winner": winner,
            "confidence": round(confidence, 3),
        }

    def complete_test(
        self, test: "ABTestConfig", winning_variant: "PromptVariant"
    ) -> None:
        test.is_active = False
        test.ended_at = timezone.now()
        test.save(update_fields=["is_active", "ended_at"])

        loser = (
            test.variant_b
            if winning_variant.pk == test.variant_a.pk
            else test.variant_a
        )
        loser.is_active = False
        loser.save(update_fields=["is_active"])

    def create_test(
        self,
        variant_a: "PromptVariant",
        variant_b: "PromptVariant",
        traffic_split: float = 0.5,
        sample_size: int | None = None,
    ) -> "ABTestConfig":
        from cases.models import ABTestConfig

        config_id = f"ab-{variant_a.run_type}-{uuid.uuid4().hex[:12]}"
        return ABTestConfig.objects.create(
            config_id=config_id,
            variant_a=variant_a,
            variant_b=variant_b,
            run_type=variant_a.run_type,
            traffic_split=traffic_split,
            sample_size=sample_size,
            started_at=timezone.now(),
        )


class FewShotManager:
    """Select, inject, and curate few-shot examples for prompt enrichment."""

    MAX_EXAMPLES_PER_PROMPT = 3

    def get_examples_for_run_type(
        self, run_type: str, limit: int = MAX_EXAMPLES_PER_PROMPT
    ) -> list["FewShotExample"]:
        from cases.models import FewShotExample

        return list(
            FewShotExample.objects.filter(
                run_type=run_type, is_validated=True
            )
            .order_by("last_used_at")
            .only("input_snapshot", "expected_output")[:limit]
        )

    def inject_into_prompt(
        self, base_template: str, examples: list["FewShotExample"]
    ) -> str:
        if not examples:
            return base_template

        blocks = []
        for i, ex in enumerate(examples, 1):
            inp = ex.input_snapshot
            out = ex.expected_output
            blocks.append(
                f"INPUT {i}:\n{inp}\n\nEXPECTED OUTPUT {i}:\n{out}"
            )

        separator = "\n\n---\n\n"
        return "\n\n".join(blocks) + separator + base_template

    def promote_from_feedback(
        self, feedback: "EditorFeedback", created_by=None
    ) -> "FewShotExample":
        from cases.models import FewShotExample

        if not feedback.corrected_output:
            raise ValueError("Cannot promote feedback without corrected_output")

        return FewShotExample.objects.create(
            run_type=feedback.run_type,
            input_snapshot=feedback.original_output,
            expected_output=feedback.corrected_output,
            source_feedback=feedback,
            is_validated=False,
            created_by=created_by,
        )

    def validate_example(
        self, example: "FewShotExample"
    ) -> "FewShotExample":
        example.is_validated = True
        example.save(update_fields=["is_validated"])
        return example

    def record_usage(self, examples: list["FewShotExample"]) -> None:
        now = timezone.now()
        for ex in examples:
            ex.usage_count += 1
            ex.last_used_at = now
            ex.save(update_fields=["usage_count", "last_used_at"])


def build_slug(case_id: str, run_type: str) -> str:
    """Generate a stable run_id from case_id + run_type + date."""
    today = date.today().isoformat()
    raw = f"{case_id}|{run_type}|{today}"
    return hashlib.sha256(raw.encode()).hexdigest()[:20]
