"""Capture provenance for material MediaObjects (the archive-plane record).

Every file-bearing MediaObject on a Material records **how that byte-stream was
captured** — the evidentiary backbone of an accountability platform ("show me the
original, and when/how we got it"). Per the data-plane design (§3), provenance is
**Material provenance** — it rides on the MediaObject in the JSON-LD (queryable via
the JSONB GIN index, travels with the material), NOT a separate Iceberg bronze row
or a redundant R2 sidecar file: the material row in Postgres IS the record.

The provenance struct (schema.org has no faithful term, so the ``jawafdehi:``
extension namespace, consistent with ``jawafdehi:linkRole``) mirrors the fields the
dormant ``lakehouse.medallion.BronzeObject`` sketched:

    {
      "sha256":         <hex>,          # content hash of the captured bytes
      "captured_at":    <iso8601>,      # when we captured/stored it
      "fetch_method":   "upload" | "scrape" | "ocr" | ...,
      "source_url":     <str|None>,     # origin URL (None for a direct upload)
      "content_length": <int|None>,     # bytes, when known
      "tls_status":     <str|None>,     # for scrapes (GoN certs are often broken)
      "ocr_engine":     <str|None>,     # set by material_convert on the MARKDOWN link
      "ocr_confidence": <float|None>,
    }

**Content-hash idempotency:** ``attach_media_object`` dedups on ``(role, sha256)`` —
re-uploading the same bytes for the same role replaces the existing MediaObject
instead of appending a duplicate.
"""

from __future__ import annotations

import hashlib
from typing import Any, Optional

from django.utils import timezone

from . import jsonld

#: The extension key the provenance struct hangs off, on a MediaObject.
PROVENANCE_KEY = "jawafdehi:provenance"

#: Read uploads in bounded chunks so hashing a large scan never loads it all.
_HASH_CHUNK = 1024 * 1024  # 1 MiB


def content_sha256(uploaded_file: Any) -> str:
    """SHA-256 of an uploaded file's bytes, streamed in chunks.

    Resets the file pointer to 0 afterwards so the caller can still stream the
    same object into storage. Works with Django ``UploadedFile`` and any file-like
    object exposing ``chunks()`` or ``read()``.
    """
    h = hashlib.sha256()
    if hasattr(uploaded_file, "chunks"):
        for chunk in uploaded_file.chunks(chunk_size=_HASH_CHUNK):
            h.update(chunk)
    else:
        while True:
            chunk = uploaded_file.read(_HASH_CHUNK)
            if not chunk:
                break
            h.update(chunk)
    if hasattr(uploaded_file, "seek"):
        uploaded_file.seek(0)
    return h.hexdigest()


def build_provenance(
    *,
    sha256: Optional[str] = None,
    fetch_method: str,
    source_url: Optional[str] = None,
    content_length: Optional[int] = None,
    tls_status: Optional[str] = None,
    ocr_engine: Optional[str] = None,
    ocr_confidence: Optional[float] = None,
    captured_at: Optional[str] = None,
) -> dict[str, Any]:
    """Build a provenance struct, dropping keys that are ``None``.

    ``captured_at`` defaults to ``timezone.now()`` ISO-8601 when not supplied.
    ``fetch_method`` is the one required field (there is always a how).
    """
    prov: dict[str, Any] = {
        "fetch_method": fetch_method,
        "captured_at": captured_at or timezone.now().isoformat(),
    }
    optional = {
        "sha256": sha256,
        "source_url": source_url,
        "content_length": content_length,
        "tls_status": tls_status,
        "ocr_engine": ocr_engine,
        "ocr_confidence": ocr_confidence,
    }
    prov.update({k: v for k, v in optional.items() if v is not None})
    return prov


def _media_list(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """``associatedMedia`` as a list (it may be a single dict or absent)."""
    media = doc.get("associatedMedia")
    if isinstance(media, dict):
        return [media]
    if isinstance(media, list):
        return [m for m in media if isinstance(m, dict)]
    return []


def _same_capture(mo: dict[str, Any], *, role: str, sha256: Optional[str]) -> bool:
    """True when ``mo`` is the same capture — matched by ``(role, sha256)``.

    Falls back to ``contentUrl`` identity when no ``sha256`` is available on
    either side (so a URL-only re-register still dedups).
    """
    mo_role = mo.get("jawafdehi:linkRole") or "RAW"
    if mo_role != role:
        return False
    mo_sha = (mo.get(PROVENANCE_KEY) or {}).get("sha256")
    if sha256 and mo_sha:
        return mo_sha == sha256
    return False


def attach_media_object(
    doc: dict[str, Any],
    *,
    content_url: str,
    role: str,
    encoding_format: Optional[str] = None,
    provenance: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Attach a roled MediaObject (with provenance) to ``doc.associatedMedia``.

    Idempotent: if a MediaObject for the same ``(role, sha256)`` capture already
    exists it is **replaced in place** (preserving list order) rather than
    duplicated — content-hash idempotency. Otherwise the new one is appended.
    Mutates + returns ``doc``.
    """
    mo = jsonld._media_object({"link": content_url, "role": role})
    if mo is None:
        return doc
    if encoding_format and "encodingFormat" not in mo:
        mo["encodingFormat"] = encoding_format
    if provenance:
        mo[PROVENANCE_KEY] = provenance

    sha256 = (provenance or {}).get("sha256")
    media = _media_list(doc)
    for i, existing in enumerate(media):
        if _same_capture(existing, role=role, sha256=sha256):
            media[i] = mo  # replace the same capture (idempotent re-upload)
            break
    else:
        media.append(mo)
    doc["associatedMedia"] = media
    return doc
