"""Run A/B test comparisons and generate enrichment quality reports."""

from __future__ import annotations

import json
import sys
from datetime import date

from django.core.management.base import BaseCommand

from cases.models import ABTestConfig, EditorFeedbackType, EnrichmentRunType, FewShotExample
from cases.services.quality import ABTestService, FewShotManager, QualityMetricsCollector


class Command(BaseCommand):
    help = "Run A/B test comparison and generate enrichment quality reports"

    def add_arguments(self, parser):
        parser.add_argument(
            "--run-type",
            required=True,
            choices=[c[0] for c in EnrichmentRunType.choices],
            help="Enrichment run type to operate on",
        )
        parser.add_argument(
            "--action",
            default="report",
            choices=["report", "compare", "promote-feedback"],
            help="Action to perform",
        )
        parser.add_argument(
            "--test-id",
            help="AB test config_id for compare action",
        )
        parser.add_argument(
            "--weeks",
            type=int,
            default=4,
            help="Number of weekly windows for report action",
        )
        parser.add_argument(
            "--output-format",
            default="table",
            choices=["table", "json"],
            help="Output format",
        )
        parser.add_argument(
            "--winning-variant",
            choices=["a", "b"],
            help="Which variant wins (for complete-test)",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=10,
            help="Max examples when promoting feedback",
        )
        parser.add_argument(
            "--feedback-type",
            default="incorrect",
            help="Filter feedback by EditorFeedbackType for promotion",
        )

    def handle(self, *args, **options):
        run_type = options["run_type"]
        action = options["action"]
        fmt = options["output_format"]

        if action == "report":
            self._action_report(run_type, options["weeks"], fmt)
        elif action == "compare":
            self._action_compare(options["test_id"], fmt)
        elif action == "promote-feedback":
            self._action_promote(run_type, options)

    def _action_report(self, run_type, num_weeks, fmt):
        collector = QualityMetricsCollector()
        trends = collector.get_trend_report(run_type, num_weeks)

        if fmt == "json":
            self.stdout.write(json.dumps(trends, indent=2, default=str))
            return

        self.stdout.write(f"\nQuality Trend Report — {run_type} ({num_weeks} weeks)\n")
        self.stdout.write("-" * 90)
        self.stdout.write(
            f"{'Week Starting':<14} {'Runs':>5} {'Enr.':>6} {'Failed':>7} "
            f"{'FB':>5} {'Avg Score':>10} {'Latency':>9} {'Tokens':>10}"
        )
        self.stdout.write("-" * 90)

        for t in trends:
            score = f"{t['avg_quality_score']:.2f}" if t["avg_quality_score"] else "N/A"
            lat = f"{t['avg_latency_ms']:.0f}ms" if t["avg_latency_ms"] else "N/A"
            self.stdout.write(
                f"{str(t['window_start']):<14} "
                f"{t['total_runs']:>5} "
                f"{t['total_enriched']:>6} "
                f"{t['total_failed']:>7} "
                f"{t['total_feedback']:>5} "
                f"{score:>10} "
                f"{lat:>9} "
                f"{t['total_tokens']:>10,}"
            )
        self.stdout.write("-" * 90)

    def _action_compare(self, test_id, fmt):
        if not test_id:
            self.stderr.write(self.style.ERROR("--test-id is required for compare action"))
            sys.exit(1)

        try:
            test = ABTestConfig.objects.select_related("variant_a", "variant_b").get(
                config_id=test_id
            )
        except ABTestConfig.DoesNotExist:
            self.stderr.write(self.style.ERROR(f"No AB test found with id: {test_id}"))
            sys.exit(1)

        svc = ABTestService()
        result = svc.compare_results(test)

        if fmt == "json":
            self.stdout.write(json.dumps(result, indent=2, default=str))
            return

        self.stdout.write(f"\nA/B Comparison — {test.config_id}\n")
        self.stdout.write(
            f"  Winner: {result['winner'].upper()} "
            f"(confidence: {result['confidence']:.3f})\n"
        )
        self.stdout.write("-" * 70)
        self.stdout.write(f"{'Metric':<20} {'Variant A':>22} {'Variant B':>22}")
        self.stdout.write("-" * 70)

        a = result["variant_a"]
        b = result["variant_b"]
        rows = [
            ("Runs", a["runs"], b["runs"]),
            ("Enriched", a["enriched"], b["enriched"]),
            ("Failed", a["failed"], b["failed"]),
            ("Total Tokens", f"{a['total_tokens']:,}", f"{b['total_tokens']:,}"),
            ("Cost USD", a["total_cost_usd"], b["total_cost_usd"]),
            ("Avg Latency", f"{a['avg_latency_ms']}ms" if a["avg_latency_ms"] else "N/A",
             f"{b['avg_latency_ms']}ms" if b["avg_latency_ms"] else "N/A"),
        ]
        for label, va, vb in rows:
            self.stdout.write(f"{label:<20} {str(va):>22} {str(vb):>22}")
        self.stdout.write("-" * 70)

    def _action_promote(self, run_type, options):
        from cases.models import EditorFeedback

        fb_type = options["feedback_type"]
        limit = options["limit"]

        feedbacks = EditorFeedback.objects.filter(
            run_type=run_type,
            feedback_type=fb_type,
            corrected_output__isnull=False,
        ).select_related("case")[:limit]

        manager = FewShotManager()
        count = 0
        for fb in feedbacks:
            if FewShotExample.objects.filter(source_feedback=fb).exists():
                continue
            try:
                manager.promote_from_feedback(fb)
                count += 1
            except (ValueError, KeyError) as e:
                self.stderr.write(
                    self.style.WARNING(f"Skipping feedback {fb.pk}: {e}")
                )

        self.stdout.write(
            self.style.SUCCESS(
                f"Promoted {count} editor feedback(s) to few-shot examples "
                f"(run_type={run_type}, feedback_type={fb_type})"
            )
        )
