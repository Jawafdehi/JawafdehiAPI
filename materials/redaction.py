"""Removing personal data from a document's text before anything stores it.

Sits at the top of the app rather than inside one writer because more than one
calls it: the ingest primitive, the convert job and the PATCH verb all redact
what they are about to store.

🛑 **The page-level content plane is NOT covered by this.** ``DocumentPage`` and
``DocumentTable`` rows are written by ``ingest_document_content`` straight from
the upstream parquet, and ``…/content`` serves them to anyone. That corpus is 35
CIAA annual reports today, and closing it belongs with the extraction pipeline
that will become their second writer — not here, where it would be a claim this
branch does not deliver.

``likhit.privacy`` is the detector and the redactor; this is the one place the
platform calls it, and the tier it calls it at.

**Why here and not after.** The package's own CLI route is: convert to markdown on
disk, then run ``likhit-redact`` over the resulting tree. That produces three
artifacts and *writes the unredacted one first* — structurally wrong for a service
that must never persist unredacted text. Redacting before the writer sees the text
means the unredacted copy exists only in this process's memory and in
``Material.source_text``, which is behind ``materials.view_unredacted``. That is
the property the redaction plan (``docs/2026-10-13-likhit-pii-redaction``) asks
for and could not express against the package as shipped.

**The tier is STRICT, and the tier is the whole policy.** likhit 0.2.0 replaced
the two-passes-by-hand arrangement with named tiers, and which one we ask for is
the only decision this module makes. ``strict`` removes citizenship number, date
of birth, mobile, landline, email, bank account, age and passport.

Two things it does NOT remove, both deliberate and both upstream's reasoning:

* **PAN.** A permanent account number identifies a *business*, is public by
  design, and is frequently the only handle a reader has to tie a procurement
  finding to a registered contractor. Removing it protects nobody and costs the
  reader the check.
* **Names.** No tier removes one, and upstream is explicit that none ever will.
  For this corpus that is the product: naming officials who were prosecuted,
  convicted or acquitted is what a public accountability record is for.

The narrower ``identifiers`` tier — citizenship and date of birth only — is the
scope that was formally adjudicated, and it is not what we ship, because it is
close to a no-op on the corpus that actually carries personal data. Measured on a
real charge sheet (likhit's ``samples/aarop-patra.pdf``, 103k characters):
``identifiers`` removes **3** values and leaves **5 mobile numbers** in the text
the search indexer publishes; ``strict`` removes **11**. Override with
``MATERIALS_REDACTION_MODE`` only with that measurement in hand.

**Every surface, in one call.** ``likhit.privacy.redact_text`` runs inline →
tables → contact itself. Driving the passes by hand — which is what this module
used to do — meant every caller had to know that the table pass exists and that
omitting it leaves table-held identifiers in place.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from typing import Any

from django.conf import settings

logger = logging.getLogger("materials.extraction")


class RedactionUnavailable(RuntimeError):
    """``likhit.privacy`` cannot give us the tier we asked for.

    Raised rather than silently skipped. A pipeline that quietly stores
    unredacted text when the redactor is missing is worse than one that stops:
    the first produces a published corpus nobody knows is unredacted.
    """


def _redactor():
    """``(redact_text, mode)`` from likhit, or refuse.

    Two distinct failures, both of which used to be one ImportError: a pin older
    than ``likhit.privacy`` (no redactor at all), and a pin that has the redactor
    but not the tiers — anything between likhit #107 and #113. The second one is
    the dangerous one, because ``from likhit.privacy import redact_text`` would
    have succeeded against #112's table-pass helper of the same name and silently
    redacted at a narrower scope than the one named here.
    """
    try:
        from likhit.privacy import RedactionMode, redact_text
    except ImportError as exc:  # pragma: no cover - only on an older likhit pin
        raise RedactionUnavailable(
            "likhit.privacy does not expose the tiered redactor in this build; "
            f"the installed likhit predates it ({exc})"
        ) from exc

    configured = getattr(settings, "MATERIALS_REDACTION_MODE", "strict")
    try:
        mode = RedactionMode(configured)
    except ValueError as exc:
        # Not a fallback. An unreadable setting means nobody knows what scope the
        # corpus was redacted at, and guessing one writes that uncertainty into
        # every document from here.
        raise RedactionUnavailable(
            f"MATERIALS_REDACTION_MODE={configured!r} is not a likhit tier; "
            f"expected one of {[m.value for m in RedactionMode]}"
        ) from exc
    if mode is RedactionMode.NONE:
        raise RedactionUnavailable(
            "MATERIALS_REDACTION_MODE='none' would publish the text unredacted; "
            "to disable redaction, do not call this module"
        )
    return redact_text, mode


def redact_text(text: str) -> tuple[str, Counter]:
    """Redact one piece of text to the configured tier.

    The counter is keyed on the **kind that was removed** — ``citizenship``,
    ``mobile``, ``bank_account`` — taken from likhit's journal rather than from
    its raw statistics. Those statistics are a working record: they carry
    ``*_redacted`` for some classes and ``*_targeted`` for others, plus
    diagnostics like ``physical_cells`` that are present on every call and count
    nothing removed. Keying on them is how "did we redact anything?" became true
    for every document in the archive, and how the contact classes would have
    gone unreported the moment they started being removed.
    """
    if not text.strip():
        return text, Counter()
    redact, mode = _redactor()
    result = redact(text, mode=mode)
    return result.text, Counter(row["kind"] for row in result.journal)


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
