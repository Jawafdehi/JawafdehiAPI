"""The single ``Material`` upsert primitive — validate + upsert one doc by ``@id``.

Every write to a ``Material`` funnels through :func:`upsert_single_source_material`:
the API plane (``POST``/``PUT``/``/file`` — see :func:`materials.views._upsert_material`),
the court-case importer, and the Jawafdehi case-source ingest. Keeping one
primitive means one answer to how an upsert behaves — the DB alias, validation,
``created_at`` preservation, indexing, and the soft-delete policy are decided
here, once, rather than re-implemented (and drifting) per call site.

Behavior:
- ``from_jsonld`` derives ``source``/``ident`` from the ``@id`` and validates the
  doc; ``full_clean`` checks iri/source/ident/data agreement.
- Upsert-by-``@id`` via ``update_or_create`` (NOT a fresh-instance ``save()``,
  which would write ``created_at=NULL`` over an existing row on UPDATE, since
  ``auto_now_add`` fires only on INSERT). Only the mutable columns are written,
  so ``created_at`` is preserved and ``updated_at`` bumped.
- **Soft-delete policy — REVIVE.** A re-upsert of a soft-deleted ``@id`` clears
  ``is_deleted`` (it is in ``defaults``): the incoming write is the source of
  truth. This is the platform-wide rule (overwrites are permitted; re-sourcing a
  previously-taken-down document republishes it). Reviving via ``defaults`` is
  also what keeps the ``post_save`` indexer correct — a row left
  ``is_deleted=True`` would be silently evicted from the search index on save.
- Routed to the ``ngm`` DB (where ``Material`` lives) under one transaction; the
  ``post_save`` ``ngm-materials`` indexer fires. Idempotent by ``@id``.

There is no ≥2-publisher HOLD gate on this path: gating is a sourcing-policy
decision made before the write (an inherently single-source document — a court
order, a charge sheet, a law-journal precedent — has exactly one authority), not
a property of the upsert.
"""

from __future__ import annotations

from typing import Any

from django.db import transaction

import logging

from .redaction import TEXT_KEY, keep_original, redact_jsonld

from .models import Material

logger = logging.getLogger("materials.ingest")


def _removals(counts) -> dict:
    """Only the counters that mean something was REMOVED.

    likhit's redactors also return table diagnostics and refusals on every call
    — ``table_blocks_seen``, ``physical_cells``, ``*_refused_*`` — so logging
    the raw Counter reports "redacted" for a document with nothing in it, which
    makes the one signal that redaction did anything useless.
    """
    return {k: v for k, v in counts.items() if k.endswith("_redacted") and v}


def upsert_single_source_material(
    jsonld: dict[str, Any], *, material_type: str, visibility_policy: str | None = None
) -> Material:
    """Validate + upsert ONE ``Material`` from a JSON-LD doc by ``@id``.

    The single upsert primitive (see the module docstring): idempotent by ``@id``,
    preserves ``created_at``, fires the ngm-materials indexer, and REVIVES a
    soft-deleted row (``is_deleted=False`` in ``defaults`` — the write wins).

    ``visibility_policy`` (see ``materials.models.Policy``): when omitted, a NEW
    row is born at ``default_policy_for(source)`` (corpus→PUBLIC,
    jawafdehi-upload→CASE_GATED) via ``create_defaults`` — INSERT-only, so
    re-sourcing a document NEVER clobbers a caseworker's manual policy. When
    supplied it is an explicit override, applied on both create AND update (it
    also lands in ``defaults``). Note: the cached ``visibility`` still needs a
    recompute afterwards to reflect the policy — the API views do this; bulk
    importers rely on ``recompute_all`` / the case-side trigger.
    """
    # Validate the doc + the promoted-column agreement on a transient instance
    # (validate_unique=False — an existing ``@id`` is an UPDATE, not a duplicate).
    # REDACT HERE, not in the callers. This is the single primitive every write
    # funnels through — the convert job, four scrapers, the upload endpoint and
    # every PATCH — so redacting at this boundary is what makes "the public text
    # is redacted" true by construction rather than true of whichever writer
    # remembered. It was previously done inside the convert job alone, which
    # left the AG charge-sheet shelf (scraper-ingested, and the highest-PII
    # corpus we hold) publishing identifiers into the field the search indexer
    # reads.
    #
    # The text as it arrived is kept on the row, outside ``data``, and only when
    # redaction changed it — see Material.source_text. Transcription is
    # expensive and not reproducible byte-for-byte, so the original is preserved
    # rather than re-derivable: nobody should have to OCR a document twice to
    # answer a question about what it said.
    jsonld, source_text, counts = redact_jsonld(jsonld)
    removals = _removals(counts)
    if removals:
        # Guarded on the REMOVALS, not on the raw Counter: likhit's redactors
        # return table diagnostics on every call, so `if counts:` is true for
        # every document ever ingested and the one signal that redaction did
        # something would fire with `{}` beside it.
        logger.info("redacted %s on ingest: %s", jsonld.get("@id"), removals)
    candidate = Material.from_jsonld(jsonld, material_type=material_type)
    # from_jsonld already set the source-based birth default; an explicit override
    # wins. Assign it BEFORE full_clean so an invalid policy is REJECTED by the
    # choices check rather than silently persisted (this is the single validation
    # point for a direct caller that bypasses the API's _clean_policy).
    if visibility_policy is not None:
        candidate.visibility_policy = visibility_policy
    candidate.full_clean(validate_unique=False)
    # Mutable columns written on every upsert (create AND update). Reviving on
    # re-upsert: an incoming write to a soft-deleted @id republishes it — omitting
    # is_deleted would overwrite the row's data while leaving it hidden AND
    # trigger an index eviction on save.
    mutable = {
        "material_type": candidate.material_type,
        "source": candidate.source,
        "ident": candidate.ident,
        "data": candidate.data,
        "is_deleted": False,
    }
    # An explicit override applies on update too; the implicit birth default must
    # NOT (it would reset a caseworker's manual policy every re-ingest), so it
    # goes only into create_defaults (candidate.visibility_policy is the single,
    # already-validated source of truth for both).
    if visibility_policy is not None:
        mutable["visibility_policy"] = candidate.visibility_policy
    create_defaults = {**mutable, "visibility_policy": candidate.visibility_policy}
    with transaction.atomic(using="ngm"):
        # The stored original is decided against what is ALREADY on the row, so
        # it needs the existing row in hand — a caseworker who reads the
        # redacted representation and writes it back must not blank the only
        # copy of the original. See redaction.keep_original.
        existing = (
            Material.objects.using("ngm")
            .filter(pk=candidate.iri)
            .values("data", "source_text")
            .first()
        )
        candidate.source_text = keep_original(
            incoming=source_text,
            stored=(existing or {}).get("source_text") or "",
            text_changed=(existing or {}).get("data", {}).get(TEXT_KEY)
            != candidate.data.get(TEXT_KEY),
        )
        mutable["source_text"] = candidate.source_text
        create_defaults["source_text"] = candidate.source_text
        material, _ = Material.objects.using("ngm").update_or_create(
            iri=candidate.iri,
            defaults=mutable,
            create_defaults=create_defaults,
        )
    return material
