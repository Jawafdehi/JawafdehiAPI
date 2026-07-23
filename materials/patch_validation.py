"""Immutable-path policy for the material field-level PATCH.

The materials write plane has two verbs against a stored document: ``PUT``
replaces ``data`` wholesale, and ``PATCH`` edits it in place. ``PATCH`` is the
narrower verb, so it must not become a back door to anything ``PUT`` guards —
identity, the promoted columns derived from the doc, or the server-owned
visibility annotations. This module is that guard list; the op grammar itself is
shared (:mod:`jawafdehi_shared.jsonpatch_ops`).
"""

from __future__ import annotations

from jawafdehi_shared.jsonpatch_ops import blocked_path_predicate

#: Envelope/annotation keys that must NEVER be persisted into a Material's stored
#: JSON-LD ``data``: the write-envelope control field ``visibility_policy`` and
#: the authed-read annotations ``jawafdehi:visibility``/``jawafdehi:visibilityPolicy``.
#: ``PUT`` strips them at the write chokepoint (``_upsert_material``); ``PATCH``
#: rejects them outright, because a patch has no "strip" semantics — silently
#: dropping the op would report success for a write that did not happen.
RESERVED_WRITE_KEYS = frozenset(
    {"visibility_policy", "jawafdehi:visibility", "jawafdehi:visibilityPolicy"}
)

#: JSON Pointer prefixes a material PATCH may never target.
#:
#: * ``/@id`` — the material IRI is the identity. It is the join key cases store
#:   (``CaseMaterialReference.material_iri``), it is the row's primary key, and
#:   the promoted ``source``/``ident`` columns are parsed out of it. Repointing it
#:   would orphan every inbound reference rather than rename anything.
#: * ``/@context`` — owned by the platform's JSON-LD vocabulary, not per-document.
#: * ``/@type`` + ``/additionalType`` — unlike the NES entity plane (which
#:   deliberately allows re-typing because ``entity_type`` is re-derived on every
#:   write), ``Material.material_type`` is passed to ``from_jsonld`` by the caller
#:   and is NOT re-derived here. Patching the doc's type alone would silently
#:   drift the column away from the document. Re-typing a material needs a
#:   deliberate write that moves both together — that is ``PUT``, not this.
#: * the reserved keys above.
#:
#: Deliberately NOT blocked: ordinary content (``name``, ``jawafdehi:caseNumber``,
#: ``datePublished``, ``publisher``, ``associatedMedia``, ``text``, …). The
#: patched document is re-run through ``validate_material_jsonld``, so a patch
#: that strips a required field is rejected on the way out rather than enumerated
#: here.
PATCH_BLOCKED_PATH_PREFIXES = frozenset(
    {"/@id", "/@context", "/@type", "/additionalType"}
    | {f"/{key}" for key in RESERVED_WRITE_KEYS}
)

#: ``is_blocked`` predicate for :func:`jawafdehi_shared.jsonpatch_ops.normalize_patch_ops`.
is_blocked_patch_path = blocked_path_predicate(PATCH_BLOCKED_PATH_PREFIXES)

#: Max operations in one patch request.
#:
#: Django's ``DATA_UPLOAD_MAX_MEMORY_SIZE`` does NOT bound this path — DRF's
#: ``JSONParser`` reads the WSGI stream directly rather than through
#: ``HttpRequest.body``, which is where Django's check lives (measured: a 3.6 MB
#: body against the 2.5 MB default was accepted and persisted). So PATCH was the
#: only Material write with no ceiling, while the upload endpoint beside it is
#: capped at ``views._MAX_UPLOAD_BYTES``. 1000 is far above any real edit — the
#: AG backfill sends exactly one op per record.
MAX_PATCH_OPS = 1000

#: Max declared ``Content-Length`` of a PATCH request body.
#:
#: The guards below both run AFTER DRF's ``JSONParser`` has read and parsed the
#: whole stream, so neither bounds the memory the parse itself costs: one op
#: carrying a 200 MB string is fully materialized before the document ceiling
#: rejects it. Checking ``Content-Length`` is the only guard that can refuse a
#: body while it is still on the wire.
#:
#: Sized as the document ceiling plus 1 MB of headroom, so the guard can never
#: refuse a patch that would have produced a legal document: the largest
#: legitimate body is a single op replacing the whole doc, i.e. the document
#: plus a JSON-Patch envelope of a few dozen bytes.
MAX_PATCH_BODY_BYTES = 5 * 1024 * 1024 + 1024 * 1024

#: Max serialized size of the stored JSON-LD document AFTER a patch is applied.
#:
#: A Material's ``data`` is read on every detail hit, fed wholesale to the
#: OpenSearch indexer on every write, and loaded in full by
#: ``visibility.recompute_all``, so an unbounded row degrades all three. 5 MB is
#: roughly an order of magnitude above a long indictment's embedded full text
#: (~400 KB of Nepali), so it bounds abuse without touching real documents.
MAX_MATERIAL_DOC_BYTES = 5 * 1024 * 1024
