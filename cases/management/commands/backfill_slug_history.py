"""``backfill_slug_history`` — retroactively populate CaseSlugHistory (BB-38).

The slug-redirect feature only started recording retired slugs going forward,
so cases that were re-slugged BEFORE it shipped have orphaned old URLs that
still hard-404. Those pre-existing retired slugs survive in one place: the
``CaseReview`` table, whose ``slug`` column snapshots the slug a case carried
when it was reviewed. This command reads each review's snapshot slug, and for
any that no longer addresses a live case, resolves it back to its current case
and records ``old_slug → case`` in CaseSlugHistory so the stale URL 301-redirects.

Resolution mirrors ``review.migrations.0004``'s four tiers (in order):
  1. a direct ``case`` FK on the review, if the review model has one (post the
     review-key-on-case-id migration — then no fuzzy resolution is needed);
  2. a court-case reference number (``NNN-XX-NNN[N]``) embedded in the slug,
     matched against ``courtcase_references__courtcase_iri`` (unique hit only);
  3. the review's snapshot ``case_title`` matched exactly against a live title;
  4. a unique ``slug__istartswith`` prefix (re-slugs usually append a suffix).

Read-only by default (lists what WOULD be recorded). Pass ``--apply`` to write.
Idempotent: ``CaseSlugHistory.record`` upserts, so re-running is safe. Live
slugs are never recorded (a live slug must resolve to its own case, not
redirect), and rows shadowing a live slug are dropped by ``record`` itself.
"""

from __future__ import annotations

import re

from django.core.management.base import BaseCommand
from django.db import transaction

from cases.models import Case, CaseSlugHistory

COURT_REF_RE = re.compile(r"(\d{3}-[a-z]{2}-\d{3,4})")


class Command(BaseCommand):
    help = (
        "Backfill CaseSlugHistory from CaseReview snapshot slugs so pre-feature "
        "retired slugs 301-redirect (dry-run unless --apply)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Write the history rows. Without this flag, only lists what "
            "would be recorded.",
        )

    def handle(self, *args, **options):
        apply = options["apply"]

        # Import lazily so the command still loads if the review app is absent
        # (it lives in the same monolith today, but keep the coupling soft).
        from review.models import CaseReview

        live_slugs = set(Case.objects.values_list("slug", flat=True))

        # old_slug -> Case (deduped; a slug maps to exactly one former owner).
        candidates: dict[str, Case] = {}
        skipped_live = 0
        unresolved: list[str] = []

        for review in CaseReview.objects.all():
            old_slug = (getattr(review, "slug", "") or "").strip()
            if not old_slug:
                continue
            if old_slug in live_slugs:
                # The review's slug still addresses a live case — not outdated.
                skipped_live += 1
                continue

            case = self._resolve(review, old_slug)
            if case is None:
                unresolved.append(old_slug)
                continue
            # Never record a case's own current slug as a predecessor, and skip
            # the pathological empty-slug case.
            if not case.slug or case.slug == old_slug:
                continue
            candidates[old_slug] = case

        existing = set(CaseSlugHistory.objects.values_list("slug", flat=True))
        new_rows = {s: c for s, c in candidates.items() if s not in existing}

        mode = "APPLY" if apply else "dry-run"
        self.stdout.write(
            f"CaseReview rows: {CaseReview.objects.count()} | "
            f"skipped (slug still live): {skipped_live} | "
            f"unresolved: {len(unresolved)} | "
            f"resolvable outdated slugs: {len(candidates)} | "
            f"already recorded: {len(candidates) - len(new_rows)} | "
            f"to record: {len(new_rows)} ({mode})"
        )
        for old_slug, case in sorted(new_rows.items()):
            self.stdout.write(f"  {old_slug} -> {case.slug} (case id={case.pk})")
        if unresolved:
            self.stdout.write(
                self.style.WARNING(
                    f"Unresolved (left as 404): {sorted(unresolved)[:20]}"
                )
            )

        if not apply:
            if new_rows:
                self.stdout.write("Re-run with --apply to record these rows.")
            return

        with transaction.atomic():
            for old_slug, case in candidates.items():
                # ``record`` no-ops on old==new, drops any row shadowing the
                # live slug, then upserts old_slug -> case.
                CaseSlugHistory.record(case, old_slug=old_slug, new_slug=case.slug)

        # Safety assertion: the table must never shadow a live slug.
        shadow = CaseSlugHistory.objects.filter(slug__in=live_slugs).count()
        self.stdout.write(
            self.style.SUCCESS(
                f"Recorded {len(candidates)} retired slug(s); "
                f"rows shadowing a live slug: {shadow} (must be 0)."
            )
        )

    def _resolve(self, review, old_slug: str) -> Case | None:
        """Resolve a retired slug back to its current Case (four tiers)."""
        # Tier 1: a direct FK on the review model (post review-key-on-case-id).
        case = getattr(review, "case", None)
        if isinstance(case, Case):
            return case

        # Tier 2: a court-case reference number embedded in the slug.
        match = COURT_REF_RE.search(old_slug.lower())
        if match:
            qs = Case.objects.filter(
                courtcase_references__courtcase_iri__icontains=match.group(1)
            ).distinct()
            if qs.count() == 1:
                return qs.first()

        # Tier 3: the review's snapshot title, matched exactly.
        title = (getattr(review, "case_title", "") or "").strip()
        if title:
            qs = Case.objects.filter(title=title)
            if qs.count() == 1:
                return qs.first()

        # Tier 4: a unique live slug that this retired slug is a prefix of.
        qs = Case.objects.filter(slug__istartswith=old_slug.lower())
        if qs.count() == 1:
            return qs.first()

        return None
