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

#: ``text_source`` suffix recording that a document's text went through the
#: redactor. It rides on the provenance block the material detail payload already
#: serves, because a reader comparing our text against the published PDF has to
#: be able to tell a removal from a transcription error — and the placeholders
#: themselves say so in the text, but only on the pages that carried a value.
REDACTED_SUFFIX = "+redacted"


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


class RedactionIncomplete(RuntimeError):
    """The redactor ran, and its own scanner still finds what it should have taken.

    Not a crash and not a near-miss: likhit's detector is wider than its removers,
    so a labelled value whose shape no rule anchors is *found and left*. Measured
    on the live charge-sheet shelf, 67 of 883 documents came back this way —
    mostly bank accounts, with some dates of birth and citizenship numbers.

    It carries counts per class and never a value, so it is safe to log.
    """

    def __init__(self, residual: Counter):
        self.residual = residual
        super().__init__(
            "redaction left personal data in the text: "
            f"{dict(sorted(residual.items()))}"
        )


#: Which tier first removes each class likhit's scanner reports.
#:
#: Only the tier BOUNDARY lives here; whether the configured tier reaches it is
#: asked of likhit (``mode.removes(minimum)``) rather than re-decided, so
#: widening a tier upstream needs no edit here.
#:
#: ``phone_labelled`` is in this table and NOT exempt, which it was until the
#: first corpus run proved otherwise: an English-labelled phone number
#: (``Phone No.: 014234567``) is removed and journalled under that kind, 193
#: times in a single charge-sheet sweep. Reading it as a diagnostic left residual
#: English-labelled phone numbers ungated — on exactly the documents where the
#: details were written in English.
def _removal_tiers() -> dict:
    from likhit.privacy import RedactionMode

    return {
        "citizenship": RedactionMode.IDENTIFIERS,
        "date_of_birth": RedactionMode.IDENTIFIERS,
        "mobile": RedactionMode.CONTACT,
        "landline": RedactionMode.CONTACT,
        "phone_labelled": RedactionMode.CONTACT,
        "email": RedactionMode.CONTACT,
        "bank_account": RedactionMode.STRICT,
        "passport": RedactionMode.STRICT,
    }


#: Scanner keys no tier removes, and that therefore must never gate a write. A
#: permanent account number identifies a *business*, is public by design, and is
#: often the only handle tying a procurement finding to a registered contractor.
NOT_REMOVED = frozenset({"pan"})


def configured_mode():
    """The tier every writer redacts at, as a likhit ``RedactionMode``.

    Exposed because the PDF producer needs the tier itself rather than the
    classes it removes — and reaching into ``_redactor`` from another module to
    get it would put a second copy of "what do we do when likhit cannot give us
    the tier" outside this file, which is the one place that question is
    answered.
    """
    _redact, mode = _redactor()
    return mode


def gated_classes() -> set:
    """Classes the configured tier is expected to have zeroed out."""
    _redact, mode = _redactor()
    return {
        kind
        for kind, minimum in _removal_tiers().items()
        if kind not in NOT_REMOVED and mode.removes(minimum)
    }


def residual_classes(value: Any) -> Counter:
    """What the scanner still finds in already-redacted text, by class.

    This is the check likhit's own ``redact_tree`` makes and its single-call
    ``redact_text`` does not — which is why it has to live here, at the boundary
    every writer crosses, rather than in whichever caller remembered.
    """
    from likhit.privacy import scan_text

    gated = gated_classes()
    found: Counter = Counter()
    for chunk in _text_chunks(value):
        detections, _name_signals = scan_text(chunk)
        for kind, count in detections.items():
            if count and kind in gated:
                found[kind] += count
    return found


def _text_chunks(value: Any):
    """Every non-empty string in a ``text`` value, whatever shape it arrived in."""
    if isinstance(value, str):
        if value.strip():
            yield value
    elif isinstance(value, dict):
        for item in value.values():
            if isinstance(item, str) and item.strip():
                yield item


def redact_jsonld(
    jsonld: dict, *, on_residue: str = "withhold"
) -> tuple[dict, str, Counter]:
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

    **The result is re-scanned before it is handed back**, because running the
    redactor is not the same as having removed everything: on the live
    charge-sheet shelf, 67 of 883 documents came back still carrying a class this
    tier removes. ``on_residue`` says what to do about it, and the two callers
    want opposite things:

    * ``"withhold"`` (the write path) — drop ``text`` from the document entirely.
      The material, its metadata and its PDF link still land, so the archive
      keeps the record and the reader keeps the citation; what does not land is
      an unsafe transcript in the field the search indexer reads. The text as it
      arrived still goes to ``source_text``, behind ``materials.view_unredacted``,
      so nothing is lost — it is unpublished, not destroyed.
    * ``"raise"`` (the backfill) — refuse, so the sweep leaves an already-public
      row exactly as it was and reports it. Removing a transcript the archive has
      been serving is a decision for a human looking at a list, not a side effect
      of a sweep.

    Unattended writes fail closed; a supervised sweep stops and asks.
    """
    if on_residue not in ("withhold", "raise"):
        # Not a fallback. Every unrecognised value would behave as "withhold",
        # so a typo in the one caller that wants to be TOLD about residue would
        # silently turn its refusal into a quiet drop.
        raise ValueError(
            f"on_residue must be 'withhold' or 'raise', not {on_residue!r}"
        )
    text = (jsonld or {}).get(TEXT_KEY)
    if text is None:
        return jsonld, "", Counter()
    redacted, counts, changed = _redact_value(text)

    residual = residual_classes(redacted)
    if residual:
        if on_residue == "raise":
            raise RedactionIncomplete(residual)
        logger.error(
            "redaction incomplete for %s: %s — text withheld from the document",
            (jsonld or {}).get("@id"),
            dict(sorted(residual.items())),
        )
        withheld = {k: v for k, v in jsonld.items() if k != TEXT_KEY}
        return withheld, json.dumps(text, ensure_ascii=False), counts

    if not changed:
        return jsonld, "", counts
    return {**jsonld, TEXT_KEY: redacted}, json.dumps(text, ensure_ascii=False), counts


def withheld_by_the_write_path():
    """Rows whose transcript the gate withheld — holds an original, serves no text.

    Withholding is deliberately **not** announced in the public payload. The
    obvious honest move — a marker saying "we are not showing you this
    transcript, it had three bank accounts in it" — is a map to unredacted
    personal data for as long as the original PDF stays publicly fetchable, which
    is exactly the state the archive is in until the PDF half of this work lands.
    A reader gains a label; anyone harvesting gains a shortlist.

    So the signal is kept where operators are: an ERROR log at the moment it
    happens, and this query afterwards. It needs no new column, because the state
    is already exact — an original kept outside ``data`` while ``data`` serves no
    text is a combination nothing else produces.
    """
    from materials.models import Material

    return (
        Material.objects.using("ngm")
        .filter(is_deleted=False)
        .exclude(source_text="")
        .exclude(data__has_key=TEXT_KEY)
    )


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
