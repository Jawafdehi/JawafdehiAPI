"""Storing a transcript and describing it as a MediaObject — one copy, two writers.

These three functions were private to :mod:`materials.conversion` while it was
the only thing that produced a transcript. The backfill sweep now produces one
too — for every document converted before redaction existed, whose
``REDACTED_MARKDOWN`` was never written — and that is exactly the situation the
docstring on :func:`media` already warns about: the redacted MediaObject was once
assembled by a near-copy of the original's block, and the two drifted until the
redacted one was being stripped unconditionally and re-added conditionally.

So they live here, shared, rather than being copied a second time.

🛑 **The object key follows the CONTENT, not the file name.** Both writers pass a
``content_hash``; see :func:`jawafdehi_shared.storage.store_file_as_link` for what
happens otherwise — a per-role constant name had seventeen production materials
resolving to a single ``.md`` URL that each conversion overwrote.
"""

from __future__ import annotations

import hashlib
from typing import Any

from . import artifacts, jsonld, provenance

#: Roles this module owns. A writer replacing one replaces the pair, because a
#: redacted transcript left beside a *different* original is worse than neither:
#: ``materials.artifacts.is_withheld`` withholds the original once a redacted
#: counterpart exists, so a stale pairing publishes a redaction of a document we
#: are no longer serving.
OUR_ROLES = frozenset({artifacts.ORIGINAL_TRANSCRIPT_ROLE, artifacts.REDACTED_TRANSCRIPT_ROLE})


def store(text: str, role: str, name: str) -> dict:
    """Put one transcript in object storage, keyed on its own bytes."""
    from django.core.files.base import ContentFile

    from jawafdehi_shared.storage import store_file_as_link

    raw = text.encode("utf-8")
    return store_file_as_link(
        ContentFile(raw, name=name),
        role=role,
        content_hash=hashlib.sha256(raw).hexdigest(),
    )


def media(
    link: dict, role: str, text: str, source_url: Any, *, derived: bool
) -> dict | None:
    """One stored transcript as a roled MediaObject carrying its provenance.

    ``derived`` is the honest half: a redacted copy was not produced by OCR-ing
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


def replace(existing: list[dict], fresh: list[dict | None]) -> list[dict]:
    """``existing`` with the roles ``fresh`` carries replaced by ``fresh``.

    Dedup is by ROLE, not by content hash: a transcript is our own singular
    derived output, so a second one of the same role is a replacement rather than
    an addition.

    🛑 **The drop set comes from ``fresh``, not from :data:`OUR_ROLES`.** Two
    callers write different subsets — the convert job rebuilds both transcripts,
    the backfill sweep adds only the redacted one to a row whose original it has
    not touched — so dropping the pair unconditionally deletes the surviving
    caller's `MARKDOWN` link outright. It is also the shape of the bug this
    module's own history records: the redacted object was once *stripped
    unconditionally and re-added only conditionally*, and deriving the drop set
    from what is actually being written makes that unrepresentable.

    ``None`` entries are skipped — :func:`media` returns one for a link the
    JSON-LD layer refuses — and skipping one therefore also leaves its previous
    version in place rather than removing a link we could not rebuild.
    """
    writing = [mo for mo in fresh if mo is not None]
    # ``role_of`` rather than a raw ``.get``: contentUrl and role both come out of
    # free-form JSONB, where a role can be absent, empty or not a string at all.
    # It normalises every one of those to "RAW", which this module refuses below —
    # so an unroled object fails closed instead of matching nothing and surviving.
    roles = {artifacts.role_of(mo) for mo in writing}
    unknown = roles - OUR_ROLES
    if unknown:
        # A caller reaching this with, say, RAW would silently drop the
        # publisher's own link — which this module has no business touching.
        raise ValueError(f"not a transcript role: {sorted(unknown)}")
    kept = [mo for mo in existing if artifacts.role_of(mo) not in roles]
    kept.extend(writing)
    return kept
