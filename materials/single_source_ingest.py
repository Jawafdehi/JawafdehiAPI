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

from .models import Material, default_policy_for


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
    candidate = Material.from_jsonld(jsonld, material_type=material_type)
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
    # An explicit override applies on update too; an implicit default must NOT (it
    # would reset a caseworker's manual policy every re-ingest), so it goes only
    # into create_defaults.
    if visibility_policy is not None:
        mutable["visibility_policy"] = visibility_policy
    create_defaults = {
        **mutable,
        "visibility_policy": visibility_policy or default_policy_for(candidate.source),
    }
    with transaction.atomic(using="ngm"):
        material, _ = Material.objects.using("ngm").update_or_create(
            iri=candidate.iri,
            defaults=mutable,
            create_defaults=create_defaults,
        )
    return material
