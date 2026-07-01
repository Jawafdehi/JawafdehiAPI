"""Single-source ``Material`` upsert — the ≥2-publisher gate BYPASSED.

The bulk path (:class:`materials.bulk_ingest.MaterialBulkIngestService`)
HOLDs any material with fewer than two distinct-publisher sources. That is the
right default for general public documents, but it is WRONG for inherently
single-source documents:

  * a **court order / verdict** — published by exactly one authority (the court);
  * a **court-case record** Material — derived from one court's relational row;
  * (spec 06) a **Jawafdehi case source** — one cited document.

For those, the ≥2-source gate would HOLD every record. Rather than weaken the
gate on the bulk service (which would let genuinely multi-source materials slip
through under-vetted), this helper writes ONE material directly:
``from_jsonld`` → ``full_clean`` → ``update_or_create`` by ``@id``. It does NOT
``save()`` a fresh instance the way ``bulk_ingest._persist`` does — that would
write ``created_at=NULL`` over an existing row on UPDATE (``auto_now_add`` fires
only on INSERT). Model validation (iri/source/ident/data) still runs via
``full_clean``, the ``post_save`` ``ngm-materials`` indexer still fires, and the
upsert is idempotent by ``@id``.
"""

from __future__ import annotations

from typing import Any

from django.db import transaction

from .models import Material


def upsert_single_source_material(
    jsonld: dict[str, Any], *, material_type: str
) -> Material:
    """Validate + upsert ONE ``Material`` from a JSON-LD doc, with NO source gate.

    Reuses ``Material.from_jsonld`` (derives ``source``/``ident`` from the ``@id``
    and validates the doc) + ``full_clean`` (iri/source/ident/data agreement) +
    ``save`` (post_save → ``ngm-materials`` index). Upsert-by-``@id``: a second
    call with the same ``@id`` updates the row in place. Routed to the ``ngm`` DB.
    """
    # Validate the doc + the promoted-column agreement on a transient instance
    # (validate_unique=False — an existing ``@id`` is an UPDATE, not a duplicate).
    candidate = Material.from_jsonld(jsonld, material_type=material_type)
    candidate.full_clean(validate_unique=False)
    # Upsert by ``@id`` via update_or_create rather than a fresh-instance
    # ``save()``: a fresh instance has ``created_at=None``, and ``save()`` on a set
    # PK issues an UPDATE that would write that NULL over the existing row's
    # ``created_at`` (auto_now_add only fires on INSERT). update_or_create writes
    # only the mutable columns (so ``created_at`` is preserved, ``updated_at``
    # bumped) and still fires the ``post_save`` ngm-materials indexer.
    with transaction.atomic(using="ngm"):
        material, _ = Material.objects.using("ngm").update_or_create(
            iri=candidate.iri,
            defaults={
                "material_type": candidate.material_type,
                "source": candidate.source,
                "ident": candidate.ident,
                "data": candidate.data,
            },
        )
    return material
