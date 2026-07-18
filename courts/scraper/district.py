"""District Court cause-list + enrichment parsers (ported from the ngm
``district_court_cases`` / ``district_case_enrichment`` spiders).

District is single-stage on the portal: one POST per (court, date) returns the
whole day's causelist, so — unlike Special/High — there is no bench discovery
round-trip. A page can still carry several per-bench ``record_display`` tables,
each attributed to the bench/judge in its sibling header table; every data row
maps to one CourtCase + one CourtCaseHearing. Court-owned typed columns are set
here; the district-only legacy fields (``section``, ``priority``, ``case_id``,
``secondary_case_number``) go to ``extra_data`` — the v2 court_cases projection
does not carry them as columns.

The enrichment half parses a case's detail page into a :class:`ParsedEnrichment`:
typed columns (``case_status``/``verdict_*``/``case_subject``/``hearing_count``/
``registration_number``/``verdict_judge``), ``extra_data`` (hearings/timeline),
and plaintiff/defendant entities. The raw ``case_status`` string is normalised
through :mod:`courts.case_status` at parse time — a scraped column-header is
dropped (never stored as a status), and a verdict_type/date is derived where the
status (or, failing that, the final hearing) yields one. Pure functions: no
network, no DB.
"""

from __future__ import annotations

from datetime import date

from bs4 import BeautifulSoup

from courts.case_status import (
    is_status_artifact,
    parse_case_status,
    verdict_from_hearings,
)
from courts.normalize import best_effort_normalize
from courts.scraper.rows import ParsedCase, ParsedEnrichment, ParsedHearing
from courts.scraper.text import (
    coerce_count,
    nepali_to_roman_numerals,
    normalize_date,
    normalize_whitespace,
)
from jawafdehi_shared.dates import bs_to_ad

# The "no verdict" placeholder the portal stamps into फैसला मिति; never a date.
_VERDICT_DATE_SENTINEL = "**** ** **"


# --- cause list --------------------------------------------------------------


def parse_daily_list(
    html: str,
    *,
    court_identifier: str,
    date_bs: str,
) -> list[tuple[ParsedCase, ParsedHearing]]:
    """Parse a district daily causelist page into ``(case, hearing)`` rows.

    ``court_identifier`` is the court's ``code_name`` (district has ~77 courts,
    so there is no single module constant the way Special has). Each
    ``record_display`` table inherits the bench/judge from its previous-sibling
    header table, matching the portal's per-bench sectioning on one page.
    """
    soup = BeautifulSoup(html, "html.parser")
    hearing_date_ad = bs_to_ad(date_bs)

    case_tables = soup.find_all("table", {"border": "1", "class": "record_display"})
    if not case_tables:
        return []

    rows: list[tuple[ParsedCase, ParsedHearing]] = []
    current_bench: str | None = None
    current_judge: str | None = None

    for table in case_tables:
        prev_table = table.find_previous_sibling("table")
        if prev_table:
            bench_row = prev_table.find("tr")
            if bench_row:
                bench_td = bench_row.find("td", align="right")
                judge_td = bench_row.find("td", class_="judge")
                if bench_td:
                    current_bench = normalize_whitespace(bench_td.get_text()) or None
                if judge_td:
                    current_judge = normalize_whitespace(judge_td.get_text()) or None

        for tr in table.find_all("tr"):
            if tr.find("th"):  # header row
                continue
            parsed = _parse_row(
                tr.find_all("td"),
                court_identifier=court_identifier,
                date_bs=date_bs,
                hearing_date_ad=hearing_date_ad,
                bench=current_bench,
                judge=current_judge,
            )
            if parsed is not None:
                rows.append(parsed)
    return rows


def _parse_row(
    cells,
    *,
    court_identifier: str,
    date_bs: str,
    hearing_date_ad: date | None,
    bench: str | None,
    judge: str | None,
) -> tuple[ParsedCase, ParsedHearing] | None:
    if len(cells) < 10:
        return None

    serial_no = nepali_to_roman_numerals(normalize_whitespace(cells[0].get_text()))

    # Cell 1 carries the case number on line 1 and (when present) a parenthetical
    # secondary/original id on line 2 — split on the <br>-derived newlines.
    case_parts = cells[1].get_text(separator="\n").strip().split("\n")
    case_number = normalize_whitespace(case_parts[0]) if case_parts else ""
    if not case_number:
        return None
    case_number = best_effort_normalize(case_number)

    # case_id / secondary keep the ngm treatment: transliterated but NOT
    # canonicalised (no uppercase/zero-pad), and only the parens stripped.
    case_id = (
        nepali_to_roman_numerals(normalize_whitespace(case_parts[1].strip("()")))
        if len(case_parts) > 1
        else ""
    )
    secondary_case_number = None
    if len(case_parts) >= 2:
        secondary_case_number = nepali_to_roman_numerals(
            normalize_whitespace(case_parts[-1].strip("()"))
        )

    reg_date_parts = cells[2].get_text(separator="\n").strip().split("\n")
    registration_date = normalize_date(reg_date_parts[0]) if reg_date_parts else ""

    # section / priority are free Devanagari text — normalised for whitespace but
    # never transliterated — and are not v2 columns, so they go to extra_data.
    extra_data = {
        "section": normalize_whitespace(cells[6].get_text()) or None,
        "priority": normalize_whitespace(cells[7].get_text()) or None,
        "case_id": case_id or None,
    }
    if secondary_case_number:
        extra_data["secondary_case_number"] = secondary_case_number

    case = ParsedCase(
        case_number=case_number,
        court_identifier=court_identifier,
        registration_date_bs=registration_date or None,
        registration_date_ad=bs_to_ad(registration_date),
        case_type=normalize_whitespace(cells[3].get_text()) or None,
        plaintiff=normalize_whitespace(cells[4].get_text()) or None,
        defendant=normalize_whitespace(cells[5].get_text()) or None,
        extra_data=extra_data,
    )

    hearing = ParsedHearing(
        case_number=case_number,
        court_identifier=court_identifier,
        hearing_date_bs=date_bs,
        hearing_date_ad=hearing_date_ad,
        bench=bench,
        serial_no=serial_no or None,
        judge_names=judge,
        decision_type=normalize_whitespace(cells[9].get_text()) or None,
        remarks=normalize_whitespace(cells[8].get_text()) or None,
    )
    return case, hearing


# --- enrichment (detail page) ------------------------------------------------


def parse_district_detail(html: str) -> ParsedEnrichment:
    """Parse a district case detail page into a :class:`ParsedEnrichment`."""
    soup = BeautifulSoup(html, "html.parser")
    fields = _extract_detail_fields(soup)
    entities = _extract_entities(soup)
    hearings_timeline = _extract_hearings_timeline(soup)

    core_fields: dict[str, object] = {}
    for key in ("registration_number", "case_type", "case_subject", "verdict_judge"):
        if fields.get(key):
            core_fields[key] = fields[key]

    hearing_count = coerce_count(fields.get("hearing_count"))
    if hearing_count is not None:
        core_fields["hearing_count"] = hearing_count

    # case_status: keep the raw string only if it is a real status (a scraped
    # column-header is dropped), and let it drive the typed verdict fields.
    raw_status = fields.get("case_status")
    parsed_status = parse_case_status(raw_status)
    if raw_status and not is_status_artifact(raw_status):
        core_fields["case_status"] = raw_status

    # verdict_type from the status; failing that, from the final decisive hearing.
    verdict_type = parsed_status.verdict_type or verdict_from_hearings(
        hearings_timeline["hearings"]
    )
    if verdict_type:
        core_fields["verdict_type"] = verdict_type

    # verdict date: the explicit फैसला मिति field is authoritative for district;
    # fall back to a date embedded in the case_status string.
    verdict_date_bs = fields.get("verdict_date_bs") or parsed_status.verdict_date_bs
    verdict_date_ad = fields.get("verdict_date_ad") or parsed_status.verdict_date_ad
    if verdict_date_bs:
        core_fields["verdict_date_bs"] = verdict_date_bs
        core_fields["verdict_date_ad"] = verdict_date_ad

    extra_data = {
        "enrichment_hearings": hearings_timeline["hearings"],
        "enrichment_timeline": hearings_timeline["timeline"],
    }
    return ParsedEnrichment(
        core_fields=core_fields, extra_data=extra_data, entities=entities
    )


def _extract_detail_fields(soup: BeautifulSoup) -> dict[str, object]:
    """Pull the labelled ``<dl>`` fields (and the ``<h2>`` reg-no fallback)."""
    data: dict[str, object] = {}
    for content_div in soup.find_all("div", class_="content"):
        for dl in content_div.find_all("dl"):
            dts = dl.find_all("dt")
            dds = dl.find_all("dd")
            for dt, dd in zip(dts, dds):
                label = dt.get_text(strip=True).rstrip(":").strip()
                value = dd.get_text(strip=True)
                if not value:
                    continue
                if label == "रजिष्ट्रेशन नं":
                    data["registration_number"] = value
                elif label == "मुद्दाको किसिम":
                    data["case_type"] = value
                elif label == "मुद्दाको बिषय":
                    data["case_subject"] = value
                elif label == "मुद्दाको स्थिति":
                    data["case_status"] = value
                elif label == "फैसला मिति" and value != _VERDICT_DATE_SENTINEL:
                    date_bs = normalize_date(value)
                    data["verdict_date_bs"] = date_bs
                    data["verdict_date_ad"] = bs_to_ad(date_bs)
                elif label == "फैसला गर्ने मा. न्यायाधीश":
                    data["verdict_judge"] = value
                elif label == "पेशी चढेको संख्या":
                    data["hearing_count"] = value

    if "registration_number" not in data:
        for h2 in soup.find_all("h2"):
            text = h2.get_text(strip=True)
            if "रजिष्ट्रेशन नं" in text:
                reg_num = text.split(":")[-1].strip()
                if reg_num:
                    data["registration_number"] = reg_num
    return data


def _extract_entities(soup: BeautifulSoup) -> list[dict[str, object]]:
    """Parse the plaintiff/defendant tables into ``{side, name, address}`` dicts."""
    entities: list[dict[str, object]] = []

    h4_party = None
    for h4 in soup.find_all("h4"):
        if "वादी/प्रतिवादीको विवरण" in h4.get_text():
            h4_party = h4
            break
    if not h4_party:
        return entities

    parent_tr = h4_party.find_parent("tr")
    if not parent_tr:
        return entities
    next_tr = parent_tr.find_next_sibling("tr")
    if not next_tr:
        return entities

    for table in next_tr.find_all("table", class_="record_display"):
        header = table.find("th", colspan="2")
        if not header:
            continue
        header_text = header.get_text(strip=True)
        if "वादी" in header_text and "प्रतिवादी" not in header_text:
            side = "plaintiff"
        elif "प्रतिवादी" in header_text:
            side = "defendant"
        else:
            continue
        for party in _parse_party_table(table):
            entities.append({"side": side, **party})
    return entities


def _parse_party_table(table) -> list[dict[str, object]]:
    """Rows of a party table (first two rows are headers): name + address."""
    parties: list[dict[str, object]] = []
    for row in table.find_all("tr")[2:]:
        cells = row.find_all("td")
        if len(cells) >= 2:
            name = cells[0].get_text(strip=True)
            address = cells[1].get_text(strip=True)
            if name:
                parties.append({"name": name, "address": address or None})
    return parties


def _extract_hearings_timeline(soup: BeautifulSoup) -> dict[str, list[dict[str, str]]]:
    """Pull the पेशी विवरण (hearings) and तारेख विवरण (timeline) tables."""
    data: dict[str, list[dict[str, str]]] = {"hearings": [], "timeline": []}
    for h4 in soup.find_all("h4"):
        h4_text = h4.get_text(strip=True)
        parent = h4.find_parent("tr")
        if not parent:
            continue
        next_row = parent.find_next_sibling("tr")
        if not next_row:
            continue
        table = next_row.find("table", class_="record_display")
        if not table:
            continue
        if "पेशी विवरण" in h4_text:
            data["hearings"] = _parse_hearing_table(table)
        elif "तारेख" in h4_text and "विवरण" in h4_text:
            data["timeline"] = _parse_timeline_table(table)
    return data


def _parse_hearing_table(table) -> list[dict[str, str]]:
    hearings: list[dict[str, str]] = []
    for row in table.find_all("tr")[1:]:
        cells = row.find_all("td")
        if len(cells) >= 5:
            hearings.append(
                {
                    "date": cells[0].get_text(strip=True),
                    "type": cells[1].get_text(strip=True),
                    "division": cells[2].get_text(strip=True),
                    "judge": cells[3].get_text(strip=True),
                    "order": cells[4].get_text(strip=True),
                }
            )
    return hearings


def _parse_timeline_table(table) -> list[dict[str, str]]:
    timeline: list[dict[str, str]] = []
    for row in table.find_all("tr")[1:]:
        cells = row.find_all("td")
        if len(cells) >= 2:
            timeline.append(
                {
                    "date": cells[0].get_text(strip=True),
                    "type": cells[1].get_text(strip=True),
                }
            )
    return timeline
