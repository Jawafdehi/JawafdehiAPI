"""Removing personal data from a document's text before anything stores it.

``likhit.privacy`` is the detector and the redactor; this is the one place the
extraction pipeline calls it, and the order it calls it in.

**Why here and not after.** The package's own CLI route is: convert to markdown on
disk, then run ``likhit-redact`` twice over the resulting tree, once for inline
values and once for table cells. That produces three artifacts and *writes the
unredacted one first* — structurally wrong for a service that must never persist
unredacted text. Redacting the bundle before the writer sees it means the
unredacted text exists only in this process's memory, which is the property the
redaction plan (``docs/2026-10-13-likhit-pii-redaction``) asks for and could not
express against the package as shipped.

**What is actually removed is narrower than what is found, and that gap is the
point.** Today the redactor removes two classes — citizenship numbers and dates
of birth. Phone, landline, email, PAN and bank account are *detected and
deliberately left in place*: the contact pass exists, but it lives in another
repository (the OAG release), which is why ``[REDACTED:PHONE-NO]`` is a
registered placeholder that nothing in likhit emits. Verified on this pin: a
sample carrying all three gives back a redacted citizenship number, a redacted
date of birth, and an untouched mobile number. So calling a document "redacted"
is a claim about two classes, and :func:`redaction_summary` reports the counts
rather than a boolean.

**Both passes, every surface.** The inline pass handles a label and its value in
one span; the table pass handles a value in a cell away from its label. They are
independent — the table pass's row-spent guard reads only its own ``TABLE_*``
markers — so running both over text that mixes prose and rendered tables is
required, not belt-and-braces. A page's text carries its tables' markdown (see
:mod:`materials.extraction.pdf`), which is exactly such a mixture.
"""

from __future__ import annotations

import logging
from collections import Counter

logger = logging.getLogger("materials.extraction")

#: ``text_source`` suffix recording that a document's text went through the
#: redactor. It rides on the provenance block the material detail payload already
#: serves, because a reader comparing our text against the published PDF has to
#: be able to tell a removal from a transcription error — and the placeholders
#: themselves say so in the text, but only on the pages that carried a value.
REDACTED_SUFFIX = "+redacted"


class RedactionUnavailable(RuntimeError):
    """``likhit.privacy`` is not importable — the pin predates the subpackage.

    Raised rather than silently skipped. A pipeline that quietly stores
    unredacted text when the redactor is missing is worse than one that stops:
    the first produces a published corpus nobody knows is unredacted.
    """


def _passes():
    try:
        from likhit.privacy import redact_inline_text, redact_table_text
    except ImportError as exc:  # pragma: no cover - only on an older likhit pin
        raise RedactionUnavailable(
            "likhit.privacy is not available in this build; "
            f"the installed likhit predates the redactor ({exc})"
        ) from exc
    return redact_inline_text, redact_table_text


def redact_text(text: str) -> tuple[str, Counter]:
    """Both redaction passes over one piece of text, inline first.

    Inline first is conventional rather than required — the passes read their own
    markers and neither hides the other's targets — but a fixed order is what
    makes two runs over the same document produce the same bytes, which the
    content ETag depends on.
    """
    if not text.strip():
        return text, Counter()
    redact_inline_text, redact_table_text = _passes()
    counts: Counter = Counter()
    text, _journal, inline_counts = redact_inline_text(text)
    counts.update(inline_counts)
    text, _journal, table_counts = redact_table_text(text)
    counts.update(table_counts)
    return text, counts


def redact_bundle(bundle) -> Counter:
    """Redact every stored surface of ``bundle`` in place; return what was removed.

    Pages and table markdown both go through both passes. The journals are
    dropped on purpose: a journal row names the value it replaced, so keeping one
    would re-create the problem the redaction exists to solve. The counters say
    how much was removed per class and name nothing, which is why they are safe
    to log and to put in a job result.
    """
    totals: Counter = Counter()
    for page in bundle.pages:
        page.text, counts = redact_text(page.text)
        page.chars = len(page.text)
        totals.update(counts)
    for table in bundle.tables:
        table.markdown, counts = redact_text(table.markdown)
        totals.update(counts)
        # The header is a list of cell strings rather than one blob, so it is
        # redacted cell by cell. It is rarely where an identifier sits, and
        # "rarely" is not "never" — a one-column table of citizenship numbers
        # puts one in the header.
        table.header = [
            redact_text(cell)[0] if isinstance(cell, str) else cell
            for cell in (table.header or [])
        ]
        table.caption, counts = redact_text(table.caption)
        totals.update(counts)
    if totals:
        logger.info("redacted %s: %s", bundle.doc_id, dict(totals))
    if not bundle.text_source.endswith(REDACTED_SUFFIX):
        bundle.text_source = f"{bundle.text_source}{REDACTED_SUFFIX}"
    return totals


def redaction_summary(totals: Counter) -> dict:
    """Counts per class, for a job result. Never a match, never a location."""
    return {key: int(value) for key, value in sorted(totals.items())}
