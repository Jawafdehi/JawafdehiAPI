# SPDX-License-Identifier: Hippocratic-3.0
"""The pull loop: fetch, hand to a handler, ack or bury.

Structured so that the interesting part — what happens to one message — is a
function taking a message-like object and a JetStream context, and can be
exercised with fakes. The loop around it is deliberately dull.

**Handlers run in a worker thread.** They touch the Django ORM, which raises
``SynchronousOnlyOperation`` if called from inside a running event loop. Since
that also means a slow handler cannot block the loop, the fetch for one consumer
keeps its ack deadlines while another is mid-work.

**Consumers are created on subscribe, streams are not.** ``pull_subscribe`` with
a durable name upserts the consumer, which needs only ``$JS.API.CONSUMER.*`` —
not stream-admin rights. The streams themselves must already exist; assert them
with ``manage.py nats_bootstrap``. A subscribe against a missing stream fails
loudly here, which is the intended behaviour: a consumer with nothing to bind to
should not sit looking healthy.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import structlog
from django.conf import settings

from case_events import subjects
from case_events.consumers import ConsumerSpec, Disposition, PoisonMessage, decide
from case_events.envelope import build_envelope

logger = structlog.get_logger(__name__)

#: Seconds a fetch waits for messages before returning empty. Short enough that
#: a shutdown signal is noticed promptly, long enough not to spin.
FETCH_TIMEOUT_SECONDS = 5.0

#: Ceiling on the initial connect.
CONNECT_TIMEOUT_SECONDS = 10


class ConsumerStopped(Exception):
    """Raised internally to unwind a consumer loop on shutdown."""


async def connect():
    """Connect to the broker. Raises if it cannot.

    Unlike the publisher's connection, this one is bounded and fatal. A
    publisher that cannot reach the broker degrades enrichment; a consumer that
    cannot reach the broker has no reason to be running, and should exit so the
    orchestrator restarts it and the failure is visible as a CrashLoopBackOff
    rather than as silence.
    """
    import nats

    nc = await nats.connect(
        settings.NATS_URL,
        name="jawafdehi-consumers",
        connect_timeout=CONNECT_TIMEOUT_SECONDS,
        # Bounded, for the reason above.
        max_reconnect_attempts=10,
    )
    return nc, nc.jetstream()


async def subscribe(js, spec: ConsumerSpec):
    """Create-or-attach the durable pull consumer for ``spec``."""
    from nats.js.api import AckPolicy, ConsumerConfig

    from case_events.consumers import DEFAULT_BACKOFF_SECONDS

    return await js.pull_subscribe(
        spec.filter_subject,
        durable=spec.durable,
        stream=spec.stream,
        config=ConsumerConfig(
            durable_name=spec.durable,
            # Explicit acks are the whole basis of the retry model: a message is
            # only off the queue once a handler has said so.
            ack_policy=AckPolicy.EXPLICIT,
            ack_wait=spec.ack_wait_seconds,
            max_deliver=spec.max_deliver,
            backoff=list(DEFAULT_BACKOFF_SECONDS),
            filter_subject=spec.filter_subject,
        ),
    )


def parse_envelope(raw: bytes) -> dict:
    """Decode a message body into an envelope.

    Raises:
        PoisonMessage: on anything that is not a JSON object. Redelivering
            malformed bytes produces malformed bytes.
    """
    try:
        envelope = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PoisonMessage(f"message body is not valid UTF-8 JSON: {exc}") from None
    if not isinstance(envelope, dict):
        raise PoisonMessage(f"message body is a {type(envelope).__name__}, not an envelope object")
    return envelope


def _num_delivered(msg) -> int:
    """This message's delivery count, defaulting to 1 if unavailable.

    Defaulting LOW on purpose. If the metadata cannot be read, treating the
    message as a first delivery means it gets retried rather than buried — the
    recoverable mistake of the two.
    """
    try:
        return int(msg.metadata.num_delivered)
    except Exception:  # noqa: BLE001 - metadata is best-effort
        return 1


async def dead_letter(js, spec: ConsumerSpec, msg, error: BaseException, num_delivered: int) -> bool:
    """Republish a poison message to the DLQ. True if it was accepted.

    The original subject is appended to ``jaw.dlq.`` so the message stays
    attributable — a body that died on ``jaw.case.matched`` lands on
    ``jaw.dlq.jaw.case.matched`` and can be found without knowing which consumer
    gave up on it.

    The original body is carried as raw text rather than re-parsed, because the
    commonest reason to be here is that it could not be parsed.
    """
    original_subject = getattr(msg, "subject", "") or "unknown"
    try:
        body = msg.data.decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        body = "<undecodable>"

    envelope = build_envelope(
        subject=subjects.dlq_subject(original_subject),
        producer=f"consumer:{spec.name}",
        payload={
            "consumer": spec.name,
            "original_subject": original_subject,
            "num_delivered": num_delivered,
            "error": str(error) or type(error).__name__,
            "error_type": type(error).__name__,
            "body": body[:20_000],
        },
    )
    try:
        await js.publish(
            subjects.dlq_subject(original_subject),
            json.dumps(envelope, ensure_ascii=False, default=str).encode("utf-8"),
        )
        return True
    except Exception as exc:  # noqa: BLE001
        # Logged at error, not warning: this is the one failure that loses a
        # message outright, and it is the reason the caller does NOT terminate a
        # message whose DLQ publish failed.
        logger.error(
            "case_events.dlq_publish_failed",
            consumer=spec.name,
            original_subject=original_subject,
            error=str(exc),
        )
        return False


async def process_message(js, spec: ConsumerSpec, msg) -> Disposition:
    """Run one message through its handler and settle it.

    Returns the disposition actually applied, which is what the tests assert on.
    """
    error: BaseException | None = None
    envelope: dict[str, Any] = {}
    try:
        envelope = parse_envelope(msg.data)
        # to_thread, not a direct call: handlers use the Django ORM, which
        # refuses to run inside an event loop.
        await asyncio.to_thread(spec.handler, envelope, msg)
    except Exception as exc:  # noqa: BLE001 - the handler's failure is data here
        error = exc

    num_delivered = _num_delivered(msg)
    disposition = decide(error=error, num_delivered=num_delivered, max_deliver=spec.max_deliver)

    if disposition is Disposition.ACK:
        await msg.ack()
        return disposition

    logger.warning(
        "case_events.handler_failed",
        consumer=spec.name,
        subject=getattr(msg, "subject", ""),
        dedup_key=envelope.get("dedup_key"),
        num_delivered=num_delivered,
        disposition=disposition.value,
        error=str(error) or type(error).__name__,
        error_type=type(error).__name__,
        exc_info=not isinstance(error, PoisonMessage),
    )

    if disposition is Disposition.RETRY:
        # No delay argument: the consumer's own `backoff` schedule governs when
        # it comes back, so passing one here would override the policy the
        # subscription was created with.
        await msg.nak()
        return disposition

    if await dead_letter(js, spec, msg, error, num_delivered):
        # Terminate ONLY once the DLQ has the message. Terminating first and
        # failing to republish would be a silent loss — exactly what the DLQ
        # exists to prevent.
        await msg.term()
        return Disposition.DEAD_LETTER

    # The DLQ is unreachable. NAK instead, so the message stays on the stream
    # and someone can deal with it. It may exceed max_deliver and be dropped by
    # JetStream, but an un-acked message with a loud error beats a terminated one.
    await msg.nak()
    return Disposition.RETRY


async def run_one(js, spec: ConsumerSpec, *, stop: asyncio.Event, once: bool, max_messages: int | None):
    """Fetch-and-process loop for a single consumer. Returns messages handled."""
    from nats.errors import TimeoutError as NatsTimeoutError

    sub = await subscribe(js, spec)
    logger.info(
        "case_events.consumer_started",
        consumer=spec.name,
        stream=spec.stream,
        filter=spec.filter_subject,
        durable=spec.durable,
    )

    handled = 0
    while not stop.is_set():
        if max_messages is not None and handled >= max_messages:
            break
        batch = spec.batch
        if max_messages is not None:
            batch = min(batch, max_messages - handled)
        try:
            msgs = await sub.fetch(batch, timeout=FETCH_TIMEOUT_SECONDS)
        except (NatsTimeoutError, asyncio.TimeoutError):
            # An empty window. In --once mode that is the signal that the
            # backlog is drained; otherwise just poll again.
            if once:
                break
            continue
        except Exception as exc:  # noqa: BLE001
            logger.warning("case_events.fetch_failed", consumer=spec.name, error=str(exc))
            if once:
                break
            await asyncio.sleep(1)
            continue

        for msg in msgs:
            await process_message(js, spec, msg)
            handled += 1

    logger.info("case_events.consumer_stopped", consumer=spec.name, handled=handled)
    return handled


async def run(specs: list[ConsumerSpec], *, once: bool = False, max_messages: int | None = None) -> dict[str, int]:
    """Run every spec concurrently until stopped. Returns per-consumer counts.

    One connection is shared by all of them; each gets its own subscription and
    its own task. A consumer whose loop raises takes only itself down — recorded
    as an exception in the returned mapping's place, logged, and left for the
    orchestrator to notice via the process exiting when all of them have.
    """
    nc, js = await connect()
    stop = asyncio.Event()

    _install_signal_handlers(stop)

    try:
        results = await asyncio.gather(
            *(run_one(js, spec, stop=stop, once=once, max_messages=max_messages) for spec in specs),
            return_exceptions=True,
        )
    finally:
        # Drain rather than close: in-flight acks should reach the server.
        try:
            await nc.drain()
        except Exception as exc:  # noqa: BLE001 - shutdown is best-effort
            logger.warning("case_events.drain_failed", error=str(exc))

    counts = {}
    for spec, result in zip(specs, results):
        if isinstance(result, BaseException):
            logger.error("case_events.consumer_crashed", consumer=spec.name, error=str(result))
            counts[spec.name] = -1
        else:
            counts[spec.name] = result
    return counts


def _install_signal_handlers(stop: asyncio.Event) -> None:
    """Stop cleanly on SIGTERM/SIGINT so in-flight messages get acked.

    Without this, a rollout's SIGTERM kills the process mid-handler and every
    un-acked message is redelivered — correct, since delivery is at-least-once,
    but it burns a delivery attempt on every message in flight at every deploy.
    """
    import signal

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            # Not available on every platform, and not available at all when the
            # loop is not running in the main thread (which is the case under
            # pytest). Losing graceful shutdown there costs nothing.
            pass
