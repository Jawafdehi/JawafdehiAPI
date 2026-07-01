"""Tests for material MediaObject provenance + content-hash idempotency."""

from __future__ import annotations

import hashlib
import io

from materials.provenance import (
    PROVENANCE_KEY,
    attach_media_object,
    build_provenance,
    content_sha256,
)


class _Uploaded:
    """Minimal UploadedFile stand-in exposing chunks()/seek()."""

    def __init__(self, data: bytes):
        self._buf = io.BytesIO(data)
        self.size = len(data)
        self._data = data

    def chunks(self, chunk_size=None):
        size = chunk_size or 1024
        for i in range(0, len(self._data), size):
            yield self._data[i : i + size]

    def seek(self, pos):
        self._buf.seek(pos)


# --- content_sha256 ----------------------------------------------------------


def test_content_sha256_matches_hashlib_and_rewinds():
    data = b"%PDF-1.4 nepali scan bytes" * 1000
    up = _Uploaded(data)
    got = content_sha256(up)
    assert got == hashlib.sha256(data).hexdigest()
    # Pointer rewound so the caller can still stream the file into storage.
    assert up._buf.tell() == 0


# --- build_provenance --------------------------------------------------------


def test_build_provenance_drops_none_keeps_required():
    prov = build_provenance(fetch_method="upload", sha256="abc", source_url=None)
    assert prov["fetch_method"] == "upload"
    assert prov["sha256"] == "abc"
    assert "source_url" not in prov  # None dropped
    assert "captured_at" in prov  # defaulted


def test_build_provenance_honors_explicit_captured_at():
    prov = build_provenance(fetch_method="ocr", captured_at="2026-07-01T00:00:00")
    assert prov["captured_at"] == "2026-07-01T00:00:00"


# --- attach_media_object -----------------------------------------------------


def _doc():
    return {"@id": "https://jawafdehi.org/material/ciaa/x", "@type": "DigitalDocument"}


def test_attach_appends_media_with_provenance():
    doc = _doc()
    prov = build_provenance(sha256="h1", fetch_method="upload")
    attach_media_object(
        doc, content_url="https://cdn/a.pdf", role="RAW",
        encoding_format="application/pdf", provenance=prov,
    )
    media = doc["associatedMedia"]
    assert len(media) == 1
    assert media[0]["contentUrl"] == "https://cdn/a.pdf"
    assert media[0]["jawafdehi:linkRole"] == "RAW"
    assert media[0]["encodingFormat"] == "application/pdf"
    assert media[0][PROVENANCE_KEY]["sha256"] == "h1"


def test_attach_dedups_same_role_and_sha256():
    doc = _doc()
    p = build_provenance(sha256="same", fetch_method="upload")
    attach_media_object(doc, content_url="https://cdn/v1.pdf", role="RAW", provenance=p)
    # Re-upload identical bytes (same sha256, same role) → replaced, not doubled.
    attach_media_object(doc, content_url="https://cdn/v2.pdf", role="RAW", provenance=p)
    media = doc["associatedMedia"]
    assert len(media) == 1
    assert media[0]["contentUrl"] == "https://cdn/v2.pdf"


def test_attach_keeps_distinct_hashes_and_roles():
    doc = _doc()
    attach_media_object(
        doc, content_url="https://cdn/a.pdf", role="RAW",
        provenance=build_provenance(sha256="h1", fetch_method="upload"),
    )
    # Different bytes, same role → distinct capture, kept.
    attach_media_object(
        doc, content_url="https://cdn/b.pdf", role="RAW",
        provenance=build_provenance(sha256="h2", fetch_method="upload"),
    )
    # Same-ish but different role → kept.
    attach_media_object(
        doc, content_url="https://cdn/c.pdf", role="ALTERNATE",
        provenance=build_provenance(sha256="h1", fetch_method="upload"),
    )
    assert len(doc["associatedMedia"]) == 3


def test_attach_handles_preexisting_single_dict_media():
    doc = _doc()
    doc["associatedMedia"] = {
        "@type": "MediaObject", "contentUrl": "https://cdn/old.pdf",
        "jawafdehi:linkRole": "RAW",
    }
    attach_media_object(
        doc, content_url="https://cdn/new.pdf", role="MARKDOWN",
        provenance=build_provenance(sha256="m", fetch_method="ocr"),
    )
    media = doc["associatedMedia"]
    assert isinstance(media, list) and len(media) == 2
