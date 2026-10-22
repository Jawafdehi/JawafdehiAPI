"""Removing personal data from a document's text before anything stores it.

Sits at the top of the app rather than inside one writer because more than one
calls it: the ingest primitive, the convert job and the PATCH verb all redact
what they are about to store.

🛑 **The legacy parquet import is NOT covered by this.** The ``DocumentPage`` and
``DocumentTable`` rows that ``ingest_document_content`` loads from the upstream
parquet are written as they arrive, and ``…/content`` serves them to anyone —
that is the 35 CIAA annual reports already in the corpus. Documents *this* branch
reads are covered: :func:`redact_bundle` runs inside ``extraction.pdf`` before
the writer ever sees the bundle. So the gap closes only as those 35 are re-read
from their PDFs, not by anything here.

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
is a claim about two classes, and what gets recorded is the count per class
rather than a boolean.

**Both passes, every surface.** The inline pass handles a label and its value in
one span; the table pass handles a value in a cell away from its label. They are
independent — the table pass's row-spent guard reads only its own ``TABLE_*``
markers — so running both over text that mixes prose and rendered tables is
required, not belt-and-braces. A transcript is exactly such a mixture.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from typing import Any

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


#: The JSON-LD key carrying a document's full text. A language map
#: (``{"ne": "…"}``) per MATERIAL_CONTEXT, though a bare string is tolerated
#: because the corpus contains both.
TEXT_KEY = "text"


def _redact_value(value: Any) -> tuple[Any, Counter, bool]:
    """Redact a ``text`` value whatever shape it arrived in."""
    if isinstance(value, str):
        redacted, counts = redact_text(value)
        return redacted, counts, redacted != value
    if isinstance(value, dict):
        out: dict = {}
        totals: Counter = Counter()
        changed = False
        for lang, text in value.items():
            if isinstance(text, str):
                redacted, counts = redact_text(text)
                totals.update(counts)
                changed = changed or redacted != text
                out[lang] = redacted
            else:
                out[lang] = text
        return out, totals, changed
    return value, Counter(), False


def redact_jsonld(jsonld: dict) -> tuple[dict, str, Counter]:
    """``(document with redacted text, the text as it arrived, counts)``.

    **The one place a document's text is redacted**, called from the single
    upsert primitive so that every writer is covered by construction. There were
    five writers of ``data["text"]`` — the convert job and four scrapers — and
    redacting inside one of them left the others publishing identifiers into a
    field the search indexer reads. The AG shelf is charge sheets naming
    individuals and ingests entirely through a scraper, so the highest-PII
    corpus we hold was the one still exposed.

    Returns the original text ONLY when redaction changed something. For the
    overwhelming majority of the archive the two are identical, and keeping a
    second copy of bytes that answer no question is cost without a reader.
    """
    text = (jsonld or {}).get(TEXT_KEY)
    if text is None:
        return jsonld, "", Counter()
    redacted, counts, changed = _redact_value(text)
    if not changed:
        return jsonld, "", counts
    return {**jsonld, TEXT_KEY: redacted}, json.dumps(text, ensure_ascii=False), counts


def original_text(source_text: str) -> Any:
    """The stored original, back in the shape it was written in."""
    if not source_text:
        return None
    try:
        return json.loads(source_text)
    except ValueError:
        # Stored before this was JSON-encoded, or hand-edited. A bare string is
        # a legitimate `text` shape, so return it rather than losing it.
        return source_text


def keep_original(*, incoming: str, stored: str, text_changed: bool) -> str:
    """Which original to keep on the row after a write.

    Three cases, and the middle one is why this is not just ``incoming``:

    * this write carried text we had to redact — keep what arrived;
    * this write carried text we did NOT have to redact, and the document's
      text did not change — somebody round-tripped the redacted representation
      (read the API, wrote it back, which is exactly what a caseworker edit
      does). Keeping ``incoming`` would blank the only copy of the original, and
      it is not recoverable without re-running the transcription;
    * this write carried genuinely different text with nothing to redact — a
      correction that removed the identifier. Clear it, or the row goes on
      answering with text the document no longer has.
    """
    if incoming:
        return incoming
    return "" if text_changed else stored


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
    for figure in bundle.figures:
        # A chart read by a model carries free prose — a title, the transcriber's
        # notes, the reconciliation it did. Nothing stops an identifier printed
        # on a chart from landing there, and a figure is served to the public
        # exactly like a page is.
        figure.title, counts = redact_text(figure.title)
        totals.update(counts)
        figure.notes, counts = redact_text(figure.notes)
        totals.update(counts)
        figure.verify_note, counts = redact_text(figure.verify_note)
        totals.update(counts)
        for point in figure.points:
            point.label, counts = redact_text(point.label)
            totals.update(counts)
            point.series, counts = redact_text(point.series)
            totals.update(counts)
    if totals:
        logger.info("redacted %s: %s", bundle.doc_id, dict(totals))
    bundle.text_source = _mark(bundle.text_source)
    return totals


#: ``DocumentContent.text_source`` is 40 characters. The marker has to survive
#: the writer's truncation, or the claim it makes is cut off mid-word and the
#: idempotence check below stops recognising its own mark — appending again on
#: every re-extract.
_TEXT_SOURCE_WIDTH = 40


def _mark(text_source: str) -> str:
    """``<source>+redacted``, fitted to the column rather than overflowing it."""
    if text_source.endswith(REDACTED_SUFFIX):
        return text_source
    room = _TEXT_SOURCE_WIDTH - len(REDACTED_SUFFIX)
    return f"{text_source[:room]}{REDACTED_SUFFIX}"


def redaction_summary(totals: Counter) -> dict:
    """Counts per class, for a job result. Never a match, never a location."""
    return {key: int(value) for key, value in sorted(totals.items())}
