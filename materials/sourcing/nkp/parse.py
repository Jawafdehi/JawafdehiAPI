"""Framework-agnostic parsing for nkp.gov.np (Nepal Law Journal) pages.

The extraction logic behind the NKP precedent crawler (``materials.sourcing.nkp.
crawl``). All functions take raw HTML strings and return plain dicts/lists (lxml
under the hood) — no transport coupling. Pure + unit-testable offline.

nkp.gov.np publishes Supreme Court precedents (नजिर) as selectable Unicode
Devanagari HTML — no OCR needed — which is why this is scraped rather than the
scanned issue PDFs on ``supremecourt.gov.np/web/nkpold``.

The page structures (verified against live pages 2026-07-10):

- ``/browse``               → year index; each year link
  ``browse_monthly/?...&year=YYYY`` with a "(<count> थान)" published count.
- ``/browse_monthly/?...&year=YYYY`` → month links
  ``advance_search/?...&year=YYYY&month=M``.
- ``/advance_search/?...&year=YYYY&month=M`` → the decision listing (PAGINATED via
  a ``per_page`` ROW OFFSET; ≈20/page). :func:`extract_listing` scopes to the
  result ``article`` rows and reads the page's stated total.
- ``/full_detail/{id}``     → one decision (Unicode HTML, no OCR).
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urljoin

import lxml.html

from .normalizer import (
    nepali_to_roman_numerals,
    normalize_date,
    normalize_whitespace,
)

BASE = "https://nkp.gov.np"

# ── metadata markers in the judgment body ────────────────────────────────────
_LABEL_MUDDA = "मुद्दा"
_LABEL_ORDER_DATE = "आदेश मिति"
_LABEL_JUDGE = "न्यायाधीश"

_PRAKARAN_RE = re.compile(r"\(?\s*प्रकरण\s*नं[.\s]*([०-९\d]+)\s*\)?")
_COURT_BENCH_RE = re.compile(r"^(.+अदालत)\s*,\s*(.+इजलास)\s*$")
_CASE_NUMBER_RE = re.compile(r"\b(\d{3}-[A-Z]{2,3}-\d{3,5})\b")
_DASH_RE = re.compile(r"[‐-―−]")
_LAW_TAIL_RE = re.compile(r"ऐन,?\s*([०-९\d]{4})")
_DANDA_RE = re.compile(r"[।॥]")
_REMOVAL_MARKERS = ("हटाइएको", "विचाराधिन", "झिकिएको", "राखिने छैन")

_YEAR_RE = re.compile(r"year=(\d{3,4})")
_MONTH_RE = re.compile(r"month=(\d+)")
_COUNT_RE = re.compile(r"\(([०-९\d]+)\s*थान\)")
_DETAIL_ID_RE = re.compile(r"full_detail/(\d+)")


# ── /browse — year index ─────────────────────────────────────────────────────

def parse_browse(html: str) -> list[dict[str, Any]]:
    """Year index → ``[{year, expected}]`` (expected = published "थान" count)."""
    doc = lxml.html.fromstring(html)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for a in doc.xpath('//a[contains(@href, "browse_monthly")]'):
        href = a.get("href") or ""
        ym = _YEAR_RE.search(href)
        if not ym or ym.group(1) in seen:
            continue
        seen.add(ym.group(1))
        text = normalize_whitespace(a.text_content())
        cm = _COUNT_RE.search(text)
        expected = int(nepali_to_roman_numerals(cm.group(1))) if cm else None
        out.append({"year": ym.group(1), "expected": expected})
    return out


# ── /browse_monthly — months in a year ───────────────────────────────────────

def parse_year_months(html: str) -> list[str]:
    """Month links in a year page → sorted list of month numbers (as strings)."""
    doc = lxml.html.fromstring(html)
    months: set[str] = set()
    for a in doc.xpath(
        '//a[contains(@href, "advance_search") and contains(@href, "month=")]'
    ):
        mm = _MONTH_RE.search(a.get("href") or "")
        if mm:
            months.add(mm.group(1))
    return sorted(months, key=int)


# ── /advance_search — the (paginated) decision listing ────────────────────────

_RESULT_COUNT_RE = re.compile(r"([०-९\d]+)\s*खोजी\s*नतिजा")


def extract_listing(html: str, current_url: str = BASE) -> dict[str, Any]:
    """Extract a month listing PAGE's result decision ids + the total count.

    Returns ``{"detail_ids": [...], "total": int|None, "page_size": int}``:
    - ``detail_ids``: the ``full_detail/{id}`` ids of the genuine RESULT rows on
      this page — scoped to ``div.main-listing article`` (each result is an
      ``<article class="format-standard …">``). This is deliberately NOT the old
      ``col-md-8`` scope: past-the-last-page offsets render the sidebar's
      "recently published"/"most viewed" set into the column, and those constant
      links must NOT be mistaken for results (that was the pagination bug).
    - ``total``: the "<N> खोजी नतिजाहरु" ("N search results") count the page
      prints — the exact number of decisions in this month, so the caller knows
      how many ``per_page`` offset pages to walk and can verify completeness.
    - ``page_size``: results per page (20 on this site) inferred from the pager;
      defaults to 20.

    Pagination on this site is a ``per_page`` ROW OFFSET (page 2 = ``per_page=20``,
    page 3 = ``per_page=40`` …). The caller steps the offset itself rather than
    following pager links, and stops at ``total`` — robust against the sidebar
    fallback an over-run offset would otherwise yield.
    """
    doc = lxml.html.fromstring(html)

    # RESULT rows only: div.main-listing > article ... a[full_detail]. Fall back
    # to article anywhere, then (last resort) main column — but the article scope
    # is what keeps the sidebar out.
    rows = doc.xpath(
        '//div[contains(@class,"main-listing")]//article//a[contains(@href,"full_detail")]/@href'
    )
    if not rows:
        rows = doc.xpath('//article//a[contains(@href,"full_detail")]/@href')
    detail_ids: list[str] = []
    for href in rows:
        m = _DETAIL_ID_RE.search(href)
        if m and m.group(1) not in detail_ids:
            detail_ids.append(m.group(1))

    total: int | None = None
    cm = _RESULT_COUNT_RE.search(doc.text_content())
    if cm:
        total = int(nepali_to_roman_numerals(cm.group(1)))

    # Page size from any per_page link (the pager increments by one page worth).
    page_size = 20
    for href in doc.xpath('//a[contains(@href,"per_page=")]/@href'):
        pm = re.search(r"per_page=(\d+)", href)
        if pm and int(pm.group(1)) > 0:
            page_size = int(pm.group(1))
            break

    return {"detail_ids": detail_ids, "total": total, "page_size": page_size}


# ── /full_detail/{id} — one decision ─────────────────────────────────────────

def _extract_laws(para: str) -> set[str]:
    """Best-effort act citations ("<title> ऐन, <BS year>") from a paragraph."""
    laws: set[str] = set()
    for m in _LAW_TAIL_RE.finditer(para):
        head = _DANDA_RE.split(para[: m.start()])[-1]
        words = normalize_whitespace(head).split()
        title_words = words[-7:]
        if title_words and re.search(r"(उपर|लाई|माथि|समेत)$", title_words[0]):
            title_words = title_words[1:]
        cite = normalize_whitespace(f"{' '.join(title_words)} ऐन, {nepali_to_roman_numerals(m.group(1))}")
        if 6 <= len(cite) <= 120:
            laws.add(cite)
    return laws


def _edition_field(main, label: str, roman: bool = True):
    vals = main.xpath(
        f'.//div[@id="edition-info"]//span[contains(text(), "{label}")]/strong/text()'
    )
    val = normalize_whitespace(vals[0]) if vals else ""
    return (nepali_to_roman_numerals(val) if roman else val) or None


def _parse_body(item: dict[str, Any], paras: list[str]) -> None:
    """Split body paragraphs into header metadata, headnotes, and full text."""
    judges: list[str] = []
    headnotes: list[dict[str, str]] = []
    laws: set[str] = set()
    text_parts: list[str] = []
    pending_headnote: str | None = None

    for para in paras:
        cb = _COURT_BENCH_RE.match(para)
        if cb and "court" not in item:
            item["court"] = normalize_whitespace(cb.group(1))
            item["bench"] = normalize_whitespace(cb.group(2))
            continue
        if _LABEL_JUDGE in para and "माननीय" in para:
            name = re.sub(r"^.*?न्यायाधीश\s*(श्री)?\s*", "", para).strip()
            if name:
                judges.append(normalize_whitespace(name))
            continue
        if para.startswith(_LABEL_ORDER_DATE):
            item["order_date_bs"] = normalize_date(para.split(":", 1)[-1])
            continue
        if para.startswith(_LABEL_MUDDA) and "case_name" not in item:
            item["case_name"] = normalize_whitespace(para.split(":", 1)[-1])
            continue
        roman = _DASH_RE.sub("-", nepali_to_roman_numerals(para))
        cn = _CASE_NUMBER_RE.search(roman)
        if cn and "case_number" not in item and len(para) < 40:
            item["case_number"] = cn.group(1)
            continue
        pk = _PRAKARAN_RE.search(para)
        if pk and len(para) < 40:
            if pending_headnote:
                headnotes.append(
                    {"text": pending_headnote, "prakaran": nepali_to_roman_numerals(pk.group(1))}
                )
                pending_headnote = None
            continue
        laws.update(_extract_laws(para))
        if para:
            text_parts.append(para)
            pending_headnote = para

    if judges:
        item["judges"] = judges
    if headnotes:
        item["headnotes"] = headnotes
    if laws:
        item["referenced_laws"] = sorted(laws)
    item["full_text"] = "\n\n".join(text_parts)


def parse_detail(html: str, detail_id: str, source_url: str | None = None) -> dict[str, Any] | None:
    """Parse a ``full_detail/{id}`` page into the ``NkpDecisionItem`` dict shape.

    Returns ``None`` when the content column is absent (not a decision page —
    e.g. an F5 challenge interstitial slipped through). ``scraped_at`` is NOT set
    here (the caller stamps it) so this stays pure/deterministic for tests.
    """
    doc = lxml.html.fromstring(html)
    mains = doc.xpath(
        '//div[contains(@class,"col-md-8") and contains(@class,"para-sections")]'
    )
    if not mains:
        return None
    main = mains[0]

    item: dict[str, Any] = {
        "detail_id": detail_id,
        "source_url": source_url or f"{BASE}/full_detail/{detail_id}",
    }

    titles = main.xpath('.//h1[contains(@class,"post-title")]')
    title = normalize_whitespace(titles[0].text_content()) if titles else ""
    item["title"] = title
    dec_m = re.search(r"निर्णय नं[.\s]*([०-९\d]+)\s*-\s*(.+)$", title)
    if dec_m:
        item["decision_no_bs"] = dec_m.group(1)
        item["decision_no"] = nepali_to_roman_numerals(dec_m.group(1))
        item["case_name"] = normalize_whitespace(dec_m.group(2))

    item["volume"] = _edition_field(main, "भाग")
    item["year_bs"] = _edition_field(main, "साल")
    item["month"] = _edition_field(main, "महिना", roman=False)
    item["issue"] = _edition_field(main, "अंक")

    pm = main.xpath('.//div[contains(@class,"post-meta")]')
    post_meta = normalize_whitespace(pm[0].text_content()) if pm else ""
    fdate = re.search(r"फैसला मिति\s*:?\s*([०-९\d/।.\-]+)", post_meta)
    if fdate:
        item["decision_date_bs"] = normalize_date(fdate.group(1))
    views = re.search(r"([०-९\d]+)\s*$", post_meta)
    if views:
        item["view_count"] = int(nepali_to_roman_numerals(views.group(1)))

    paras = [
        normalize_whitespace(p.text_content())
        for p in main.xpath('.//div[starts-with(@id,"faisala_detail")]//p')
    ]
    paras = [p for p in paras if p]
    _parse_body(item, paras)

    joined = " ".join(paras)
    item["removed"] = bool(
        any(mk in joined for mk in _REMOVAL_MARKERS) and len(joined) < 1500
    )

    pdf_links = main.xpath(
        './/div[starts-with(@id,"faisala_detail")]//a[contains(@href,".pdf")]/@href'
    )
    pdf = pdf_links[0] if pdf_links else None
    if not pdf:
        pmatch = re.search(r"https?://\S+\.pdf", joined)
        pdf = pmatch.group(0) if pmatch else None
    if pdf:
        item["fallback_pdf_url"] = urljoin(source_url or BASE, pdf)

    return item
