# SPDX-License-Identifier: Hippocratic-3.0
"""Run the bus consumers.

    manage.py run_consumers                          # READ-ONLY: print topology, exit
    manage.py run_consumers --apply                  # run all four, forever
    manage.py run_consumers --apply --only matcher   # run one (the split-later seam)
    manage.py run_consumers --apply --once           # drain the backlog, exit

Read-only by default, matching ``scrape_worker`` and ``review_poller``: the bare
command tells you what WOULD run, and needs neither a broker nor credentials to
do it. That is what makes it useful for checking a Deployment's args before the
Deployment exists.

The four run as four subscriptions in one process. ``--only`` is here from the
first commit so that splitting them into separate Deployments later is a
manifest change rather than a rewrite; see ``case_events.consumers.select``.
"""

from __future__ import annotations

import asyncio

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from case_events import consumers
from case_events.consumers import runner


class Command(BaseCommand):
    help = "Run the durable pull consumers for the case-enrichment bus."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help=(
                "Actually subscribe and process messages. Without it the command "
                "prints the consumer topology and exits."
            ),
        )
        parser.add_argument(
            "--only",
            nargs="+",
            metavar="NAME",
            help=(
                "Run only these consumers (default: all). An unknown name is an "
                f"error, not a silent skip. Known: {', '.join(consumers.known())}."
            ),
        )
        parser.add_argument(
            "--once",
            action="store_true",
            help="With --apply: drain what is currently available, then exit.",
        )
        parser.add_argument(
            "--max-messages",
            type=int,
            default=None,
            help="Handle at most this many messages per consumer, then exit.",
        )

    def handle(self, *args, **options):
        try:
            specs = consumers.select(options.get("only"))
        except KeyError as exc:
            # KeyError's str() is the repr of its argument, which would print the
            # whole message wrapped in quotes.
            raise CommandError(exc.args[0]) from None

        if not specs:
            raise CommandError(
                "No consumers are registered. That usually means "
                "case_events.consumers.handlers failed to import — check the log "
                "for case_events.consumer_registration_failed."
            )

        if not options["apply"]:
            self._report(specs)
            return

        if not getattr(settings, "NATS_URL", ""):
            raise CommandError(
                "NATS_URL is not set, so there is no broker to consume from. "
                "Unlike publishing, consuming has no meaningful no-op: a consumer "
                "with no connection would idle while looking healthy."
            )

        self.stdout.write(
            self.style.MIGRATE_HEADING(
                f"run_consumers: {len(specs)} consumer(s) — {', '.join(s.name for s in specs)}"
            )
        )

        counts = asyncio.run(
            runner.run(
                specs,
                once=options["once"],
                max_messages=options["max_messages"],
            )
        )

        crashed = [name for name, n in counts.items() if n < 0]
        for name, handled in sorted(counts.items()):
            if handled < 0:
                self.stderr.write(self.style.ERROR(f"  {name}: crashed (see log)"))
            else:
                self.stdout.write(f"  {name}: handled {handled}")
        if crashed:
            # A non-zero exit so an orchestrator restarts the pod rather than
            # treating a half-dead consumer set as a clean run.
            raise CommandError(f"consumer(s) crashed: {', '.join(sorted(crashed))}")

    def _report(self, specs):
        self.stdout.write(self.style.MIGRATE_HEADING("Consumers (read-only; use --apply to run)"))
        for spec in specs:
            self.stdout.write(f"  {spec.name}")
            self.stdout.write(f"    durable        {spec.durable}")
            self.stdout.write(f"    stream         {spec.stream}")
            self.stdout.write(f"    filter         {spec.filter_subject}")
            self.stdout.write(f"    max_deliver    {spec.max_deliver} (then -> {spec.dlq_hint})")
            self.stdout.write(f"    ack_wait       {spec.ack_wait_seconds}s")
            if spec.description:
                self.stdout.write(f"    {spec.description}")
        self.stdout.write("")
        self.stdout.write(
            "Streams must already exist: run `manage.py nats_bootstrap` first. "
            "A pull subscribe against a missing stream fails."
        )
