"""Validation and encoding for user-controlled API path components."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from jawafdehi_shared.entities.ids import (
    build_entity_iri,
    build_material_iri,
    canonicalize_entity_iri,
    is_valid_case_iri,
)


def encode_path_segment(value: Any, *, label: str = "identifier") -> str:
    """Encode one opaque route segment after rejecting route separators."""
    raw = str(value).strip()
    if not raw:
        raise ValueError(f"{label} must not be empty.")
    if len(raw) > 300:
        raise ValueError(f"{label} is too long.")
    has_separator = any(char in raw for char in ("/", "\\", "?", "#"))
    has_control = any(ord(char) < 32 for char in raw)
    if raw in {".", ".."} or has_separator or has_control:
        raise ValueError(f"{label} contains an invalid path component.")
    return quote(raw, safe="")


def encode_case_slug(value: Any) -> str:
    """Validate a Jawafdehi case slug against the canonical shared grammar."""
    slug = str(value).strip()
    probe = f"https://jawafdehi.invalid/case/{slug}"
    if not is_valid_case_iri(probe, any_host=True):
        raise ValueError("slug is not a valid Jawafdehi case slug.")
    return quote(slug, safe="")


def encode_entity_ref(value: Any) -> str:
    """Validate an entity IRI or prefix/slug reference and encode it opaquely."""
    ref = str(value).strip()
    if ref.startswith(("http://", "https://")):
        ref = canonicalize_entity_iri(ref)
    else:
        prefix, separator, slug = ref.rpartition("/")
        if not separator:
            raise ValueError("entity reference must be an IRI or prefix/slug.")
        ref = build_entity_iri(prefix, slug)
    return quote(ref, safe="")


def encode_material_parts(source: Any, ident: Any) -> tuple[str, str]:
    """Validate material source/ident values against their canonical grammar."""
    source_value = str(source).strip()
    ident_value = str(ident).strip()
    build_material_iri(source_value, ident_value)
    return quote(source_value, safe=""), quote(ident_value, safe="")
