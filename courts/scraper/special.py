"""Special Court cause-list parser (ported from ngm ``special_court_cases``).

Two-stage on the portal: discover the day's benches, then fetch each bench's case
table. Portal: ``supremecourt.gov.np/special/syspublic.php?d=reports&f=daily_public``.
These are the pure parse halves; the fetch/orchestration lives in the management
command. The 11-column bench table maps to one CourtCase + one CourtCaseHearing
per row. Court-owned typed columns are set here; the low-value legacy fields
(``category``, ``original_case_number``) go to ``extra_data`` — the v2 court_cases
projection does not carry them as columns.
"""

from __future__ import annotations

from datetime import date

from bs4 import BeautifulSoup

from courts.normalize import best_effort_normalize
from courts.scraper.rows import ParsedCase, ParsedEnrichment, ParsedHearing
from courts.scraper.text import (
    coerce_count,
    fix_parenthesis_spacing,
    nepali_to_roman_numerals,
    normalize_date,
    normalize_whitespace,
)
from jawafdehi_shared.dates import bs_to_ad

COURT_ID = "special"
COURT_IDS = ("special",)
LOOKBACK_DAYS = 15 * 365  # accountability-critical: crawl the full history
BASE_URL = "https://supremecourt.gov.np/special/syspublic.php?d=reports&f=daily_public"
DETAIL_URL = "https://supremecourt.gov.np/special/syspublic.php?d=reports&f=case_details"


def court_ids(fetch) -> list[str]:
    """The court_identifiers this module crawls (Special is a single court)."""
    return list(COURT_IDS)


def crawl_detail(fetch, court_id, case_number) -> "ParsedEnrichment | None":
    """Fetch + parse one case's detail page for enrichment."""
    html = fetch(
        DETAIL_URL,
        data={"syy": "", "smm": "", "sdd": "", "mode": "show",
              "regno": case_number, "submit": " Search "},
    )
    return parse_detail(html) if html else None


def crawl_date(fetch, court_id, date_bs, nepali_date) -> list[tuple[ParsedCase, ParsedHearing]]:
    """Fetch + parse one date across all benches. ``fetch(url, data=...) -> html``
    is injected so the orchestration is testable without the network. ``court_id``
    is always ``special`` here (single court); kept for a uniform crawl interface."""
    syy = str(nepali_date.year)
    smm = f"{nepali_date.month:02d}"
    sdd = f"{nepali_date.day:02d}"
    bench_html = fetch(BASE_URL, data={"mode": "showbench", "syy": syy, "smm": smm, "sdd": sdd})
    rows: list[tuple[ParsedCase, ParsedHearing]] = []
    for bench in parse_bench_options(bench_html):
        page = fetch(
            BASE_URL,
            data={"mode": "show", "syy": syy, "smm": smm, "sdd": sdd,
                  "bench_type": bench["value"], "yo": "1"},
        )
        rows.extend(
            parse_bench_page(page, date_bs=date_bs, bench_label=bench["label"])
        )
    return rows

# Detail-page main-table labels → typed columns. Low-value legacy fields
# (category/division) are routed to extra_data, not columns.
_ENRICH_CORE_LABELS = {
    "दर्ता नँ .": "registration_number",
    "मुद्दा": "case_type",
    "मुद्दाको स्थिती": "case_status",
}
_ENRICH_EXTRA_LABELS = {
    "मुद्दाको किसिम": "category",
    "फाँट": "division",
}


def parse_bench_options(html: str) -> list[dict[str, str]]:
    """Discover the day's benches from the ``bench_type`` <select> (stage 1)."""
    soup = BeautifulSoup(html, "html.parser")
    select = soup.find("select", {"name": "bench_type"})
    if not select:
        return []
    return [
        {"value": opt.get("value", "").strip(), "label": opt.get_text(strip=True)}
        for opt in select.find_all("option")
        if opt.get("value", "").strip()
    ]


def parse_bench_page(
    html: str,
    *,
    date_bs: str,
    bench_label: str | None = None,
) -> list[tuple[ParsedCase, ParsedHearing]]:
    """Parse one bench's case table (stage 2) into (case, hearing) rows."""
    soup = BeautifulSoup(html, "html.parser")
    hearing_date_ad = bs_to_ad(date_bs)

    # `bool(x)` not `x` — see the note in courts/scraper/high.py: bs4's string
    # matcher is a bool predicate, and `x and ...` yields the str when falsy.
    court_number_elem = soup.find(
        "font", string=lambda x: bool(x) and "इजलास" in x and "नं" in x
    )
    court_number = (
        normalize_whitespace(court_number_elem.get_text()) if court_number_elem else ""
    )
    judge_names = _extract_judges(soup)

    case_table = soup.find("table", {"width": "100%", "border": "1"})
    if not case_table:
        return []

    rows: list[tuple[ParsedCase, ParsedHearing]] = []
    for tr in case_table.find_all("tr")[1:]:  # skip the header row
        parsed = _parse_row(
            tr.find_all("td"),
            date_bs=date_bs,
            hearing_date_ad=hearing_date_ad,
            bench_label=bench_label,
            court_number=court_number,
            judge_names=judge_names,
        )
        if parsed is not None:
            rows.append(parsed)
    return rows


def _extract_judges(soup: BeautifulSoup) -> str | None:
    for font_tag in soup.find_all("font", {"size": "2"}):
        text = font_tag.get_text(strip=True)
        if "अध्यक्ष माननीय न्यायाधीश" in text or "सदस्य माननीय न्यायाधीश" in text:
            parent_td = font_tag.find_parent("td")
            if not parent_td:
                continue
            for br in parent_td.find_all("br"):
                br.replace_with("\n")
            lines = [
                normalize_whitespace(ln)
                for ln in parent_td.get_text().split("\n")
                if ln.strip()
            ]
            return "\n".join(lines) or None
    return None


def _parse_row(
    cells,
    *,
    date_bs: str,
    hearing_date_ad: date | None,
    bench_label: str | None,
    court_number: str,
    judge_names: str | None,
) -> tuple[ParsedCase, ParsedHearing] | None:
    if len(cells) < 11:
        return None

    case_number = normalize_whitespace(cells[4].get_text())
    if not case_number:
        return None
    case_number = best_effort_normalize(case_number)

    registration_date = normalize_date(cells[2].get_text())
    case = ParsedCase(
        case_number=case_number,
        court_identifier=COURT_ID,
        registration_date_bs=registration_date or None,
        registration_date_ad=bs_to_ad(registration_date),
        case_type=normalize_whitespace(cells[3].get_text()) or None,
        plaintiff=normalize_whitespace(cells[5].get_text()) or None,
        defendant=normalize_whitespace(cells[6].get_text()) or None,
        # Low-value legacy fields → extra_data, not v2 columns.
        extra_data={
            "category": normalize_whitespace(cells[1].get_text()) or None,
            "original_case_number": fix_parenthesis_spacing(cells[7].get_text()) or None,
        },
    )

    hearing = ParsedHearing(
        case_number=case_number,
        court_identifier=COURT_ID,
        hearing_date_bs=date_bs,
        hearing_date_ad=hearing_date_ad,
        # NB: bench_type is intentionally left unset. The bench <select> option
        # VALUE is an internal per-sitting bench id (e.g. "8860"), NOT a bench TYPE
        # (single/joint) — writing it here produced 36k numeric-junk bench_types
        # across the special court. The bench's identity is its judges, carried in
        # judge_names (+ the short form in extra_data.bench_label).
        serial_no=nepali_to_roman_numerals(normalize_whitespace(cells[0].get_text()))
        or None,
        judge_names=judge_names,
        case_status=normalize_whitespace(cells[9].get_text()) or None,
        decision_type=normalize_whitespace(cells[10].get_text()) or None,
        remarks=normalize_whitespace(cells[8].get_text()) or None,
        extra_data={
            "bench_label": normalize_whitespace(bench_label) or None,
            "court_number": court_number or None,
        },
    )
    return case, hearing


def parse_detail(html: str) -> ParsedEnrichment:
    """Parse a Special-court case detail page into a :class:`ParsedEnrichment`.

    Ported from ngm ``special_case_enrichment``: the main caption table yields the
    typed columns + parties; the "पेशी को विवरण" section yields the hearing list
    (``enrichment_hearings``) the write path uses to derive a verdict.
    """
    soup = BeautifulSoup(html, "html.parser")
    core: dict = {}
    extra: dict = {}
    entities: list[dict] = []

    main = soup.find(
        "table",
        {"width": "100%", "border": "0", "cellspacing": "0", "cellpadding": "1"},
    )
    if main is not None:
        for tr in main.find_all("tr"):
            cells = tr.find_all("td")
            for i, cell in enumerate(cells):
                if "caption" not in cell.get("class", []):
                    continue
                if i + 1 >= len(cells) or "caption" in cells[i + 1].get("class", []):
                    continue
                label = normalize_whitespace(cell.get_text()).rstrip(":").strip()
                value = normalize_whitespace(cells[i + 1].get_text())
                if label in _ENRICH_CORE_LABELS:
                    core[_ENRICH_CORE_LABELS[label]] = value[:200] or None
                elif label in _ENRICH_EXTRA_LABELS:
                    extra[_ENRICH_EXTRA_LABELS[label]] = value[:100] or None
                elif label == "दर्ता मिती" and value:
                    core["registration_date_bs"] = normalize_date(value)
                    core["registration_date_ad"] = bs_to_ad(normalize_date(value))
                elif label in ("वादीहरु", "प्रतिवादीहरु"):
                    side = "plaintiff" if label == "वादीहरु" else "defendant"
                    for name in (n.strip() for n in value.split(",")):
                        if name:
                            entities.append({"side": side, "name": name[:500], "address": None})

    hearings = _parse_hearing_section(soup)
    if hearings:
        extra["enrichment_hearings"] = hearings
        core["hearing_count"] = coerce_count(len(hearings))
    return ParsedEnrichment(core_fields=core, extra_data=extra, entities=entities)


def _parse_hearing_section(soup: BeautifulSoup) -> list[dict]:
    """Rows of the 'पेशी को विवरण' (hearing schedule) ``utivtbl`` table."""
    heading = soup.find(string=lambda x: bool(x) and "पेशी को विवरण" in x)
    if not heading:
        return []
    parent_row = heading.find_parent("tr")
    next_row = parent_row.find_next_sibling("tr") if parent_row else None
    table = next_row.find("table", class_="utivtbl") if next_row else None
    if table is None:
        return []
    hearings = []
    for tr in table.find_all("tr")[1:]:
        cells = tr.find_all("td")
        if len(cells) >= 4:
            hearings.append(
                {
                    "hearing_date": normalize_date(cells[0].get_text()),
                    "case_status": normalize_whitespace(cells[2].get_text()),
                    "decision_type": normalize_whitespace(cells[3].get_text()),
                }
            )
    return hearings
