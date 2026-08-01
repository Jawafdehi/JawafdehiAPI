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
            # quiet window unless it says so.
            self.stderr.write(
                self.style.WARNING(f"  {dropped} signal(s) were NOT accepted by the bus")
            )

        self._warn_if_saturated(counts, limit)

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
        self._warn_if_saturated(counts, limit)

    def _warn_if_saturated(self, counts, limit):
        """Say so when the limit truncated a kind.

        A capped run looks exactly like a complete one in the output, and the
        difference is whether facts were dropped this cycle.
        """
        for subject, n in sorted(counts.items()):
            if n >= limit:
                self.stderr.write(
                    self.style.WARNING(
                        f"  {subject} hit the --limit of {limit}; the window is truncated and "
                        "the remainder waits for the next run. Raise --limit or shorten the window."
                    )
                )
