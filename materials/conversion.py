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

- :func:`enqueue_material_convert` — transactional enqueue (dedup on the IRI) from
  a file-bearing write path (the upload endpoint, ingestion).
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
import logging
from typing import Any, Optional

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


def build_convert_payload(job) -> Optional[dict]:
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
    from django.core.files.base import ContentFile
    from django.db import transaction

    from jawafdehi_shared.storage import store_file_as_link

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

    # 1) Land the markdown in R2 as a roled MARKDOWN link (hashed filename).
    # Done OUTSIDE the DB transaction below — a network round-trip must not hold a
    # row lock. NOTE: store_file_as_link uses FILE_STORAGE_PREFIX (default
    # "case_uploads/"), so the .md currently lands under that shared prefix. The
    # material-specific R2 layout (docs/data-plane-design.md §3) is folded in with
    # the provenance work (§8 item 2); until then the link is correct, just not
    # prefix-isolated.
    md_file = ContentFile(text.encode("utf-8"), name="material.md")
    link = store_file_as_link(md_file, role="MARKDOWN")

    # 2) Read-modify-write the JSONB doc under a row lock so a concurrent write
    # (e.g. a re-upload upserting the same @id) can't clobber our text, or ours
    # theirs — the whole ``data`` blob is rewritten, so a naive read+save would
    # lose the other writer's fields (lost-update).
    with transaction.atomic():
        row = (
            Material.objects.select_for_update()
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
        media = [
            mo
            for mo in _media_list(data)
            if (mo.get("jawafdehi:linkRole") or "RAW") != "MARKDOWN"
        ]
        md_mo = jsonld._media_object({"link": link["link"], "role": "MARKDOWN"})
        if md_mo is not None:
            # Provenance: this markdown was produced by OCR/likhit conversion from
            # the source doc (data-plane §3 — provenance rides on the MediaObject).
            md_mo["encodingFormat"] = "text/markdown"
            md_mo[provenance.PROVENANCE_KEY] = provenance.build_provenance(
                sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                fetch_method="ocr",
                source_url=(result or {}).get("source_url"),
                content_length=len(text.encode("utf-8")),
                ocr_engine="likhit",
            )
            media.append(md_mo)
        data["associatedMedia"] = media
        # The searchable full text (language-mapped, per MATERIAL_CONTEXT).
        data["text"] = {_TEXT_LANG: text}

        row.data = data
        row.save(update_fields=["data", "updated_at"])  # post_save → reindex
    logger.info(
        "material_convert %s: stored %d chars markdown + text (from %s)",
        iri,
        len(text),
        (result or {}).get("source_url"),
    )
