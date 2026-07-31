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

#: One year, in seconds — the retention window for all three streams in the
#: pilot. SIGNALS is the one that could grow enough to want trimming first.
ONE_YEAR_SECONDS = 365 * 24 * 60 * 60


@dataclass(frozen=True)
class StreamSpec:
    name: str
    subjects: tuple[str, ...]
    description: str
    max_age_seconds: int = ONE_YEAR_SECONDS
    replicas: int = 1


STREAMS: tuple[StreamSpec, ...] = (
    StreamSpec(
        name="SIGNALS",
        subjects=(subjects.ALL_SIGNALS,),
        description="Raw observed facts from producers. Replayable, re-derivable.",
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
    """Idempotently assert every stream in :data:`STREAMS`.

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
        await js.add_stream(
            StreamConfig(
                name=spec.name,
                subjects=list(spec.subjects),
                description=spec.description,
                retention=RetentionPolicy.LIMITS,
                storage=StorageType.FILE,
                max_age=spec.max_age_seconds,
                num_replicas=spec.replicas,
            )
        )
        asserted.append(spec.name)
        logger.info(
            "case_events.stream_asserted",
            stream=spec.name,
            subjects=list(spec.subjects),
            replicas=spec.replicas,
        )
    return asserted
