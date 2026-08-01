# SPDX-License-Identifier: Hippocratic-3.0
"""Publish docket signals from the NGM lake.

    manage.py emit_docket_signals                    # READ-ONLY: count what would go
    manage.py emit_docket_signals --apply            # publish the window
    manage.py emit_docket_signals --window-hours 6   # narrow it
    manage.py emit_docket_signals --show 5           # print sample envelopes

Read-only by default, like ``scrape_worker`` and ``review_poller``. The bare
command reads the lake and reports; it needs no broker, which makes it the way to
size the window before a cron ever runs.

Runs as a CronJob in the platform image (it needs the ngm database). The window
must comfortably exceed the schedule — see ``case_events.producers.dockets``.
"""

from __future__ import annotations

import json

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from case_events.producers import dockets


class Command(BaseCommand):
    help = "Emit jaw.signal.docket.* for recent lake activity (read-only without --apply)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Actually publish. Without it the command only counts and reports.",
        )
        parser.add_argument(
            "--window-hours",
            type=int,
            default=dockets.DEFAULT_WINDOW_HOURS,
            help=(
                "How far back to rescan. Overlap is intentional and free (the dedup "
                f"spine drops repeats); a gap is a permanently missed fact. Default {dockets.DEFAULT_WINDOW_HOURS}."
            ),
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=dockets.DEFAULT_LIMIT,
            help=f"Cap on rows examined per kind (default {dockets.DEFAULT_LIMIT}).",
        )
        parser.add_argument(
            "--show",
            type=int,
            default=0,
            metavar="N",
            help="Print the first N signals as JSON. Works without --apply.",
        )

    def handle(self, *args, **options):
        window = options["window_hours"]
        limit = options["limit"]

        if window <= 0:
            raise CommandError("--window-hours must be positive; a zero window emits nothing.")

        if not options["apply"]:
            self._report(window, limit, options["show"])
            return

        if not getattr(settings, "NATS_URL", ""):
            # Publishing already no-ops without a broker, but silently: the
            # command would report zero sent and look like an empty window.
            raise CommandError(
                "NATS_URL is not set, so --apply would publish nothing and report it "
                "as an empty window. Run without --apply to inspect the window."
            )

        counts = dockets.publish_window(window, limit)
        total = sum(counts.values())
        self.stdout.write(
            self.style.MIGRATE_HEADING(f"emit_docket_signals: {total} signal(s) over {window}h")
        )
        for subject, n in sorted(counts.items()):
            self.stdout.write(f"  {subject}: {n}")

        dropped = sum(n for s, n in counts.items() if "not sent" in s)
        if dropped:
            # Never silent. A run that published nothing looks identical to a
            # quiet window unless it says so. Meaningful only because
            # publish_window waits for the JetStream ack.
            self.stderr.write(
                self.style.WARNING(f"  {dropped} signal(s) were NOT accepted by the bus")
            )

        missed = self._report_saturation(window, limit)

        # Non-zero exit, deliberately, AFTER the publishing is done. The run did
        # useful work and the signals it sent are on the bus — but facts inside
        # the window were never emitted and never will be, so a green CronJob
        # would be a lie. backoffLimit is 0, so this surfaces as a failed Job
        # rather than a retry storm.
        if missed:
            raise CommandError(
                f"{missed} fact(s) in the {window}h window were never emitted because --limit "
                f"({limit}) truncated the scan. They are NOT deferred — the next run rescans "
                "the same window in the same order and reaches the same rows. Re-run with a "
                "larger --limit before they age out of the window."
            )
        if dropped:
            raise CommandError(f"{dropped} signal(s) were refused by the bus.")

    def _report(self, window, limit, show):
        signals = list(dockets.scan(window, limit))
        counts: dict[str, int] = {}
        for subject, *_ in signals:
            counts[subject] = counts.get(subject, 0) + 1

        self.stdout.write(
            self.style.MIGRATE_HEADING(
                f"emit_docket_signals (read-only): {len(signals)} signal(s) over {window}h"
            )
        )
        for subject, n in sorted(counts.items()):
            self.stdout.write(f"  {subject}: {n}")
        if not signals:
            self.stdout.write("  (nothing in the window)")

        for subject, payload, refs, dedup_key, occurred_at in signals[:show]:
            self.stdout.write("")
            self.stdout.write(
                json.dumps(
                    {
                        "subject": subject,
                        "subject_refs": refs,
                        "dedup_key": dedup_key,
                        "occurred_at": str(occurred_at),
                        "payload": payload,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )

        self.stdout.write("")
        self.stdout.write("Use --apply to publish. Streams must exist first (manage.py nats_bootstrap).")
        self._report_saturation(window, limit)

    def _report_saturation(self, window, limit) -> int:
        """Report any kind the limit truncated. Returns facts never emitted.

        Counted against the real row totals rather than inferred from what came
        back, so the message can say "5000 of 8231" — and an earlier version of
        this said the remainder "waits for the next run", which was wrong in the
        way that matters. There is no watermark: the next run rescans the same
        window in the same ``created_at`` order and re-selects the same first
        ``limit`` rows. The tail is never reached, and when the burst ages out of
        the window it is gone for good.
        """
        missed = 0
        for subject, total in sorted(dockets.window_totals(window).items()):
            if total <= limit:
                continue
            missed += total - limit
            self.stderr.write(
                self.style.ERROR(
                    f"  {subject}: emitted {limit} of {total}. The other {total - limit} are "
                    "NOT deferred — with no watermark the next run reaches the same rows, so "
                    "these are lost when they age out of the window. Raise --limit."
                )
            )
        return missed
