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
SIGNAL_DOCKET_HEARING_ADDED = "jaw.signal.docket.hearing.added"
SIGNAL_DOCKET_VERDICT_ENTERED = "jaw.signal.docket.verdict.entered"
SIGNAL_DOCKET_STATUS_CHANGED = "jaw.signal.docket.status.changed"
SIGNAL_COURTORDER_PUBLISHED = "jaw.signal.courtorder.published"
SIGNAL_CIAA_PRESSRELEASE = "jaw.signal.ciaa.pressrelease"
SIGNAL_NEWS_MATCHED = "jaw.signal.news.matched"
SIGNAL_MANUAL_NOTE = "jaw.signal.manual.note"

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
