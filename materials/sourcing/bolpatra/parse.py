"""Parse the bolpatra e-GP (``bolpatra.gov.np/egp``) public tender pages.

The pure parse half of the ``bolpatra`` sourcing pipeline. e-GP is a legacy
server-rendered Java/Struts app (NOT a JSON API), so this is HTML→dataclass
extraction, mirroring the court-portal parsers:

- **Search results** (``POST /egp/searchOpportunity.action``) list rows whose
  detail links call ``getTenderDetails('<tenderId>')`` — :func:`extract_tender_ids`
  harvests those integer ids (the crawl frontier).
- **Tender detail** (``POST /egp/getTenderDetails`` with ``tenderId``) is a
  label/value page — :func:`parse_tender_detail` maps the labels (verified against
  live tenders 319751 / 321065) to a :class:`ParsedTender`.

Pure + dependency-light: uses ``lxml`` (a declared repo dep) and the shared
``normalize_whitespace``; no network, no Django.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from courts.scraper.text import normalize_whitespace

#: getTenderDetails('319751') / getTenderDetails("319751") in the results HTML.
_TENDER_ID_RE = re.compile(r"getTenderDetails\(\s*['\"](\d+)['\"]\s*\)")

#: Detail-page labels → the ParsedTender field they populate. Matched on the
#: normalized (whitespace-collapsed, trailing-colon-stripped) label text.
_LABEL_MAP = {
    "Public Entity Name": "public_entity",
    "Procurement Category": "procurement_category",
    "Procurement Method": "procurement_method",
    "IFB/RFP/EOI/PQ No": "notice_number",
    "Project Name": "project_name",
    "Current Status": "current_status",
    "Source of Funds": "source_of_funds",
    "Source of Fund": "source_of_funds",
    "Notice Publication Date": "publication_date",
    "Brief Description of Work": "description",
    "Last Date for Bid Submission": "submission_deadline",
    "Bid Opening Date": "opening_date",
    "Date of Pre-Bid Meeting": "prebid_meeting_date",
}

#: Every label the detail page renders — the mapped ones plus the section headings
#: and unmapped rows. Used as a "this is a label, not a value" guard so an empty
#: field never absorbs the NEXT label as its value (see parse_tender_detail).
_KNOWN_LABELS = frozenset(_LABEL_MAP) | frozenset(
    {
        "View IFB/PQ/EOI/EF Notice Details",
        "Bid Information",
        "Bidding Procedure",
        "Bidding Document of",
        "Bidding Type",
        "Tender Type",
        "Funding Information",
        "Bid Schedule",
        "Bid Document Publication Date",
        "Pre-Bid Meeting Address",
        "Bid Submission Address",
        "Bid Opening Address",
        "Date of Start of Works",
        "Bid Fee Details",
        "Bid Document Fee (in NPR)",
        "Bank Name",
    }
)


@dataclass
class ParsedTender:
    """One e-GP tender/bid notice, mapped from its detail page."""

    tender_id: str
    public_entity: str | None = None
    notice_number: str | None = None
    project_name: str | None = None
    procurement_category: str | None = None
    procurement_method: str | None = None
    current_status: str | None = None
    source_of_funds: str | None = None
    publication_date: str | None = None
    submission_deadline: str | None = None
    opening_date: str | None = None
    prebid_meeting_date: str | None = None
    description: str | None = None


def extract_tender_ids(html: str) -> list[str]:
    """Harvest tender ids from a search-results page (dedup, order-preserving)."""
    seen: dict[str, None] = {}
    for m in _TENDER_ID_RE.finditer(html or ""):
        seen.setdefault(m.group(1), None)
    return list(seen)


def _clean_label(text: str) -> str:
    """Normalize a label cell: collapse whitespace, drop a trailing colon."""
    return normalize_whitespace(text).rstrip(":").strip()


def parse_tender_detail(html: str, tender_id: str) -> ParsedTender | None:
    """Map a ``getTenderDetails`` page to a :class:`ParsedTender`, or ``None``.

    The page renders as label/value cell pairs. We walk every table row and every
    ``<td>`` sequence, pairing a recognized label cell with the following non-empty
    value cell. Returns ``None`` only when the page yields no usable fields at all
    (an error/empty page) so the caller can retry rather than cache a hollow stub.
    """
    try:
        from lxml import html as lxml_html
    except ImportError:  # pragma: no cover - lxml is a declared dependency
        return None
    if not html:
        return None

    doc = lxml_html.fromstring(html)
    # Flatten to the visible text of each cell, in document order.
    cells = [normalize_whitespace(td.text_content()) for td in doc.xpath("//td")]
    tender = ParsedTender(tender_id=str(tender_id))
    found = False
    for i, raw in enumerate(cells):
        label = _clean_label(raw)
        if not label:
            continue
        attr = _LABEL_MAP.get(label)
        if not attr:
            continue
        # The value is the next non-empty cell — but NEVER another known label.
        # e-GP serves the form SHELL (all labels, no values) for a non-existent
        # tenderId; without this guard the following label is read as the value
        # (e.g. public_entity="Procurement Category") and a garbage record is
        # cached + published instead of the id being recorded as a gap.
        value = ""
        for nxt in cells[i + 1 : i + 3]:
            candidate = normalize_whitespace(nxt)
            if not candidate:
                continue
            if _clean_label(candidate) in _KNOWN_LABELS:
                break  # hit the next label → this field is empty on the page
            value = candidate
            break
        if not value or value == label:
            continue
        if getattr(tender, attr) is None:
            setattr(tender, attr, value)
            found = True

    return tender if found else None
