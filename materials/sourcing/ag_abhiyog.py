"""Shape a scraped Attorney-General अभियोगपत्र (indictment / charge sheet) into
Material JSON-LD.

An AG indictment is an inherently SINGLE-SOURCE public document (published by one
authority — the Office of the Attorney General). It maps to
``MaterialType.CHARGE_SHEET`` (schema.org ``DigitalDocument`` +
``jawafdehi:ChargeSheet``) — the same type the CIAA importer already uses for
``AG_ABHIYOG_PATRA`` evidence (``cases.services.ciaa_draft_case_service``). This
module produces standalone charge-sheet Materials from the bulk AG scrape, keyed
under the ``ag`` IRI source segment so provenance ("came from ag.gov.np") stays
legible and re-ingest is idempotent.

Pure + DB-free: takes primitive fields (not ORM objects), returns
``(jsonld_doc, material_type)``. Persist via
``materials.single_source_ingest.upsert_single_source_material`` (which bypasses
the ≥2-publisher HOLD gate that would otherwise hold every single-source doc).
"""

from __future__ import annotations

import re
from typing import Any

from jawafdehi_shared.entities.ids import build_material_iri

from materials.jsonld import (
    MATERIAL_CONTEXT,
    MaterialType,
    media_objects_from_document_sources,
    type_for,
)

#: IRI source segment for AG indictments: ``/material/ag/<ident>``. Distinct from
#: the ``jawafdehi`` (case-source) and ``court`` segments so the archive origin is
#: legible and these don't collide with case-cited documents.
AG_SOURCE = "ag"

SOURCE_TYPE = "AG_ABHIYOG_PATRA"

#: Devanagari digit → ASCII, so a case number like ``०८२-FT-०५२४`` keeps its
#: digits in the ident (``082-ft-0524``) instead of collapsing to ``ft``. Without
#: this every "FT" case would map to the same IRI and clobber each other.
_DEVA_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")


def _slug_ident(*candidates: Any) -> str:
    """First non-empty candidate → a valid material ``ident``.

    Material ident grammar is ``[a-z0-9][a-z0-9._-]*``. Devanagari DIGITS are
    transliterated to ASCII first (so ``०८२-FT-०५२४`` → ``082-ft-0524``, not
    ``ft``); remaining non-grammar chars → ``-``, collapse repeats, trim to a
    valid leading char. Falls back through candidates so an ident is always
    produced. If a candidate has NO usable alphanumerics after coercion (e.g. a
    purely-Devanagari-consonant string), it is skipped so we fall back to the
    record id rather than emit a lossy/colliding ident.
    """
    for c in candidates:
        s = str(c or "").strip()
        if not s:
            continue
        s = s.translate(_DEVA_DIGITS).lower()
        s = re.sub(r"[^a-z0-9._-]+", "-", s)
        s = re.sub(r"-{2,}", "-", s).strip("._-")
        # require at least one alphanumeric so we don't produce an all-"-" ident
        if s and re.search(r"[a-z0-9]", s):
            return s
    return ""


def ag_abhiyog_to_jsonld(
    record: dict[str, Any],
    *,
    markdown: str | None = None,
    pdf_url: str | None = None,
    markdown_url: str | None = None,
) -> tuple[dict[str, Any], str]:
    """Shape one AG indictment → ``(jsonld_doc, material_type)``.

    ``record`` carries the scrape metadata (from the corpus index / manifest):
      * ``court_case_no`` — the AG/court case number (used for the IRI ident).
      * ``record_id``     — the AG portal record id (ident fallback).
      * ``name``          — case title / parties (Devanagari), the material name.
      * ``office``        — issuing AG office name (rides as ``publisher``).
      * ``created_date_np`` — filing date in Bikram Sambat ``YYYY-MM-DD`` (optional).
      * ``date_ad``       — pre-converted AD ISO date (optional; preferred if present).

    ``markdown`` (the likhit-converted, font-normalized full text) is embedded as
    ``data["text"] = {"ne": ...}`` so unified search indexes it WITHOUT re-OCR.
    ``pdf_url`` / ``markdown_url`` are the R2 ``contentUrl``s for the RAW pdf and
    MARKDOWN text MediaObjects (attach whichever are provided).

    Entity links are deliberately OMITTED (``about`` absent): defendant→NES
    resolution is a later enrichment pass (decision: ingest now, link later).
    """
    material_type = MaterialType.CHARGE_SHEET
    schema_type, additional_type = type_for(material_type)

    ident = _slug_ident(record.get("court_case_no"), record.get("record_id"))
    if not ident:
        raise ValueError(f"AG record has no usable ident: {record!r}")
    iri = build_material_iri(AG_SOURCE, ident)

    title = str(record.get("name") or "").strip() or (
        record.get("court_case_no") or ident
    )
    doc: dict[str, Any] = {
        "@context": MATERIAL_CONTEXT,
        "@type": schema_type,
        "@id": iri,
        "name": {"ne": str(title)},
        "jawafdehi:sourceType": SOURCE_TYPE,
    }
    if additional_type:
        doc["additionalType"] = additional_type

    case_no = str(record.get("court_case_no") or "").strip()
    if case_no:
        doc["jawafdehi:caseNumber"] = case_no

    office = str(record.get("office") or "").strip()
    if office:
        # Issuing authority. Kept as a schema.org publisher Organization node.
        doc["publisher"] = {"@type": "GovernmentOrganization", "name": {"ne": office}}

    # Filing date. The Bikram Sambat original is the authoritative Nepali date;
    # always preserve it on jawafdehi:filingDateBS. datePublished (schema.org)
    # prefers the AD conversion when available, else falls back to the BS string
    # so the date is never dropped even if BS→AD conversion was unavailable.
    date_bs = str(record.get("created_date_np") or "").strip()
    date_ad = record.get("date_ad")
    if date_bs:
        doc["jawafdehi:filingDateBS"] = date_bs
    if date_ad:
        doc["datePublished"] = str(date_ad)
    elif date_bs:
        doc["datePublished"] = date_bs

    # RAW pdf + MARKDOWN text as associatedMedia (reuse the shared shaper).
    links: list[dict[str, Any]] = []
    if pdf_url:
        links.append({"link": pdf_url, "role": "RAW"})
    if markdown_url:
        links.append({"link": markdown_url, "role": "MARKDOWN"})
    media = media_objects_from_document_sources([{"url": links}]) if links else []
    if media:
        doc["associatedMedia"] = media

    # Full text → data["text"] (Nepali), the unified-search feed. Skips re-OCR.
    if markdown and markdown.strip():
        doc["text"] = {"ne": markdown.strip()}

    return doc, material_type
