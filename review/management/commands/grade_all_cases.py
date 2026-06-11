"""Submit + run a quality review for EVERY local case (VOL-3 22:22 directive).

After `seed_jawafdehi` has loaded all cases/sources/entities locally, this
command creates one CaseReview per case slug and runs the full pipeline
(likhit source->markdown + Bedrock rule judge) sequentially, with per-case
error isolation and progress logging. Each case's LLM rules already fan out
in parallel inside the pipeline, so wall-clock per case scales with the
Bedrock worker pool, while running cases one-at-a-time keeps memory + Bedrock
throttling bounded across hundreds of cases.

Designed to run as a long background job. It is resumable: by default it skips
slugs that already have a DONE review (unless --regrade), so if the process is
restarted it picks up where it left off.

Examples:
  # Grade every local case (skip ones already done), resumable
  python manage.py grade_all_cases

  # Force a fresh review for every case even if already graded
  python manage.py grade_all_cases --regrade

  # Just the first N (smoke test / timing)
  python manage.py grade_all_cases --limit 1
"""

import time

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand

from cases.models import Case
from review import pipeline
from review.models import CaseReview, ReviewConfig


class Command(BaseCommand):
    help = "Create + run a quality review for every local case (sequential, resilient)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Only grade the first N cases (by slug order).",
        )
        parser.add_argument(
            "--regrade",
            action="store_true",
            help="Re-review cases that already have a DONE review.",
        )
        parser.add_argument(
            "--state",
            default=None,
            help="Only grade cases in this state (e.g. PUBLISHED).",
        )
        parser.add_argument(
            "--submitted-by",
            default=None,
            help="Username to attribute reviews to (default: first superuser/any).",
        )

    def handle(self, *args, **opts):
        ReviewConfig.get_active()

        qs = Case.objects.all().order_by("slug")
        if opts["state"]:
            qs = qs.filter(state=opts["state"])
        slugs = list(qs.values_list("slug", flat=True))
        if opts["limit"]:
            slugs = slugs[: opts["limit"]]

        user = None
        if opts["submitted_by"]:
            user = User.objects.filter(username=opts["submitted_by"]).first()
        if user is None:
            user = (
                User.objects.filter(is_superuser=True).first() or User.objects.first()
            )

        total = len(slugs)
        self.stdout.write(
            self.style.MIGRATE_HEADING(
                f"Grading {total} case(s). regrade={opts['regrade']} submitted_by={user}"
            )
        )

        done = failed = skipped = 0
        t_start = time.monotonic()

        for i, slug in enumerate(slugs, 1):
            # Resumability: skip slugs that already have a DONE review.
            if not opts["regrade"]:
                if CaseReview.objects.filter(
                    slug=slug, status=CaseReview.STATUS_DONE
                ).exists():
                    skipped += 1
                    self.stdout.write(f"[{i}/{total}] skip (already done): {slug}")
                    continue

            review = CaseReview.objects.create(slug=slug, submitted_by=user)
            t0 = time.monotonic()
            try:
                pipeline.run_review(review.id)
            except Exception as e:  # noqa: BLE001 - run_review already records failures
                self.stdout.write(self.style.ERROR(f"    pipeline raised: {e}"))

            review.refresh_from_db()
            dt = time.monotonic() - t0
            res = review.result or {}
            if review.status == CaseReview.STATUS_DONE:
                done += 1
                self.stdout.write(
                    self.style.SUCCESS(
                        f"[{i}/{total}] DONE  {slug}  "
                        f"score={res.get('overall_score')} disp={res.get('disposition')} "
                        f"type={review.case_type} src={review.sources_converted}/{review.source_count} "
                        f"{dt:.0f}s (review #{review.id})"
                    )
                )
            else:
                failed += 1
                err = (review.error or "").splitlines()[0] if review.error else ""
                self.stdout.write(
                    self.style.ERROR(
                        f"[{i}/{total}] FAIL  {slug}  stage={review.stage} {dt:.0f}s — {err[:160]}"
                    )
                )

            elapsed = time.monotonic() - t_start
            graded = done + failed
            if graded:
                rate = elapsed / graded
                remaining = (total - i) * rate
                self.stdout.write(
                    f"    progress: done={done} failed={failed} skipped={skipped} "
                    f"| {elapsed/60:.1f}m elapsed, ~{remaining/60:.1f}m left"
                )

        self.stdout.write(
            self.style.SUCCESS(
                f"\nFINISHED: {done} done, {failed} failed, {skipped} skipped "
                f"of {total} in {(time.monotonic()-t_start)/60:.1f}m."
            )
        )
