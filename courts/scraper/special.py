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

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from bs4 import BeautifulSoup

from courts.normalize import best_effort_normalize
from courts.scraper.text import (
    fix_parenthesis_spacing,
    nepali_to_roman_numerals,
    normalize_date,
    normalize_whitespace,
)
from jawafdehi_shared.dates import bs_to_ad

COURT_ID = "special"


@dataclass
class ParsedCase:
    case_number: str
    court_identifier: str
    registration_date_bs: str | None = None
    registration_date_ad: date | None = None
    case_type: str | None = None
    plaintiff: str | None = None
    defendant: str | None = None
    extra_data: dict[str, Any] = field(default_factory=dict)


@dataclass
class ParsedHearing:
    case_number: str
    court_identifier: str
    hearing_date_bs: str
    hearing_date_ad: date | None
    bench_type: str | None = None
    serial_no: str | None = None
    judge_names: str | None = None
    case_status: str | None = None
    decision_type: str | None = None
    remarks: str | None = None
    extra_data: dict[str, Any] = field(default_factory=dict)


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
    bench_type: str | None = None,
    bench_label: str | None = None,
) -> list[tuple[ParsedCase, ParsedHearing]]:
    """Parse one bench's case table (stage 2) into (case, hearing) rows."""
    soup = BeautifulSoup(html, "html.parser")
    hearing_date_ad = bs_to_ad(date_bs)

    court_number_elem = soup.find(
        "font", string=lambda x: x and "इजलास" in x and "नं" in x
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
            bench_type=bench_type,
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
    bench_type: str | None,
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
        bench_type=bench_type,
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
