"""Parse the PPMO (Public Procurement Monitoring Office) blacklist portal.

The pure parse/shape half of the ``scrape_ppmo_blacklist`` command: every
function here is a pure transform over HTML, so it unit-tests without a network
or a database. Ported faithfully from the retired ``ppmo_blacklist`` Scrapy
spider (archived ``Jawafdehi/ngm``) — the selectors (``table.list4`` list page,
``table.list3`` detail page), the non-firm-row filter, and the BS-year
plausibility band are the spider's, re-expressed over BeautifulSoup to match the
other court-portal parsers in this package.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from bs4 import BeautifulSoup

from courts.scraper.text import normalize_date, normalize_whitespace
from jawafdehi_shared.dates import bs_to_ad

#: The blacklist lives on the OLD subdomain — the main site 404s the route.
LIST_URL = "https://old.ppmo.gov.np/index.php?route=information/black_lists"

#: PPMO was established in BS 2064; a real blacklist row's date sits in this band.
#: The source occasionally leaks AD-formatted text ("2017-09-04") into a date
#: cell which, read as BS, converts to a nonsense 1960s AD date — so anything
#: outside the band is rejected rather than persisted (the spider's guard).
_VALID_BS_YEAR = (2060, 2099)
_BS_DATE_RE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")

#: Non-firm strings that appear in the firm-name column and must not become rows.
_ARROWS = {"»", "›", ">", "<", "«", "‹"}

_PROPRIETOR_RE = re.compile(r"मुख्य व्यक्ति:\s*([^)]+)")
_OFFICE_RE = re.compile(r"सार्बजनिक निकायको नाम\s*:?\s*([^)]+)")


@dataclass
class ParsedFirm:
    """One blacklist row. ``duration`` + the detail fields come off the pages;
    the date fields stay ``None`` until :func:`resolve_dates` fills them."""

    firm_name: str
    duration: str
    detail_href: str | None = None
    address: str | None = None
    reason: str | None = None
    proprietor_name: str | None = None
    recommending_office: str | None = None
    blacklist_date_bs: str | None = None
    effective_until_bs: str | None = None
    blacklist_date_ad: date | None = None
    effective_until_ad: date | None = None


def looks_like_firm_name(value: str) -> bool:
    """Reject pagination arrows, the column header, and pure-punctuation cells."""
    s = (value or "").strip()
    if len(s) < 3 or s in _ARROWS:
        return False
    if "विवरण" in s or "company name" in s.lower():
        return False
    # Require at least 2 letters (Devanagari or Latin); punctuation-only rows fail.
    return sum(1 for ch in s if ch.isalpha()) >= 2


def _cell_text(cell) -> str:
    """All text in a cell, whitespace-collapsed (``normalize_whitespace`` folds
    NBSP too, since it splits on Unicode whitespace)."""
    return normalize_whitespace(cell.get_text(" ", strip=True))


def parse_list(html: str) -> tuple[list[ParsedFirm], str | None]:
    """Parse a blacklist list page → ``(firms, next_page_href)``.

    Reads ``table.list4`` (falling back to any table with data rows). Each row is
    firm-name (col 0, optional detail link) + duration (col 1); rows failing
    :func:`looks_like_firm_name` are skipped. ``next_page_href`` is the pagination
    "next" (``>``) link, or ``None`` on the last page.
    """
    soup = BeautifulSoup(html, "html.parser")
    rows = soup.select("table.list4 tr")
    if not rows:
        rows = [tr for tr in soup.select("table tr") if tr.find("td")]

    firms: list[ParsedFirm] = []
    for row in rows:
        cols = row.find_all("td", recursive=False) or row.find_all("td")
        if len(cols) < 2:
            continue
        firm_name = _cell_text(cols[0])
        if not looks_like_firm_name(firm_name):
            continue
        link = cols[0].find("a")
        firms.append(
            ParsedFirm(
                firm_name=firm_name,
                duration=_cell_text(cols[1]),
                detail_href=link.get("href") if link and link.get("href") else None,
            )
        )

    return firms, _next_page_href(soup)


def _next_page_href(soup) -> str | None:
    pagination = soup.select_one("div.pagination")
    if not pagination:
        return None
    for anchor in pagination.find_all("a"):
        if ">" in anchor.get_text() and anchor.get("href"):
            return anchor.get("href")
    return None


def parse_detail(html: str) -> dict | None:
    """Parse a firm detail page (``table.list3``) → address / reason / proprietor
    / recommending-office.

    Returns ``None`` when the page has no ``list3`` table (we followed a non-detail
    link, e.g. a pagination arrow), so the caller can skip rather than persist a
    half-empty row.
    """
    soup = BeautifulSoup(html, "html.parser")
    rows = soup.select("table.list3 tr")
    if not rows:
        return None

    out: dict = {}
    for row in rows:
        cells = row.find_all("td")
        if len(cells) < 2:
            continue
        label = normalize_whitespace(cells[0].get_text())
        value = normalize_whitespace(cells[1].get_text(" "))
        if "Address" in label:
            out["address"] = value
        elif "Cause" in label:
            out["reason"] = value
            prop = _PROPRIETOR_RE.search(value)
            if prop:
                out["proprietor_name"] = prop.group(1).replace("श्री", "").strip()
            office = _OFFICE_RE.search(value)
            if office:
                out["recommending_office"] = office.group(1).replace("श्री", "").strip()
    return out


def resolve_dates(firm: ParsedFirm) -> bool:
    """Split ``duration`` into BS/AD blacklist + effective-until dates, in place.

    ``duration`` is ``"<bs> to <bs>"`` or a single ``<bs>``. Returns ``False``
    (leaving the date fields ``None``) when the first date isn't a plausible BS
    date — the spider's guard against AD-format values that would otherwise
    convert to nonsense AD dates.
    """
    duration = firm.duration or ""
    if " to " in duration:
        start, _, until = duration.partition(" to ")
    else:
        start, until = duration, ""

    start_bs = normalize_date(start)
    until_bs = normalize_date(until) or None

    year = _bs_year(start_bs)
    if year is None or not (_VALID_BS_YEAR[0] <= year <= _VALID_BS_YEAR[1]):
        return False

    firm.blacklist_date_bs = start_bs
    firm.effective_until_bs = until_bs
    firm.blacklist_date_ad = bs_to_ad(start_bs)
    firm.effective_until_ad = bs_to_ad(until_bs) if until_bs else None
    return True


def _bs_year(date_str: str) -> int | None:
    match = _BS_DATE_RE.match((date_str or "").strip())
    return int(match.group(1)) if match else None


def to_payload(firm: ParsedFirm) -> dict:
    """Serialize a resolved firm to the ``/ingestion/firms`` item shape (JSON-able).

    Assumes :func:`resolve_dates` has run (``blacklist_date_bs`` is set). Dates
    become ISO strings; ``None`` detail fields are omitted so the idempotent
    upsert never clobbers a stored value with an explicit null.
    """
    payload = {
        "firm_name": firm.firm_name,
        "blacklist_date_bs": firm.blacklist_date_bs,
    }
    optional = {
        "proprietor_name": firm.proprietor_name,
        "address": firm.address,
        "duration": firm.duration,
        "reason": firm.reason,
        "recommending_office": firm.recommending_office,
        "effective_until_bs": firm.effective_until_bs,
        "blacklist_date_ad": firm.blacklist_date_ad.isoformat() if firm.blacklist_date_ad else None,
        "effective_until_ad": firm.effective_until_ad.isoformat() if firm.effective_until_ad else None,
    }
    payload.update({key: value for key, value in optional.items() if value})
    return payload
