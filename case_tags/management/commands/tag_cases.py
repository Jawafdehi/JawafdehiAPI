"""Enqueue tagging jobs, one per case.

Enqueue-only by design. The command does not call the model: the premium tier resolves to
a CLI provider that lives on the worker, and a management command that blocked for 30–90
seconds per case across 82 cases would be a two-hour foreground process with no lease, no
retry and no visibility. Queueing makes each case independently retryable and observable
through ``/api/jobs``.

Which means: nothing happens until a worker is polling. That is a real operational
dependency, so the command says so rather than exiting silently.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand

from jobs.queue import enqueue

from case_tags.job_kind import KIND


class Command(BaseCommand):
    help = "Queue a tagging job for each published case."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--slug", action="append", default=[], help="Limit to these cases.")
        parser.add_argument("--limit", type=int, default=0, help="Queue at most N (0 = all).")
        parser.add_argument(
            "--retag",
            action="store_true",
            help="Include cases that already carry vocabulary tags (default skips them).",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        from cases.models import Case  # noqa: PLC0415

        from case_tags.resolve import TagResolver  # noqa: PLC0415

        qs = Case.objects.filter(state="PUBLISHED").order_by("slug")
        if options["slug"]:
            qs = qs.filter(slug__in=options["slug"])

        resolver = TagResolver()
        queued = skipped = 0
        for case in qs:
            if not options["retag"]:
                # Already-tagged means "carries at least one value this vocabulary
                # recognises". A case holding only unresolved geography still needs a run.
                if resolver.resolve_all([t for t in (case.tags or []) if isinstance(t, str)]):
                    skipped += 1
                    continue
            # dedup_key so re-running the command does not stack duplicate jobs for the
            # same case — enqueue() returns the existing non-terminal job instead.
            enqueue(
                KIND,
                payload={"case_slug": case.slug},
                dedup_key=f"{KIND}:{case.slug}",
            )
            queued += 1
            if options["limit"] and queued >= options["limit"]:
                break

        self.stdout.write(self.style.SUCCESS(f"Queued {queued}, skipped {skipped}."))
        if queued:
            self.stdout.write(
                self.style.NOTICE(
                    "Nothing runs until a worker claims these. Check /api/jobs for "
                    "progress, and job.result.tagging for what each run applied or "
                    "refused."
                )
            )
