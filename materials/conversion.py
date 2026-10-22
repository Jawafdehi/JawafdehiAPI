"""Server-side plumbing for the ``material_convert`` job kind (OCR → full text).

This is the *data-plane FTS feed*: a Material that carries a file (a RAW/source
document link) but no extracted ``text`` is not full-text searchable — the
unified-search indexer reads ``data["text"]`` (``materials/search_index.py``),
and nothing populates it on upload. ``material_convert`` closes that gap
asynchronously on the central ``jobs`` queue.

The three seams here are the SERVER-side halves of the job (the worker-side
conversion handler lives in ``materials.job_handlers``, which reuses the in-repo
``review.converter`` — likhit/MarkItDown — so its heavy deps never import into the
API process):

- :func:`enqueue_material_convert` — best-effort enqueue after the material write
  (cross-DB; idempotent via dedup key) from a file-bearing write path (the upload
  endpoint, ingestion).
- :func:`build_convert_payload` — resolve the source file URL(s) from the
  Material's ``associatedMedia`` at claim time, so the DB-free consumer gets its
  input without touching the DB. Registered as the kind's ``build_payload`` hook.
- :func:`apply_convert_result` — persist the worker's extracted text: store the
  markdown in R2 as a ``MARKDOWN`` MediaObject and set ``data["text"]``, then save
  the Material (the ``post_save`` signal reindexes it). The kind's ``on_result``.

Only :func:`build_convert_payload` / :func:`apply_convert_result` are wired into
the queue (``jobs.consumers``); OCR itself is the worker's job.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any

from . import artifacts
from . import jsonld
from . import provenance

logger = logging.getLogger("materials.conversion")

CONVERT_KIND = "material_convert"

#: Link roles whose ``contentUrl`` is an original document worth OCR-ing, in
#: preference order. MARKDOWN is intentionally excluded (it's our own output);
#: SOURCE_PAGE is HTML, not a document scan.
_SOURCE_ROLES = ("RAW", "ALTERNATE", "PERMALINK")

#: Language tag for the extracted text map (``{"ne": …}``); NGM documents are
#: Nepali. The indexer's ``body`` analysis handles Devanagari + transliteration.
_TEXT_LANG = "ne"


def _media_list(data: dict[str, Any]) -> list[dict[str, Any]]:
    """The Material's ``associatedMedia`` as a list (it may be a single dict)."""
    media = data.get("associatedMedia")
    if isinstance(media, dict):
        return [media]
    if isinstance(media, list):
        return [m for m in media if isinstance(m, dict)]
    return []


def source_urls(data: dict[str, Any]) -> list[str]:
    """Ordered, de-duplicated source document URLs to OCR for this Material.

    Picks MediaObject ``contentUrl``s by role preference (:data:`_SOURCE_ROLES`),
    skipping our own MARKDOWN output and non-document SOURCE_PAGE links. Returns
    ``[]`` when the Material carries nothing OCR-able.
    """
    by_role: dict[str, list[str]] = {r: [] for r in _SOURCE_ROLES}
    for mo in _media_list(data):
        # contentUrl comes from free-form JSONB; guard against a non-string
        # (int/list/dict) so a malformed doc can't crash the whole convert.
        raw_url = mo.get("contentUrl")
        if not isinstance(raw_url, str):
            continue
        url = raw_url.strip()
        if not url:
            continue
        role = mo.get("jawafdehi:linkRole") or "RAW"
        if role in by_role:
            by_role[role].append(url)
    ordered: list[str] = []
    for role in _SOURCE_ROLES:
        for url in by_role[role]:
            if url not in ordered:
                ordered.append(url)
    return ordered


def enqueue_material_convert(material_iri: str, *, priority: int = 100):
    """Enqueue an OCR/extraction job for ``material_iri`` (idempotent on the IRI).

    Call from a file-bearing write path *after* the Material is upserted, in the
    same request. ``jobs.enqueue`` dedups on ``dedup_key`` so re-uploading while a
    convert is still queued/running is a no-op; a fresh upload after a prior
    convert finished enqueues again (the key is freed on terminal state). Returns
    the ``Job`` (or the existing one), or ``None`` if the jobs app is unavailable.
    """
    from jobs import queue as jobs_queue

    return jobs_queue.enqueue(
        CONVERT_KIND,
        payload={"material_iri": material_iri},
        dedup_key=f"{CONVERT_KIND}:{material_iri}",
        priority=priority,
    )


def build_convert_payload(job) -> dict:
    """``build_payload`` hook: resolve the source URLs for a claimed convert job.

    Reads the Material row named by ``payload['material_iri']`` and returns the
    ordered ``source_urls`` for the DB-free worker to fetch + OCR. Raises when the
    material is missing or has no OCR-able source (so the job fails fast rather
    than the worker no-op-ing).
    """
    from .models import Material

    iri = (job.payload or {}).get("material_iri")
    if not iri:
        raise ValueError("material_convert payload is missing 'material_iri'.")
    row = Material.objects.filter(pk=iri, is_deleted=False).first()
    if row is None:
        raise ValueError(f"material_convert: no live Material at {iri!r}.")
    urls = source_urls(row.data or {})
    if not urls:
        raise ValueError(f"material_convert: {iri} has no OCR-able source link.")
    return {"source_urls": urls}


def apply_convert_result(job, result: dict) -> None:
    """``on_result`` hook: persist the worker's extracted text onto the Material.

    ``result`` carries ``{"text": <str>, "source_url": <str>}`` (the converted
    markdown). This stores the markdown in R2 as a ``MARKDOWN`` MediaObject and
    sets ``data['text']`` (the field the search indexer reads), then saves — the
    ``post_save`` signal re-indexes the row into unified search. Idempotent: a
    prior MARKDOWN MediaObject for this material is replaced, not duplicated.

    Best-effort by contract (``jobs.finalize`` swallows hook errors); a failure
    here leaves the job DONE but the text unset — a re-run (re-upload) recovers.
    """
    from django.db import transaction

    iri = (job.payload or {}).get("material_iri")
    text = (result or {}).get("text") or ""
    if not iri or not text.strip():
        logger.info("material_convert %s: empty text, nothing to persist", iri)
        return

    from .models import Material

    # Cheap pre-check so we don't upload to R2 for a material that's already gone.
    if not Material.objects.filter(pk=iri, is_deleted=False).exists():
        logger.warning("material_convert %s: material gone before result apply", iri)
        return

    # 1) Land the markdown in R2 as a roled MARKDOWN link, keyed on the
    # transcript's own SHA-256.
    #
    # The filename here is a constant, and the storage backend keys objects on a
    # hash of the NAME — so every material's transcript resolved to the SAME R2
    # object, each conversion overwriting the last (seventeen materials — every
    # material ever converted — were observed sharing one .md URL in production,
    # measured 2026-09-30). ``content_hash`` is what makes the key per-transcript;
    # the constant name now only supplies the extension. Two materials whose text
    # is byte-identical legitimately share one object — safe only while nothing in
    # this path ever DELETES a stored object, which is true today and is the
    # reason cases/image_views.py made the opposite choice (uuid4) for images.
    #
    # This is forward-only: the materials already damaged still link to another
    # document's transcript. Their own ``data["text"]`` below is per-row and
    # correct, so a backfill can re-store from the DB without re-running OCR.
    #
    # Done OUTSIDE the DB transaction below — a network round-trip must not hold a
    # row lock. NOTE: store_file_as_link uses FILE_STORAGE_PREFIX (default
    # "case_uploads/"), so the .md currently lands under that shared prefix. The
    # material-specific R2 layout (docs/data-plane-design.md §3) is folded in with
    # the provenance work (§8 item 2); until then the link is correct, just not
    # prefix-isolated.
    # The transcript is stored TWICE: as converted, and redacted. Both are files
    # of ours, and the difference between them is who may read each — see
    # materials.artifacts. The redacted one is what ``data["text"]`` becomes, and
    # ``data["text"]`` is what the search indexer reads (materials/search_index),
    # so an unredacted transcript here is an unredacted snippet in public search
    # results. That is the exposure this split closes.
    redacted_text, redaction_counts = _redact(iri, text)
    if redacted_text is None:
        # We could not redact. Storing the conversion anyway would publish the
        # transcript and the identifiers in it, so nothing is written at all —
        # and it is logged as an error rather than left to look like a document
        # that simply has no text. A re-upload re-runs the whole job.
        logger.error(
            "material_convert %s: not storing a transcript we could not redact", iri
        )
        # Scrub FIRST. Returning early here used to leave the full unredacted
        # transcript in jobs_job.result forever — the one path where it matters
        # most, because nothing downstream ever stored or gated it.
        _scrub_job_result(job)
        return

    link = _store(text, "MARKDOWN", "material.md")
    redacted_link = _store(
        redacted_text, artifacts.REDACTED_TRANSCRIPT_ROLE, "material.redacted.md"
    )

    # 2) Read-modify-write the JSONB doc under a row lock so a concurrent write
    # (e.g. a re-upload upserting the same @id) can't clobber our text, or ours
    # theirs — the whole ``data`` blob is rewritten, so a naive read+save would
    # lose the other writer's fields (lost-update).
    # `using="ngm"` is load-bearing, not decoration: `Material` routes to `ngm`,
    # so a bare `atomic()` opens a transaction on `default` and leaves the `ngm`
    # connection in autocommit — on Postgres `select_for_update` then raises
    # TransactionManagementError ("cannot be used outside of a transaction"), and
    # the sqlite gate hides it because SQLite sets has_select_for_update = False
    # and Django skips the check entirely.
    with transaction.atomic(using="ngm"):
        row = (
            Material.objects.using("ngm")
            .select_for_update()
            .filter(pk=iri, is_deleted=False)
            .first()
        )
        if row is None:  # deleted between the pre-check and the lock
            logger.warning(
                "material_convert %s: material gone before result apply", iri
            )
            return
        data = dict(row.data or {})
        # Replace-or-append the MARKDOWN MediaObject (idempotent re-run). MARKDOWN
        # is our own singular derived output, so dedup is by role (drop any prior
        # MARKDOWN, add the fresh one) — not by content hash.
        ours = {"MARKDOWN", artifacts.REDACTED_TRANSCRIPT_ROLE}
        media = [
            mo
            for mo in _media_list(data)
            if (mo.get("jawafdehi:linkRole") or "RAW") not in ours
        ]
        source_url = (result or {}).get("source_url")
        for mo in (
            _media(link, "MARKDOWN", text, source_url, derived=False),
            _media(
                redacted_link,
                artifacts.REDACTED_TRANSCRIPT_ROLE,
                redacted_text,
                source_url,
                derived=True,
            ),
        ):
            if mo is not None:
                media.append(mo)
        data["associatedMedia"] = media
        # The searchable full text (language-mapped, per MATERIAL_CONTEXT) — the
        # REDACTED transcript where we have one. The indexer reads this field, so
        # whatever is here is what a public search snippet can quote.
        data["text"] = {_TEXT_LANG: _without_markers(redacted_text)}
        # The original, kept once and reachable only with view_unredacted. The
        # expensive half of this job is the transcription, and it is not
        # reproducible byte-for-byte, so an original we drop costs an OCR run to
        # recover. Stored here rather than left to the ingest boundary because
        # this path does not go through it — it writes the row directly.
        # Only when redaction changed something — the column's contract. On the
        # rest of the archive the two are identical and a second copy answers no
        # question; the markers stripped from the indexed text are still in the
        # MARKDOWN artifact, which is where a reader would look for them.
        row.source_text = (
            json.dumps({_TEXT_LANG: text}, ensure_ascii=False)
            if redacted_text != text
            else ""
        )

        row.data = data
        row.save(  # post_save → reindex
            update_fields=["data", "source_text", "updated_at"]
        )
    _scrub_job_result(job)
    logger.info(
        "material_convert %s: stored %d chars markdown + text (from %s)",
        iri,
        len(text),
        (result or {}).get("source_url"),
    )


def _redact(iri: str, text: str) -> tuple[str | None, dict]:
    """``(redacted_text, counts)``, or ``(None, {})`` when we cannot redact.

    **None is not "nothing to remove" — it is "we could not look".** The two must
    stay apart here: a transcript with no identifiers in it is safe to publish,
    and one the redactor never ran over is not. When this returns None the caller
    publishes neither a redacted file nor a redacted ``text`` field, so the
    document keeps its original behaviour (staff-visible transcript, no public
    redacted copy) instead of being labelled redacted on the strength of a
    failure.
    """
    from .redaction import redact_text

    try:
        redacted, counts = redact_text(text)
    except Exception as exc:  # noqa: BLE001 - ANY failure means do not publish
        # Not just RedactionUnavailable. These are regexes over transcripts
        # averaging ~56,000 characters, so a re.error, a TypeError or a bug in
        # the redactor raises out of this hook — which jobs.finalize swallows,
        # leaving the job DONE, nothing stored, and the document permanently
        # absent from search with no failure to retry. Catching it here turns an
        # invisible loss into a logged refusal.
        logger.exception("material_convert %s: redaction failed: %s", iri, exc)
        return None, {}
    if counts:
        logger.info("material_convert %s: redacted %s", iri, dict(counts))
    return redacted, dict(counts)


def _store(text: str, role: str, name: str):
    """Put one transcript in object storage, keyed on its own bytes.

    The key must follow the CONTENT, not the name: the name is a constant, and
    the backend hashes it, so a per-role constant name once had every material's
    transcript resolving to a single object that each conversion overwrote —
    seventeen production materials shared one .md URL (measured 2026-09-30).
    """
    from django.core.files.base import ContentFile
    from jawafdehi_shared.storage import store_file_as_link

    raw = text.encode("utf-8")
    return store_file_as_link(
        ContentFile(raw, name=name),
        role=role,
        content_hash=hashlib.sha256(raw).hexdigest(),
    )


def _media(link, role: str, text: str, source_url, *, derived: bool):
    """One stored transcript as a roled MediaObject carrying its provenance.

    Both transcripts go through here so they cannot drift — the redacted one was
    previously assembled by a near-copy of this block, which is how it ended up
    stripped from associatedMedia unconditionally and re-added only
    conditionally.

    ``derived`` is the honest half: the redacted copy was not produced by OCR-ing
    the publisher's file, it was produced by redacting our own transcript.
    Stamping both ``fetch_method="ocr"`` would describe two artifacts as the same
    kind of claim about the same source, differing only in a digest.
    """
    mo = jsonld._media_object({"link": link["link"], "role": role})
    if mo is None:
        return None
    raw = text.encode("utf-8")
    mo["encodingFormat"] = "text/markdown"
    mo[provenance.PROVENANCE_KEY] = provenance.build_provenance(
        sha256=hashlib.sha256(raw).hexdigest(),
        fetch_method="redaction" if derived else "ocr",
        source_url=source_url,
        content_length=len(raw),
        ocr_engine="likhit",
    )
    return mo


def _without_markers(text: str) -> str:
    """Text with likhit's own in-band markers removed.

    likhit emits ``<!-- likhit:page N -->`` per page and stamps
    ``<!-- likhit:needs-ocr pages=… -->`` on a document it could not fully read.
    They are useful provenance in the FILE and noise in the index: this is the
    field search reads, so a 446-page report would otherwise contribute 446 HTML
    comments to its body, and a highlight can return one inside a user-facing
    snippet. likhit exports the stripper for exactly this; nothing called it.
    """
    try:
        from likhit.renderers.markdown import strip_page_anchors
    except ImportError:  # pragma: no cover - older likhit pin
        return text
    return _NEEDS_OCR.sub("", strip_page_anchors(text)).strip()


#: likhit's "this document was not fully read" stamp. Kept in the file, dropped
#: from the indexed text with the page anchors.
_NEEDS_OCR = re.compile(r"<!--\s*likhit:needs-ocr[^>]*-->")


def _scrub_job_result(job) -> None:
    """Drop the transcript from the job row once it has been stored.

    **The transcript is unredacted and the jobs table is not a gated store.**
    The DB-free consumer returns the converted markdown as its result, and
    ``jobs.queue.finalize`` commits that dict into ``jobs_job.result`` BEFORE
    this hook runs — its own docstring says hooks run after the transaction
    commits. Nothing prunes that table, so without this every conversion leaves
    a permanent copy of the document's full text in a place the
    ``view_unredacted`` permission does not reach and a takedown would not think
    to look.

    What stays is what the result is actually for: which source URL was read,
    how long the text was, and its digest — enough to answer "did this run, and
    on what" from the jobs dashboard without holding the document.

    Best-effort by the same contract as the rest of this hook: a failure here
    leaves the row as the queue wrote it, which is the status quo rather than a
    new exposure.
    """
    try:
        from jobs.models import Job

        result = dict(job.result or {})
        text = result.pop("text", None)
        if text is None:
            return
        result["text_sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
        result["text_chars"] = len(text)
        result["text_scrubbed"] = True
        Job.objects.filter(pk=job.pk).update(result=result)
    except Exception as exc:  # noqa: BLE001 - scrubbing must not fail the apply
        logger.warning("could not scrub job %s result: %s", getattr(job, "pk", "?"), exc)
