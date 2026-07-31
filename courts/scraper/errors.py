"""Exceptions shared by the court-portal parsers.

Kept in their own module so the pure parsers can raise them without importing
:mod:`courts.scraper.base` (which pulls in the ORM).
"""

from __future__ import annotations


class UnexpectedPage(RuntimeError):
    """The portal served something that is not the page we asked for.

    The distinction that matters is **"no such case" vs "no answer"**. Both arrive
    as HTTP 200 — the courts' F5 front end serves challenges, maintenance notices
    and truncated bodies with a 200 — so ``raise_for_status`` never sees them and a
    parser that returns "nothing found" for both is indistinguishable from a court
    that genuinely has no such docket.

    That conflation is expensive in exactly one place: the register sweep records a
    not-found in ``RegisterProbe`` and skips the number for 90 days. One soft block
    would otherwise write a whole budget's worth of false absences and report a
    clean run. Raising instead keeps the failure inside the sweep's error path,
    where it counts toward the abort threshold and is retried.
    """
