# SPDX-License-Identifier: Hippocratic-3.0
"""JetStream stream topology, asserted from code rather than declared in YAML.

NATS has no CRD and no declarative stream config in the server file, so the
topology has to be created by *something*. Keeping the definition here — beside
the code that depends on it, in a form that re-applies idempotently — beats a
hand-run ``nats stream add`` that drifts the moment someone edits a stream.

**Who calls this matters, and it is not the publisher.** This was originally
asserted from :meth:`case_events.bus._Bus._connect`, so every process that
published also created streams. That hands broker-admin authority to a web
process whose only job is to emit a message, and it defeats the per-identity
NATS users the deployment sets up — whose whole point is that a compromised
publisher cannot reconfigure the bus.

So it is invoked by ``manage.py nats_bootstrap``, run once per deploy in the
same place migrations already are. A publisher's NATS user then needs nothing
beyond publish permission on its own subjects.

The cost of that split, stated plainly: **the streams must exist before the
first publish.** JetStream rejects a publish to a subject no stream claims, so
running the bootstrap is not optional — a fresh broker without it drops every
event, visible only as a publish failure in the logs.

``add_stream`` is upsert-like: creating a stream that already exists with the
same config is a no-op, so the command is safe to re-run on every deploy.

**Replicas are 1 for the pilot, deliberately.** With ``local-path`` storage the
pod is pinned to one node, so that node's disk *is* the bus. Going to R3 is not
a number change here — it needs three pinned nodes with three PVCs, i.e. a
re-deploy. That is tolerable only because nothing here is a system of record:
``SIGNALS`` is re-derivable from the NGM lake and the CIAA source, and committed
updates live in the case record.
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog

from case_events import subjects

logger = structlog.get_logger(__name__)

#: One year, in seconds. The right window for the two streams that are RECORDS —
#: the case-domain log and the poison queue.
ONE_YEAR_SECONDS = 365 * 24 * 60 * 60

#: Seven days, for SIGNALS. The earlier version of this file gave all three streams
#: a year and noted that "SIGNALS is the one that could grow enough to want
#: trimming first". Measured 2026-08-04, that guess was right and the number is
#: worse than it looks: the docket producer is STATELESS by design, rescanning a
#: 48h window every 6h, so every observed fact is re-published 8 times. At 4831
#: signals per scan that is roughly 19k messages and ~12MB a day, which a
#: year-long window turns into ~4.5GB — against a JetStream ceiling of 8GiB
#: shared with CASE_EVENTS.
#:
#: Seven days loses nothing. Signals are "noisy, re-derivable" by the subject
#: vocabulary's own description, the consumers are durable and ack immediately, and
#: the dedup spine that makes a re-observation harmless lives in Postgres
#: (``CaseUpdateProposal.dedup_key``), not in stream history. The only thing stream
#: retention buys here is replay while debugging, and a week is generous for that.
SEVEN_DAYS_SECONDS = 7 * 24 * 60 * 60

#: A hard byte backstop for SIGNALS, well above the ~88MB a week should hold. Its
#: job is not trimming — ``max_age`` does that — but to stop a runaway or
#: backfilling producer from consuming the shared JetStream store and taking
#: CASE_EVENTS down with it. Publishes into a full store FAIL, and
#: ``handle_matcher`` raises on a failed publish, so an unbounded SIGNALS stream is
#: a way for noise to wedge the audit trail.
ONE_GIBIBYTE = 1024**3


@dataclass(frozen=True)
class StreamSpec:
    name: str
    subjects: tuple[str, ...]
    description: str
    max_age_seconds: int = ONE_YEAR_SECONDS
    #: -1 means unlimited, which is JetStream's own sentinel.
    max_bytes: int = -1
    replicas: int = 1


STREAMS: tuple[StreamSpec, ...] = (
    StreamSpec(
        name="SIGNALS",
        subjects=(subjects.ALL_SIGNALS,),
        description="Raw observed facts from producers. Replayable, re-derivable.",
        max_age_seconds=SEVEN_DAYS_SECONDS,
        max_bytes=ONE_GIBIBYTE,
    ),
    StreamSpec(
        name="CASE_EVENTS",
        subjects=(subjects.ALL_CASE_EVENTS,),
        description="The case-domain log: matches, proposals, and decisions.",
    ),
    StreamSpec(
        name="DLQ",
        subjects=(subjects.ALL_DLQ,),
        description="Poison messages past MaxDeliver, kept for human triage.",
    ),
)


async def ensure_streams(js) -> list[str]:
    """Idempotently assert every stream in :data:`STREAMS`, creating OR converging.

    **The "or converging" half was missing, and the docstring was wrong without
    it.** This used to call ``add_stream`` only. JetStream's STREAM.CREATE accepts a
    request that matches the existing stream and REJECTS one that differs, so the
    function was idempotent in the trivial sense — re-running it changed nothing —
    while any actual edit to :data:`STREAMS` failed against a live broker with
    "stream name already in use". Since the whole point of the spec table is that
    it describes what the streams should be, that made it a table of what they were
    when first created.

    So an existing stream is UPDATED to match the spec. The bootstrap identity was
    already granted ``$JS.API.STREAM.UPDATE.>``, which says the intent was there.

    Note what an update cannot do: shrinking ``max_age`` takes effect immediately
    and messages outside the new window are dropped on the next enforcement pass.
    That is intended here — see :data:`SEVEN_DAYS_SECONDS` — but it is a deletion,
    so it should be a considered edit rather than a passing one.

    Args:
        js: A JetStream context (``nats.aio.client.Client.jetstream()``).

    Returns:
        The names asserted, in order.

    Raises:
        Whatever the client raises. Unlike publishing, this is NOT best-effort:
        a consumer that cannot see its stream has nothing to do, and should fail
        loudly at startup rather than idle while looking healthy.
    """
    from nats.js.api import RetentionPolicy, StorageType, StreamConfig

    asserted = []
    for spec in STREAMS:
        config = StreamConfig(
            name=spec.name,
            subjects=list(spec.subjects),
            description=spec.description,
            retention=RetentionPolicy.LIMITS,
            storage=StorageType.FILE,
            max_age=spec.max_age_seconds,
            max_bytes=spec.max_bytes,
            num_replicas=spec.replicas,
        )

        # Ask first, rather than reading an error message to find out. Matching on
        # exception text couples this to a server's wording; `stream_info` raising
        # NotFoundError is the documented way to ask whether a stream exists.
        try:
            await js.stream_info(spec.name)
        except Exception:  # noqa: BLE001 - NotFoundError, and anything else means "try to create"
            existed = False
        else:
            existed = True

        if existed:
            await js.update_stream(config)
        else:
            try:
                await js.add_stream(config)
            except Exception:  # noqa: BLE001 - lost a create race; converge instead of failing
                # Lost a race with another bootstrap between the check and the
                # create. Converge rather than fail: the loser's job is to leave
                # the stream matching the spec, and it now does either way.
                await js.update_stream(config)

        asserted.append(spec.name)
        logger.info(
            "case_events.stream_asserted",
            stream=spec.name,
            subjects=list(spec.subjects),
            replicas=spec.replicas,
            max_age_seconds=spec.max_age_seconds,
            max_bytes=spec.max_bytes,
            created=not existed,
        )
    return asserted
