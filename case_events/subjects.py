# SPDX-License-Identifier: Hippocratic-3.0
"""Subject vocabulary for the bus.

Two tiers, and the split is deliberate: ``jaw.signal.>`` is raw observed facts
straight from producers (noisy, re-derivable), while ``jaw.case.>`` is the
case-domain log — the audit trail of what the system decided. Consumers filter
by subject, so a consumer that only cares about decisions never sees the noise.

Subjects are string constants rather than an enum because they are also matched
with wildcards (``jaw.case.update.*``) and are compared against values arriving
off the wire, where a bare string is what you actually have.
"""

# ── jaw.signal.> — raw observed facts from producers ─────────────────────────
#
# Declaring a subject is not the same as emitting one. Two of these have NO
# producer, and that is recorded in :data:`UNPRODUCED` below rather than left for
# a reader to infer from the absence of a call site — the consumer side maps all
# of them, so the map reads as five wired sources when three are.
SIGNAL_DOCKET_HEARING_ADDED = "jaw.signal.docket.hearing.added"
SIGNAL_DOCKET_VERDICT_ENTERED = "jaw.signal.docket.verdict.entered"
SIGNAL_DOCKET_STATUS_CHANGED = "jaw.signal.docket.status.changed"
SIGNAL_COURTORDER_PUBLISHED = "jaw.signal.courtorder.published"
SIGNAL_CIAA_PRESSRELEASE = "jaw.signal.ciaa.pressrelease"
SIGNAL_NEWS_MATCHED = "jaw.signal.news.matched"
SIGNAL_MANUAL_NOTE = "jaw.signal.manual.note"

#: Subjects declared and consumable, but which nothing currently publishes —
#: mapped to why, because "why not" is the useful half.
#:
#: Kept rather than deleted so the intended shape of the vocabulary stays visible,
#: but named explicitly so nobody reads the consumer's provenance map as evidence
#: of coverage. A subject leaving this dict is how a producer announces itself.
UNPRODUCED = {
    SIGNAL_DOCKET_STATUS_CHANGED: (
        "Not derivable from the lake as it stands. Detecting a status CHANGE needs "
        "the previous value, and the mirror overwrites case_status in place without "
        "retaining history — so a producer would need a snapshot table or a schema "
        "change first. See case_events.producers.dockets, which says the same thing "
        "at the point where it would otherwise be emitted."
    ),
    SIGNAL_NEWS_MATCHED: (
        "No news ingestion exists yet. There is no source to observe, so this is a "
        "placeholder for a pipeline rather than a gap in one that runs."
    ),
}

# ── jaw.case.> — the case-domain log ─────────────────────────────────────────
CASE_MATCHED = "jaw.case.matched"
CASE_UPDATE_PROPOSED = "jaw.case.update.proposed"
CASE_UPDATE_APPROVED = "jaw.case.update.approved"
CASE_UPDATE_REJECTED = "jaw.case.update.rejected"

# ── wildcards, for consumer filter subjects ──────────────────────────────────
ALL_SIGNALS = "jaw.signal.>"
ALL_CASE_EVENTS = "jaw.case.>"
ALL_CASE_UPDATES = "jaw.case.update.*"
ALL_DLQ = "jaw.dlq.>"

#: Prefix for poison messages that exhausted their delivery budget. The
#: originating subject is appended, so a message that died on
#: ``jaw.case.matched`` lands on ``jaw.dlq.jaw.case.matched`` and stays
#: attributable. Nothing is dropped silently.
DLQ_PREFIX = "jaw.dlq."


def dlq_subject(original_subject: str) -> str:
    """The DLQ subject a poison message from ``original_subject`` republishes to."""
    return f"{DLQ_PREFIX}{original_subject}"
