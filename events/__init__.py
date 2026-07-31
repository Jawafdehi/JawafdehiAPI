# SPDX-License-Identifier: Hippocratic-3.0
"""The case-enrichment event bus (NATS + JetStream).

Producers publish observed facts to ``jaw.signal.>``; consumers turn them into
*proposals*; a caseworker approves. Automation never writes a case directly, and
the bus is transport — **not** a system of record. Everything durable lives in
the case record and the proposal store, which is what makes a single-replica
pilot broker an acceptable trade.

The one invariant worth stating up front: **publishing is best-effort and must
never fail a write.** A broker outage degrades enrichment; it does not degrade
the archive. See :mod:`events.bus`.

Nothing here is imported at Django startup, and with ``NATS_URL`` unset every
publish is a logged no-op — so the monolith runs unchanged with no broker at
all, and dev/CI need none.
"""
