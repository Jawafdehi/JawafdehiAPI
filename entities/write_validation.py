"""Write-payload normalization + validation for the JSON-LD NES write surface.

CLEAN-SLATE design (2026-06-28): the write API accepts schema.org JSON-LD keyed
by an ``@id`` IRI, OR a simple *authoring shape* that this module normalizes into
that JSON-LD before storage. Validation is the minimal JSON-LD validator
(``entities.validation``); there are no per-type Pydantic request bodies.

Authoring shape (all the JSON-LD shorthand a contributor needs)::

    {
      "prefix": "person",          # OR "entity_prefix"
      "slug": "ram-bahadur",
      "type": "Person",            # @type (string or list); OR "@type"
      "name": {"en": "Ram Bahadur", "ne": "राम बहादुर"},   # OR a plain string
      ... any other schema.org / jawafdehi: props ...
    }

A full JSON-LD document (already carrying ``@id``/``@type``/``@context``) is
accepted as-is.
"""

from __future__ import annotations

from typing import Any, Dict, List

from jawafdehi_shared.entities.ids import build_entity_iri

from .validation import JAWAFDEHI_NS, validate_jsonld_entity

# JSON-LD @context emitted on normalized documents (schema.org default vocab +
# the jawafdehi: extension namespace + language-map containers for bilingual text).
JSONLD_CONTEXT: List[Any] = [
    "https://schema.org",
    {
        "jawafdehi": JAWAFDEHI_NS,
        "name": {"@id": "schema:name", "@container": "@language"},
        "alternateName": {"@id": "schema:alternateName", "@container": "@language"},
        "description": {"@id": "schema:description", "@container": "@language"},
    },
]

# JSON Pointer prefixes that may never be touched by a PATCH. The @id is the
# immutable identity (the IRI is the join key other services store); @context and
# version provenance are owned by the publication service.
#
# @type is intentionally NOT blocked (BB-10): a mis-typed entity (e.g. a Location
# authored as a Person) must be correctable in place. @type is not part of the
# IRI — the identity is prefix+slug (jawafdehi_shared.entities.ids), and the
# promoted ``entity_type`` column is re-derived from the doc's @type on every
# write — so a re-typed doc stays consistent and breaks no inbound references. The
# patched doc is still re-run through ``validate_jsonld_entity``, which rejects an
# unknown @type.
PATCH_BLOCKED_PATH_PREFIXES = frozenset(
    {
        "/@id",
        "/@context",
        "/jawafdehi:version",
    }
)

_ALLOWED_OPS = {"add", "remove", "replace", "move", "copy", "test"}

# Authoring keys consumed to build identity; not copied verbatim into the doc.
_AUTHORING_KEYS = {"prefix", "entity_prefix", "slug", "type", "@type", "change_description"}


def is_blocked_patch_path(path: str) -> bool:
    """True if a JSON pointer path targets an immutable/reserved field.

    RFC-6901 escapes ``~`` as ``~0`` and ``/`` as ``~1``; a JSON-LD key like
    ``@id`` appears literally in a pointer (``/@id``), so a direct prefix match
    is correct here.
    """
    return any(
        path == blocked or path.startswith(blocked + "/")
        for blocked in PATCH_BLOCKED_PATH_PREFIXES
    )


def normalize_authoring_payload(body: Dict[str, Any]) -> Dict[str, Any]:
    """Turn an authoring payload (or a full JSON-LD doc) into a JSON-LD document.

    - If ``body`` already has ``@id``, treat it as full JSON-LD (only inject a
      default ``@context`` if missing).
    - Otherwise require ``prefix``/``entity_prefix`` + ``slug`` + ``type``/``@type``
      and build the ``@id`` IRI, ``@type``, ``@context``; copy every remaining key
      through verbatim (free-form schema.org / jawafdehi: properties).

    Raises ``ValueError`` on a structurally impossible identity (bad prefix/slug).
    Minimal field validation is the caller's job via ``validate_jsonld_entity``.
    """
    if not isinstance(body, dict):
        raise ValueError("Entity payload must be a JSON object.")

    if "@id" in body:
        doc = dict(body)
        doc.pop("change_description", None)
        doc.setdefault("@context", JSONLD_CONTEXT)
        return doc

    prefix = body.get("prefix") or body.get("entity_prefix")
    slug = body.get("slug")
    atype = body.get("@type") or body.get("type")
    if not prefix:
        raise ValueError("prefix (or entity_prefix) is required")
    if not slug:
        raise ValueError("slug is required")
    if not atype:
        raise ValueError("type (@type) is required")

    iri = build_entity_iri(prefix, slug)  # validates prefix/slug shape

    doc: Dict[str, Any] = {
        "@context": JSONLD_CONTEXT,
        "@type": atype,
        "@id": iri,
    }
    for key, value in body.items():
        if key in _AUTHORING_KEYS:
            continue
        doc[key] = value
    return doc


def validate_create_payload(body: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize + minimally validate a CREATE payload; return the JSON-LD doc.

    Raises ``ValueError`` (mapped to 422 by the view) on any failure.
    """
    doc = normalize_authoring_payload(body)
    validate_jsonld_entity(doc)
    return doc


def normalize_patch_ops(raw_ops: Any) -> List[Dict[str, Any]]:
    """Validate + normalize an RFC-6902 patch list.

    Returns the cleaned op list ready for ``jsonpatch.apply_patch``. Raises
    ``ValueError`` on a malformed op or a blocked-path (immutable @id/@type) target.
    """
    if not isinstance(raw_ops, list) or not raw_ops:
        raise ValueError("patch_ops must be a non-empty list.")

    normalized: List[Dict[str, Any]] = []
    for raw in raw_ops:
        if not isinstance(raw, dict):
            raise ValueError("Each patch op must be an object.")
        op = str(raw.get("op", "")).lower()
        if op not in _ALLOWED_OPS:
            raise ValueError(
                f"Unsupported patch operation '{raw.get('op')}'. "
                f"Allowed ops: {sorted(_ALLOWED_OPS)}"
            )
        path = raw.get("path")
        if not isinstance(path, str) or not path.startswith("/"):
            raise ValueError("path must be a valid JSON Pointer starting with '/'.")

        from_path = raw.get("from")
        if op in {"move", "copy"}:
            if not isinstance(from_path, str) or not from_path.startswith("/"):
                raise ValueError(f"'{op}' operation requires a valid 'from' pointer.")
        if op in {"add", "replace", "test"} and "value" not in raw:
            raise ValueError(f"'{op}' operation requires 'value'.")

        if op in {"add", "remove", "replace", "move", "copy"}:
            if is_blocked_patch_path(path):
                raise ValueError(f"Patching path '{path}' is not allowed.")
            if op in {"move", "copy"} and isinstance(from_path, str):
                if is_blocked_patch_path(from_path):
                    raise ValueError(f"Patching path '{from_path}' is not allowed.")

        clean: Dict[str, Any] = {"op": op, "path": path}
        if "value" in raw:
            clean["value"] = raw["value"]
        if from_path is not None:
            clean["from"] = from_path
        normalized.append(clean)

    return normalized
