"""Shape a PPMO publication (procurement bulletin / annual report / policy doc)
into Material JSON-LD — an ``OFFICIAL_REPORT``.

Pure + DB-free: takes what the crawler scraped off a ``ppmo.gov.np/content/{id}/``
page (the attached PDF url + the page title) and returns ``(jsonld_doc,
material_type)`` ready to POST to ``/api/materials/``.

**Text is deliberately NOT set here.** PPMO's bulletins are SCANNED IMAGE PDFs, so
extracting their Nepali text needs LLM-vision OCR (likhit → markitdown-ocr, which
requires a vision API key). This shaper ingests the *document* — title, date, the
PDF as ``associatedMedia``, provenance — so it is discoverable by metadata now; the
transcript lands later via a deferred enrichment pass that PATCHes ``text``.
"""

from __future__ import annotations

import html as html_lib
import re
from typing import Any

from jawafdehi_shared.entities.ids import build_material_iri
from materials.jsonld import MATERIAL_CONTEXT, MaterialType, type_for

#: IRI ``source`` segment for a PPMO publication (``/material/ppmo/<content_id>``).
PPMO_SOURCE = "ppmo"

#: Provenance authority (the publisher key for the ≥2-source gate).
PPMO_AUTHORITY = "ppmo.gov.np"

PPMO_BASE = "https://www.ppmo.gov.np"

#: PPMO attaches its documents on the shared government CDN (GIWMS).
CDN_PDF_RE = re.compile(r"https?://giwmscdnone\.gov\.np/[^\s\"'<>]+\.pdf", re.I)
_OG_TITLE_RE = re.compile(r'og:title["\s]+content="([^"]{3,160})"', re.I)
_TITLE_RE = re.compile(r"<title>([^<]{3,160})</title>", re.I)

#: The site appends its own name to every <title>; strip it so the material name is
#: the document's own title (``… | Public Procurement Monitoring Office``).
_TITLE_SUFFIX_RE = re.compile(r"\s*\|\s*Public Procurement Monitoring Office\s*$", re.I)


def extract_pdf_urls(html: str) -> list[str]:
    """Every distinct CDN PDF url on a PPMO content page, in document order."""
    seen: dict[str, None] = {}
    for url in CDN_PDF_RE.findall(html or ""):
        seen.setdefault(url, None)
    return list(seen)


def extract_title(html: str) -> str:
    """The document's title (og:title preferred), with the site suffix stripped.

    HTML entities are unescaped — the raw markup carries e.g. ``&amp;``, which would
    otherwise be stored literally in the material name ("e-GP Handsout &amp; …").
    """
    m = _OG_TITLE_RE.search(html or "") or _TITLE_RE.search(html or "")
    if not m:
        return ""
    title = html_lib.unescape(m.group(1))
    return _TITLE_SUFFIX_RE.sub("", title).strip()


def _is_devanagari(text: str) -> bool:
    """True if the text carries Devanagari — PPMO titles are usually Nepali."""
    return any("ऀ" <= ch <= "ॿ" for ch in text or "")


def ppmo_report_to_jsonld(
    content_id: int | str, pdf_url: str, title: str = ""
) -> tuple[dict[str, Any], str]:
    """Shape one PPMO publication → ``(jsonld_doc, material_type)``.

    ``@id`` is keyed on the site's own ``/content/{id}/`` id (its stable primary
    key), so a re-crawl upserts the same material rather than duplicating it. The
    title is language-tagged by script (Devanagari → ``ne``, else ``en``) since
    PPMO publishes mostly Nepali titles.
    """
    material_type = MaterialType.OFFICIAL_REPORT
    schema_type, additional_type = type_for(material_type)
    iri = build_material_iri(PPMO_SOURCE, str(content_id))
    source_url = f"{PPMO_BASE}/content/{content_id}/"
    name = (title or "").strip() or f"PPMO Publication {content_id}"

    doc: dict[str, Any] = {
        "@context": MATERIAL_CONTEXT,
        "@type": schema_type,
        "@id": iri,
        # Language-tag by script so Nepali titles index as `ne`, not mislabeled `en`.
        "name": {"ne": name} if _is_devanagari(name) else {"en": name},
        "identifier": str(content_id),
        "url": source_url,
        "isAccessibleForFree": True,
        "publisher": {
            "@type": "GovernmentOrganization",
            "name": {
                "en": "Public Procurement Monitoring Office",
                "ne": "सार्वजनिक खरिद अनुगमन कार्यालय",
            },
        },
        # The scanned PDF is the authoritative artifact; text arrives via the
        # deferred OCR enrichment (see the module docstring).
        "associatedMedia": [
            {
                "@type": "MediaObject",
                "contentUrl": pdf_url,
                "encodingFormat": "application/pdf",
            }
        ],
        "sources": [{"url": source_url, "authority": PPMO_AUTHORITY}],
    }
    if additional_type:
        doc["additionalType"] = additional_type
    return doc, material_type
