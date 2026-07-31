# SPDX-License-Identifier: Hippocratic-3.0
"""Assert the JetStream stream topology.

Run once per deploy, in the same place ``manage.py migrate`` is run, and
**before anything publishes**. JetStream rejects a publish to a subject that no
stream claims, so a fresh broker without this step drops every event.

Publishers deliberately do not do this themselves: it would require giving every
web process stream-CREATE rights on the broker. See :mod:`case_events.streams`.
"""

from __future__ import annotations

import asyncio

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from case_events import bus
from case_events.streams import STREAMS, ensure_streams

#: Bounded, unlike the publisher's. A publisher reconnects forever because it
#: outlives broker restarts; a command that hangs instead of failing would just
#: wedge a deploy.
CONNECT_ATTEMPTS = 3


class Command(BaseCommand):
    help = "Create or update the JetStream streams the case-enrichment bus needs."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print the streams that would be asserted and exit without connecting.",
        )

    def handle(self, *args, **options):
        if options["dry_run"]:
            for spec in STREAMS:
                self.stdout.write(
                    f"{spec.name}: subjects={list(spec.subjects)} "
                    f"replicas={spec.replicas} max_age={spec.max_age_seconds}s"
                )
            return

        if not bus.enabled():
            raise CommandError(
                "NATS_URL is not set, so there is no broker to bootstrap. This is "
                "expected in dev and CI — the bus is optional and every publish is "
                "a no-op without it."
            )

        try:
            asserted = asyncio.run(_bootstrap())
        except Exception as exc:
            # Loud on purpose. Unlike publishing, this is not best-effort: a
            # silent failure here means every later publish is dropped by a
            # broker with nowhere to put it.
            raise CommandError(f"Could not assert streams: {type(exc).__name__}: {exc}") from exc

        self.stdout.write(self.style.SUCCESS(f"Asserted {len(asserted)} streams: {asserted}"))


async def _bootstrap() -> list[str]:
    import nats

    nc = await nats.connect(
        settings.NATS_URL,
        name="jawafdehi-nats-bootstrap",
        connect_timeout=bus.STARTUP_TIMEOUT_SECONDS,
        max_reconnect_attempts=CONNECT_ATTEMPTS,
    )
    try:
        return await ensure_streams(nc.jetstream())
    finally:
        await nc.drain()
