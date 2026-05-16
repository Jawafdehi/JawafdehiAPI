"""
Management command to select high-priority CIAA cases for Phase 2c enrichment.

Selection criteria (weighted priority score):
  1. bigo amount      — primary factor; higher bigo = higher priority
  2. defendant count  — complexity proxy; more defendants = more impactful
  3. data richness    — cases with court_cases, notes, or press release references

Usage::

    # List top 30 priority cases (default)
    python manage.py select_priority_cases

    # List top 20 priority cases
    python manage.py select_priority_cases --limit 20

    # Output only case IDs (for piping into another command)
    python manage.py select_priority_cases --ids-only --limit 40

    # Detailed output with all scoring fields
    python manage.py select_priority_cases --verbose

    # Export selected case IDs to a file for later processing
    python manage.py select_priority_cases --output-file priority_cases.txt

The selection produces a priority score between 0.0 and 1.0 computed from
normalised bigo (log-scale), entity count, and data-completeness signals.
"""

from __future__ import annotations

import math
from typing import List, Optional, Tuple

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Count, Q

from case_workflows.workflows.ciaa_caseworker.constants import CIAA_CASE_NUMBERS
from cases.models import Case, CaseState, CaseType


def _compute_priority_score(
    bigo: Optional[int],
    defendant_count: int,
    total_entity_count: int,
    has_court_cases: bool,
    has_notes: bool,
) -> float:
    """
    Compute a priority score between 0.0 and 1.0.

    Scoring components (all 0.0–1.0):
      - bigo_score:      log-scale normalisation of bigo amount
      - complexity_score: based on defendant and total entity counts
      - richness_score:   data completeness (court_cases + notes)
    """
    # ── bigo score (log-scale) ──────────────────────────────────────
    if bigo and bigo > 0:
        bigo_score = min(1.0, math.log10(float(bigo)) / 12.0)
    else:
        bigo_score = 0.0

    # ── complexity score ────────────────────────────────────────────
    complexity_score = min(1.0, (defendant_count * 0.15) + (total_entity_count * 0.05))

    # ── data-richness score ─────────────────────────────────────────
    richness = 0.0
    if has_court_cases:
        richness += 0.4
    if has_notes:
        richness += 0.2

    # Weighted composite
    return (0.55 * bigo_score) + (0.30 * complexity_score) + (0.15 * richness)


def _select_priority_cases(
    limit: int,
) -> List[Tuple[str, str, float, Optional[int], int]]:
    """
    Return list of ``(case_id, title, score, bigo, defendant_count)`` tuples
    sorted by descending priority score.

    Only considers CORRUPTION cases in DRAFT or IN_REVIEW state whose title
    contains a known CIAA Special Court case number.
    """

    # Base queryset: CIAA corruption cases in enrichment states
    base = (
        Case.objects.filter(
            case_type=CaseType.CORRUPTION,
            state__in=[CaseState.DRAFT, CaseState.IN_REVIEW],
        )
        .annotate(
            total_entities=Count("entity_relationships"),
            accused_count=Count(
                "entity_relationships",
                filter=Q(entity_relationships__relationship_type="accused"),
            ),
        )
        .values_list(
            "case_id",
            "title",
            "bigo",
            "accused_count",
            "total_entities",
            "court_cases",
            "notes",
        )
    )

    # Filter to CIAA Special Court cases only
    rows = [
        (cid, title, bigo, acc, total, cc, notes)
        for (cid, title, bigo, acc, total, cc, notes) in base
        if any(num in (title or "") for num in CIAA_CASE_NUMBERS)
    ]

    # Compute scores
    scored = []
    for cid, title, bigo, acc, total, cc, notes in rows:
        score = _compute_priority_score(
            bigo=bigo,
            defendant_count=acc or 0,
            total_entity_count=total or 0,
            has_court_cases=bool(cc),
            has_notes=bool(notes and notes.strip()),
        )
        scored.append((cid, title, score, bigo, acc or 0, total or 0))

    # Sort by score descending, then bigo descending for ties
    scored.sort(key=lambda x: (x[2], x[3] or 0), reverse=True)

    return [
        (cid, title, score, bigo, acc)
        for (cid, title, score, bigo, acc, _) in scored[:limit]
    ]


class Command(BaseCommand):
    help = (
        "Select high-priority CIAA draft cases for Phase 2c enrichment. "
        "Outputs cases sorted by a composite priority score (bigo + "
        "complexity + data richness)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit",
            type=int,
            default=30,
            help="Maximum number of cases to output (default: 30).",
        )
        parser.add_argument(
            "--ids-only",
            action="store_true",
            help="Output only case IDs, one per line.",
        )
        parser.add_argument(
            "--verbose",
            action="store_true",
            help="Include score breakdown for each case.",
        )
        parser.add_argument(
            "--output-file",
            type=str,
            default=None,
            help="Write selected case IDs to a file (one per line).",
        )

    def handle(self, *args, **options):
        limit = options["limit"]
        if limit <= 0:
            raise CommandError("--limit must be a positive integer")
        ids_only = options["ids_only"]
        verbose = options["verbose"]
        output_file = options["output_file"]

        cases = _select_priority_cases(limit)

        if not cases:
            self.stdout.write(self.style.WARNING("No eligible CIAA draft cases found."))
            return

        self.stdout.write(
            f"Selected {len(cases)} high-priority case(s) "
            f"(limit={limit}, total eligible scanned)\n"
        )

        if ids_only:
            for cid, *_ in cases:
                self.stdout.write(cid)
        else:
            self.stdout.write(
                f"{'Rank':<5} {'Case ID':<22} {'Score':<8} {'Bigo':<14} "
                f"{'Defendants':<12} Title"
            )
            self.stdout.write("-" * 100)

            for i, (cid, title, score, bigo, acc) in enumerate(cases, 1):
                bigo_str = f"Rs. {bigo:,.0f}" if bigo else "—"
                title_short = (title or "")[:50]
                self.stdout.write(
                    f"{i:<5} {cid:<22} {score:<8.4f} {bigo_str:<14} "
                    f"{acc:<12} {title_short}"
                )

            if verbose:
                self.stdout.write("\nSelection criteria:")
                self.stdout.write(
                    "  Score = 0.55 × bigo(log) + 0.30 × complexity + 0.15 × richness"
                )
                self.stdout.write("  bigo:        log₁₀(bigo) / 12, capped at 1.0")
                self.stdout.write(
                    "  complexity:  defendants × 0.15 + total_entities × 0.05"
                )
                self.stdout.write("  richness:    court_cases (0.4) + notes (0.2)")

        if output_file:
            with open(output_file, "w") as f:
                for cid, *_ in cases:
                    f.write(f"{cid}\n")
            self.stdout.write(
                self.style.SUCCESS(f"\nWrote {len(cases)} case IDs to {output_file}")
            )
