# SPDX-License-Identifier: Hippocratic-3.0
"""The message envelope every event on the bus carries.

One shape for signals and case events alike, so a consumer can read provenance,
identity and timing without knowing the subject. The type-specific part is
confined to ``payload``.

Two fields carry more weight than they look:

``subject_refs`` are stable ``@id`` IRIs (case, court-case, NES entity,
material) and are **the join key** between a message and our records. They must
be built with :mod:`jawafdehi_shared.entities.ids`, never formatted by hand:
``build_courtcase_iri`` lowercases both segments, so a hand-rolled
``.../courtcase/special/082-CR-0154`` matches nothing.

``dedup_key`` is sent as the ``Nats-Msg-Id`` header, which is what makes
JetStream drop a duplicate publish inside its dedup window. It is the same
idempotency spine ``CaseUpdateProposal.dedup_key`` uses, and producers must
construct it deterministically from the fact — not from a timestamp or a random
id, or it defeats itself.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def utcnow() -> datetime:
    """Timezone-aware UTC now. Seam for tests to freeze."""
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    """RFC 3339 / ISO 8601 in UTC, always with a ``Z`` suffix.

    ``datetime.isoformat()`` renders UTC as ``+00:00``; normalising to ``Z``
    keeps the wire format stable for non-Python consumers.
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def build_envelope(
    *,
    subject: str,
    payload: dict[str, Any],
    producer: str,
    subject_refs: list[str] | None = None,
    dedup_key: str = "",
    source: str = "",
    raw_ref: str = "",
    occurred_at: datetime | None = None,
) -> dict[str, Any]:
    """Assemble one bus message.

    Args:
        subject: The subject it will be published on (see :mod:`events.subjects`).
            Carried in the body as well as the NATS subject so a message stays
            self-describing once it has been archived, DLQ'd, or re-published
            under a different subject.
        payload: Type-specific body. Must be JSON-serialisable.
        producer: What emitted this — ``"platform"`` for the monolith,
            ``"consumer:<name>"`` for a consumer, ``"producer:<name>"`` for a
            scraper.
        subject_refs: Stable ``@id`` IRIs this message is about.
        dedup_key: Deterministic idempotency key; becomes ``Nats-Msg-Id``.
        source: Where the underlying fact came from (URL, ``@id``, or a
            well-known token like ``"caseworker"``).
        raw_ref: Pointer to the raw artefact behind the fact, when there is one.
        occurred_at: When the fact happened. Defaults to now, but should be
            passed whenever the real time is known — for a scraped hearing that
            is the docket date, not the moment we noticed it.

    Returns:
        A JSON-serialisable dict.
    """
    now = utcnow()
    return {
        "subject": subject,
        "producer": producer,
        "subject_refs": list(subject_refs or []),
        "dedup_key": dedup_key,
        "source": source,
        "raw_ref": raw_ref,
        # occurred_at is when the FACT happened; published_at is when we emitted
        # it. They differ by however long the producer took to notice, which is
        # the number you need when auditing lag.
        "occurred_at": _iso(occurred_at or now),
        "published_at": _iso(now),
        "payload": payload,
    }
