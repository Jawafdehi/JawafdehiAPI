# SPDX-License-Identifier: Hippocratic-3.0
"""Durable pull consumers: the registry, and the rules for what to do with a message.

Everything in this module is pure and synchronous on purpose. The parts that
talk to a broker live in :mod:`case_events.consumers.runner`, and the domain
work lives in :mod:`case_events.consumers.handlers`; what is here is the small
set of decisions that decide whether a message is acked, retried, or buried —
the part most worth testing, and the part that would otherwise only be testable
against a live NATS.

**Pull, not push.** A durable consumer with N pull subscribers *is* the queue
group, so scaling is a replica count and flow control belongs to the worker
rather than the broker. Nothing here delivers itself work it is not ready for.

**At-least-once, with two layers of dedup.** JetStream collapses accidental
publish retries inside its own window via ``Nats-Msg-Id``; the real
"have we already recorded this docket hearing?" check is business-level, at the
proposal store, on ``dedup_key`` — because our genuine duplicates arrive days
apart, long past any broker window. A handler must therefore be idempotent, and
the retry rules below assume it is.

**Nothing is dropped silently.** A message that exhausts its delivery budget, or
that no number of redeliveries could fix, is republished to ``jaw.dlq.<original
subject>`` before it is terminated. The DLQ is for human triage; an empty one is
information, and a silently discarded message is not.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable

import structlog

from case_events import subjects

logger = structlog.get_logger(__name__)

#: How many times JetStream may deliver a message before we bury it. Five is a
#: compromise: enough to ride out a rolling restart of whatever the handler
#: depends on, few enough that a genuinely poisonous message reaches the DLQ the
#: same day rather than being retried into the following week.
DEFAULT_MAX_DELIVER = 5

#: Seconds before an un-acked message is redelivered. Handlers here are meant to
#: be short — the one long thing, intent generation, is deliberately a job — so
#: a minute is generous rather than tight.
DEFAULT_ACK_WAIT_SECONDS = 60

#: Redelivery backoff, in seconds, indexed by delivery attempt. Handed to
#: JetStream as the consumer's ``backoff``, so the broker does the waiting and a
#: retrying message costs the worker nothing while it waits.
DEFAULT_BACKOFF_SECONDS = (5, 30, 120, 600)

#: Messages fetched per pull. Small: a batch is processed serially, and a large
#: one just means the tail of it sits closer to its ack deadline.
DEFAULT_BATCH = 10


class PoisonMessage(Exception):
    """The message is unprocessable and redelivery cannot help.

    Raise this — rather than letting an ordinary exception escape — when the
    problem is the message itself: an unparseable body, a subject the handler
    does not understand, a reference to something that has been deleted. It
    routes straight to the DLQ instead of burning four more deliveries to reach
    the same conclusion.
    """


class Disposition(str, Enum):
    """What the runner does with a message once its handler has returned."""

    ACK = "ack"
    RETRY = "retry"
    DEAD_LETTER = "dead_letter"


def decide(*, error: BaseException | None, num_delivered: int, max_deliver: int) -> Disposition:
    """Choose a disposition for one delivery.

    Args:
        error: What the handler raised, or None if it returned cleanly.
        num_delivered: JetStream's delivery count for this message, 1-based on
            the first delivery.
        max_deliver: The consumer's configured delivery budget.

    Returns:
        The disposition.

    The case that matters is the last one. JetStream stops redelivering once
    ``num_delivered`` reaches ``max_deliver``, and what it does then is emit an
    advisory nobody here is listening to — so a message NAK'd on its final
    delivery is simply gone. Detecting the last delivery ourselves, and burying
    it deliberately, is the difference between a dead-letter queue and a leak.
    """
    if error is None:
        return Disposition.ACK
    if isinstance(error, PoisonMessage):
        return Disposition.DEAD_LETTER
    if num_delivered >= max_deliver:
        return Disposition.DEAD_LETTER
    return Disposition.RETRY


@dataclass(frozen=True)
class ConsumerSpec:
    """One durable consumer.

    Args:
        name: The durable name. Stable — JetStream tracks delivery state against
            it, so renaming one starts it over from the stream's beginning.
        stream: The stream it binds to (see :mod:`case_events.streams`).
        filter_subject: The subject filter. Must be covered by the stream's own
            subjects or the consumer will never see anything.
        handler: ``handler(envelope, context) -> None``. Synchronous, and run in
            a worker thread — these touch the Django ORM, which cannot be called
            from inside the event loop. Raise :class:`PoisonMessage` for an
            unprocessable message; raise anything else to retry.
        description: One line, shown by ``run_consumers`` in read-only mode.
    """

    name: str
    stream: str
    filter_subject: str
    handler: Callable[..., None]
    description: str = ""
    max_deliver: int = DEFAULT_MAX_DELIVER
    ack_wait_seconds: int = DEFAULT_ACK_WAIT_SECONDS
    batch: int = DEFAULT_BATCH

    @property
    def durable(self) -> str:
        """The durable name JetStream stores state under.

        Prefixed so that the consumers of this application are distinguishable
        from anything else that ever binds to these streams, and so a stray
        ``nats consumer ls`` reads as ours.
        """
        return f"jaw-{self.name}"

    @property
    def dlq_hint(self) -> str:
        """Where this consumer's poison messages land, as a wildcard.

        Approximate by construction: the real subject appends the message's own
        subject, so a consumer filtering a wildcard buries to several. Shown by
        ``run_consumers`` so the DLQ is greppable before anything has failed.
        """
        return f"{subjects.DLQ_PREFIX}{self.filter_subject}"


_REGISTRY: dict[str, ConsumerSpec] = {}


def register(spec: ConsumerSpec) -> ConsumerSpec:
    """Register (or replace) ``spec`` under its name. Idempotent."""
    _REGISTRY[spec.name] = spec
    return spec


def get(name: str) -> ConsumerSpec:
    """Return the spec registered as ``name``.

    Raises:
        KeyError: naming what is registered, because the usual way to reach this
            is a typo in ``--only``.
    """
    try:
        return _REGISTRY[name]
    except KeyError:
        raise KeyError(f"No consumer named {name!r}. Registered: {known()}.") from None


def known() -> list[str]:
    """Every registered consumer name, sorted."""
    return sorted(_REGISTRY)


def all_specs() -> list[ConsumerSpec]:
    """Every registered spec, ordered by name."""
    return [_REGISTRY[name] for name in known()]


def select(only: list[str] | None = None) -> list[ConsumerSpec]:
    """The specs to run: all of them, or just ``only``.

    ``--only`` exists from the first commit for a structural reason. These four
    run as four subscriptions inside ONE deployment — one manifest, one log
    stream, one rollout, and Django's import cost paid once instead of four
    times. The price is a shared failure domain and no per-consumer scaling.
    Both are acceptable now and both are things we may want back later, so the
    flag is what keeps that reversal a manifest change rather than a rewrite.

    Raises:
        KeyError: if any name is not registered. Refusing the whole set rather
            than silently running the subset that resolved — a typo in a
            Deployment's args should fail the rollout, not quietly leave a
            consumer unstarted for a fortnight.
    """
    if not only:
        return all_specs()
    unknown = [name for name in only if name not in _REGISTRY]
    if unknown:
        raise KeyError(f"Unknown consumer(s) {sorted(unknown)}. Registered: {known()}.")
    # Registration order, not argument order, so two Deployments passing the
    # same names in a different order behave identically.
    return [spec for spec in all_specs() if spec.name in set(only)]


# Importing the handlers registers the four consumers. Guarded for the same
# reason jobs.registry guards its own consumer import: a broken handler module
# should not make the registry itself unimportable, which would take down the
# management command that reports the problem.
try:  # pragma: no cover - defensive import wiring
    from . import handlers  # noqa: F401,E402
except Exception:  # noqa: BLE001
    logger.exception("case_events.consumer_registration_failed")
