"""Removing personal data from a document's text before anything stores it.

Shared by both producers of text, which is why it sits here rather than inside
either: the transcript path (``materials.conversion``) and the page-level
extraction path both run it over what they are about to store.

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
required, not belt-and-braces. A transcript is exactly such a mixture.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from typing import Any

logger = logging.getLogger("materials.extraction")

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
