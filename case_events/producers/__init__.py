# SPDX-License-Identifier: Hippocratic-3.0
"""Producers: the things that put observed facts on ``jaw.signal.>``.

A producer's whole job is to notice that something happened in the world and say
so. It does not decide which case the fact belongs to (that is the matcher), it
does not draft a change (that is the intent job), and it never writes a Case.

**Signals are emitted where the record LANDS, not where the scraper runs.** The
CIAA press-release and court-order scrapers are deliberately thin REST clients —
``scrape_ciaa_press_releases`` says outright that it "never touches the ORM" —
and hooking the bus into them would give every scraper a broker dependency, a
``NATS_URL``, and a second way to be half-configured. Both write Materials
through the ingestion plane, so one ``post_save`` producer at the Material seam
covers both, and covers anything else that later writes the same kinds of
document. This mirrors what :mod:`case_proposals.publish` does for proposals.

**Producers are stateless and re-emit freely.** There is no watermark table and
no checkpoint file. The docket producer rescans an overlapping window every run
and republishes facts it has already published; the deduplication that makes
that safe is the same spine every other layer relies on, ending at
``CaseUpdateProposal.dedup_key``, which is unique and permanent.

That is a deliberate trade and it has one sharp edge worth naming. The queue's
own dedup does NOT hold across a completed job — ``jobs.queue.enqueue`` frees a
``dedup_key`` once its job is terminal — so re-emission would have bought a fresh
premium model call every time, had ``case_events.consumers.handlers`` not checked
the proposal table before enqueueing. The stateless design and that check are one
decision, not two; do not remove either without the other.

What we get for it: no migration, no state to corrupt, and a dedup path that is
exercised continuously in production rather than only in tests. A watermark that
silently skips a range is a failure nobody notices for months.
"""

from __future__ import annotations

from typing import Any

import structlog

from case_events import bus
from case_events.envelope import build_envelope

logger = structlog.get_logger(__name__)


def emit(
    subject: str,
    *,
    producer: str,
    payload: dict[str, Any],
    subject_refs: list[str],
    dedup_key: str,
    source: str = "",
    raw_ref: str = "",
    occurred_at=None,
    wait: bool = False,
) -> bool:
    """Publish one signal. Never raises; returns False if nothing was sent.

    Best-effort in exactly the same sense as :mod:`case_proposals.publish`: a
    producer runs alongside work that matters more than the signal does — a
    scrape that populated the lake, a material that is now in the archive — and
    a broker outage must not cost that work.

    ``dedup_key`` is mandatory here rather than optional as it is on
    ``build_envelope``. A signal with no deterministic key defeats every dedup
    layer downstream, and since producers re-emit by design that is not a
    degraded message but a duplicate-proposal generator.

    Args:
        wait: Block for the JetStream ack rather than returning as soon as the
            publish is handed to the bus thread. **A False return only means
            anything when this is set.** Without it the return value says the
            coroutine was scheduled, and the broker's answer — including the
            rejection you get when no stream claims the subject, i.e. when
            ``nats_bootstrap`` has not been run — arrives later on a callback
            nobody reads. Any caller that reports a result to a human, or counts
            what it sent, wants this on. A ``post_save`` receiver does not: it
            has no one to tell and a request to keep short.
    """
    if not dedup_key:
        logger.warning("case_events.signal_without_dedup_key", subject=subject, producer=producer)
        return False

    envelope = build_envelope(
        subject=subject,
        producer=producer,
        payload=payload,
        subject_refs=subject_refs,
        dedup_key=dedup_key,
        source=source,
        raw_ref=raw_ref,
        occurred_at=occurred_at,
    )
    try:
        return bus.publish(subject, envelope, wait=wait)
    except Exception:  # noqa: BLE001 - a signal is never worth failing real work
        logger.warning("case_events.signal_publish_failed", subject=subject, dedup_key=dedup_key)
        return False
