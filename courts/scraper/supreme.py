"""Supreme Court cause-list + enrichment parsers (ported from ngm ``supreme_*``).

Single court, single-stage per date: the portal serves the weekly supplementary
cause list at ``supremecourt.gov.np/lic/sys.php?d=reports&f=weekly_suppli_public``.
Its 10-column table maps to one CourtCase + one CourtCaseHearing per row. The
detail page (``…&f=case_details``) carries the enrichment: a basic-info table and
a parties table (both ``class="table-hover"``) plus hearing/timeline tables.

These are the pure parse halves; the fetch/orchestration lives in the management
command. Two v2-shape rules the legacy scraper predates:

* ``division`` (Supreme's फाँट/इजलास) is NOT a column in the v2 ``court_cases``
  projection — it goes to ``extra_data``, never a top-level field.
* Supreme's ``case_status`` never states an outcome: it is a paren-date form
  (``फैसला (मिती: …)``) or, for ~103k rows, the mis-scraped column header
  ``आदेश /फैसलाको किसिम``. The header is dropped (``is_status_artifact``) rather
  than stored as a status, and the verdict is recovered from the final decisive
  hearing (``verdict_from_hearings``) when the status itself yields none.
"""

from __future__ import annotations

import re
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

COURT_ID = "supreme"

# The "no verdict yet" sentinel the portal stores in the फैसला मिती cell — never
# store it as a real date (matches ngm ``_map_field``).
_VERDICT_DATE_SENTINEL = "**** ** **"


# ── cause-list (weekly supplementary) ────────────────────────────────────────


def parse_cause_list(
    html: str, *, date_bs: str
) -> list[tuple[ParsedCase, ParsedHearing]]:
    """Parse one day's cause list into ``(case, hearing)`` rows.

    Supreme is a single court with a single-stage causelist per date, so this is
    the whole listing parse (no per-bench fan-out). Returns ``[]`` when the day's
    case table is absent (empty/blocked page) so the caller marks the date done.
    """
    soup = BeautifulSoup(html, "html.parser")
    case_table = _find_case_table(soup)
    if case_table is None:
        return []

    hearing_date_ad = bs_to_ad(date_bs)
    rows: list[tuple[ParsedCase, ParsedHearing]] = []
    for tr in _find_case_rows(case_table):
        parsed = _parse_row(
            tr.find_all("td"), date_bs=date_bs, hearing_date_ad=hearing_date_ad
        )
        if parsed is not None:
            rows.append(parsed)
    return rows


def _find_case_table(soup: BeautifulSoup):
    """Locate the day's case table (three fallbacks, ported verbatim from ngm).

    The portal's markup drifts, so match on (1) the exact table attributes, then
    (2) the ``#FFCC00`` header row carrying क्र/मुद्दा नं/पक्ष, then (3) any table
    whose first row has exactly 10 cells. Each candidate is validated to have a
    10-cell header before it is accepted.
    """
    table = soup.find(
        "table",
        {
            "width": "100%",
            "border": "0",
            "cellspacing": "0",
            "bordercolor": "#ffffff",
        },
    )
    if table and _validate_case_table(table):
        return table

    all_tables = soup.find_all("table")
    for table in all_tables:
        header_row = table.find("tr", bgcolor="#FFCC00")
        if not header_row:
            trs = table.find_all("tr")
            if trs and trs[0].get("bgcolor") == "#FFCC00":
                header_row = trs[0]
        if header_row:
            header_text = header_row.get_text()
            if (
                "क्र" in header_text
                and "मुद्दा नं" in header_text
                and "पक्ष" in header_text
                and _validate_case_table(table)
            ):
                return table

    for table in all_tables:
        trs = table.find_all("tr")
        if not trs:
            continue
        cells = trs[0].find_all(["td", "th"])
        if len(cells) == 10 and _validate_case_table(table):
            return table

    return None


def _validate_case_table(table) -> bool:
    """A valid case table has ≥2 rows and a 10-cell header row."""
    if not table:
        return False
    trs = table.find_all("tr")
    if len(trs) < 2:
        return False
    header_cells = trs[0].find_all(["td", "th"])
    return len(header_cells) == 10


def _find_case_rows(table):
    """Data rows are the ``bgcolor="#ffffff"`` rows (the header is ``#FFCC00``)."""
    return table.find_all("tr", bgcolor="#ffffff")


def _clean_case_number(case_number: str) -> str:
    """Strip a trailing parenthetical (e.g. ``… (पुनरावेदन)``) off the number."""
    if not case_number:
        return case_number
    return re.sub(r"\s*\([^)]*\)\s*", "", case_number).strip()


def _clean_division(division: str) -> str:
    """Trim the portal's ``- <division> _`` decoration off the फाँट cell."""
    if not division:
        return division
    cleaned = division.strip()
    if cleaned.startswith("- "):
        cleaned = cleaned[2:]
    if cleaned.endswith(" _"):
        cleaned = cleaned[:-2]
    return cleaned.strip()


def _parse_judges(cell) -> str | None:
    """Newline-join a judges cell, honouring its ``<br>`` line breaks."""
    if not cell:
        return None
    for br in cell.find_all("br"):
        br.replace_with("\n")
    names = [
        normalize_whitespace(name)
        for name in cell.get_text().split("\n")
        if normalize_whitespace(name)
    ]
    return "\n".join(names) if names else None


def _parse_row(
    cells,
    *,
    date_bs: str,
    hearing_date_ad: date | None,
) -> tuple[ParsedCase, ParsedHearing] | None:
    if len(cells) < 10:
        return None

    case_number = _clean_case_number(normalize_whitespace(cells[5].get_text()))
    if not case_number:
        return None
    # Canonicalise to the composite-PK join form (Devanagari→ASCII, zero-padded).
    case_number = best_effort_normalize(case_number)

    division = _clean_division(normalize_whitespace(cells[1].get_text()))
    registration_date = normalize_date(cells[2].get_text())

    parties = normalize_whitespace(cells[6].get_text())
    plaintiff = ""
    defendant = ""
    if "||" in parties:
        left, right = parties.split("||", 1)
        plaintiff = normalize_whitespace(left)
        defendant = normalize_whitespace(right)
    else:
        # Best-effort fallback (ported): keep the whole cell as plaintiff rather
        # than dropping the row when the ``||`` party delimiter is missing.
        plaintiff = parties

    case = ParsedCase(
        case_number=case_number,
        court_identifier=COURT_ID,
        registration_date_bs=registration_date or None,
        registration_date_ad=bs_to_ad(registration_date),
        case_type=normalize_whitespace(cells[4].get_text()) or None,
        plaintiff=plaintiff or None,
        defendant=defendant or None,
        # ``division`` is NOT a v2 court_cases column → extra_data, never top-level.
        extra_data={"division": division or None},
    )

    judges_cannot_hear = _parse_judges(cells[7])
    judges_must_hear = _parse_judges(cells[8])
    hearing = ParsedHearing(
        case_number=case_number,
        court_identifier=COURT_ID,
        hearing_date_bs=date_bs,
        hearing_date_ad=hearing_date_ad,
        bench_type=normalize_whitespace(cells[3].get_text()) or None,
        serial_no=nepali_to_roman_numerals(normalize_whitespace(cells[0].get_text()))
        or None,
        judge_names=judges_must_hear,
        remarks=normalize_whitespace(cells[9].get_text()) or None,
        extra_data={
            "judges_cannot_hear": judges_cannot_hear,
            "judges_must_hear": judges_must_hear,
        },
    )
    return case, hearing


# ── detail-page enrichment ───────────────────────────────────────────────────


def _split_parties(text: str) -> list[str]:
    """Split a party cell (``… समेत``, ``/``- and ``,``-separated) into names."""
    text = text.replace("समेत", "").strip()
    slash_parts = [p.strip() for p in text.split("/") if p.strip()]

    parties: list[str] = []
    for part in slash_parts:
        comma_parts = [p.strip() for p in part.split(",") if p.strip()]
        if comma_parts:
            parties.extend(comma_parts)
        elif part:
            parties.append(part)

    return parties if parties else ([text] if text else [])


def _map_field(data: dict, label: str, value: str) -> None:
    """Map a Nepali basic-info label to a standardised field in ``data``."""
    if label in ["दर्ता नँ", "दर्ता नँ .", "रजिष्ट्रेशन नं"]:
        data["registration_number"] = value[:100]

    elif label in ["दर्ता मिती", "दर्ता मिति"]:
        data["registration_date_bs"] = normalize_date(value)
        if value:
            data["registration_date_ad"] = bs_to_ad(normalize_date(value))

    elif label in ["मुद्दाको किसिम", "मुद्दा", "मुद्दाको बिषय"]:
        if "case_type" not in data:
            data["case_type"] = value[:200]
        if "case_subject" not in data:
            data["case_subject"] = value

    elif label in ["मुद्दाको स्थिती", "मुद्दाको स्थिति"]:
        data["case_status"] = value[:100]

    elif label in ["फैसला मिती", "फैसला मिति", "निर्णय मिति"]:
        # Don't store the "no verdict yet" sentinel as a fake date — leave NULL.
        if value and value != _VERDICT_DATE_SENTINEL:
            data["verdict_date_bs"] = normalize_date(value)
            data["verdict_date_ad"] = bs_to_ad(normalize_date(value))

    elif label in ["फैसला", "आदेश /फैसलाको किसिम"]:
        data["verdict_type"] = value[:100]

    elif label in ["फैसला गर्ने मा. न्यायाधीश", "न्यायाधीश"]:
        data["verdict_judge"] = value[:200]

    elif label in ["फाँट", "इजलास"]:
        data["division"] = value[:100]

    elif label in ["पेशी चढेको संख्या"]:
        data["hearing_count"] = value[:20]


def parse_basic_info_table(soup: BeautifulSoup) -> dict:
    """Extract the basic-info fields from the first ``table-hover`` table.

    Rows come as either a 4-cell (two label:value pairs) or a 2-cell layout; a
    header (``<th>``) row is skipped. Trailing ``:।.`` are stripped off labels.
    """
    data: dict = {}
    tables = soup.find_all("table", class_="table-hover")
    if not tables:
        return data

    for row in tables[0].find_all("tr"):
        if row.find("th"):
            continue
        cells = row.find_all("td")
        if len(cells) == 4:
            for label_cell, value_cell in ((cells[0], cells[1]), (cells[2], cells[3])):
                label = normalize_whitespace(label_cell.get_text())
                value = normalize_whitespace(value_cell.get_text())
                if label and value:
                    _map_field(data, label.rstrip(":।.").strip(), value)
        elif len(cells) == 2:
            label = normalize_whitespace(cells[0].get_text())
            value = normalize_whitespace(cells[1].get_text())
            if label and value:
                _map_field(data, label.rstrip(":।.").strip(), value)

    return data


def parse_parties(soup: BeautifulSoup) -> dict[str, list[dict]]:
    """Extract plaintiff/defendant lists from the first ``table-hover`` table."""
    entities: dict[str, list[dict]] = {"plaintiffs": [], "defendants": []}
    tables = soup.find_all("table", class_="table-hover")
    if not tables:
        return entities

    def _collect(label: str, value: str) -> None:
        label = normalize_whitespace(label).rstrip(":।.").strip()
        value = normalize_whitespace(value)
        if not value:
            return
        if label in ["वादीहरु", "वादी"]:
            target = entities["plaintiffs"]
            skip = {"वादीहरु", "वादी"}
        elif label in ["प्रतिवादीहरु", "प्रतिवादी"]:
            target = entities["defendants"]
            skip = {"प्रतिवादीहरु", "प्रतिवादी"}
        else:
            return
        for party in _split_parties(value):
            if party and party not in skip:
                target.append({"name": party[:500], "address": None})

    for row in tables[0].find_all("tr"):
        cells = row.find_all("td")
        if len(cells) == 4:
            _collect(cells[0].get_text(), cells[1].get_text())
            _collect(cells[2].get_text(), cells[3].get_text())
        elif len(cells) == 2:
            _collect(cells[0].get_text(), cells[1].get_text())

    return entities


def parse_hearings_and_timeline(soup: BeautifulSoup) -> dict[str, list[dict]]:
    """Parse the hearing-schedule and tareekh-timeline tables (by header text)."""
    data: dict[str, list[dict]] = {"hearings": [], "timeline": []}

    for table in soup.find_all("table"):
        header_row = table.find("tr")
        if not header_row:
            continue
        headers = [
            normalize_whitespace(cell.get_text())
            for cell in header_row.find_all(["th", "td"])
        ]

        if any("सुनवाइ मिती" in h for h in headers) and any(
            "न्यायाधीश" in h for h in headers
        ):
            for row in table.find_all("tr")[1:]:
                cells = row.find_all("td")
                if len(cells) >= 2:
                    hearing_date = normalize_whitespace(cells[0].get_text())
                    judges = normalize_whitespace(cells[1].get_text())
                    if judges and hearing_date and hearing_date not in [
                        "सुनवाइ मिती",
                        "मिती",
                    ]:
                        entry = {
                            "date": normalize_date(hearing_date),
                            "judges": judges,
                            "type": "hearing",
                        }
                        if len(cells) >= 3:
                            status = normalize_whitespace(cells[2].get_text())
                            if status and status not in ["मुद्दाको स्थिती", "स्थिती"]:
                                entry["status"] = status
                        if len(cells) >= 4:
                            order_type = normalize_whitespace(cells[3].get_text())
                            if order_type and order_type not in [
                                "आदेश /फैसलाको किसिम",
                                "",
                            ]:
                                entry["order_type"] = order_type
                        data["hearings"].append(entry)

        elif any("तारेख मिती" in h for h in headers) and any(
            "विवरण" in h for h in headers
        ):
            for row in table.find_all("tr")[1:]:
                cells = row.find_all("td")
                if len(cells) >= 2:
                    tareekh_date = normalize_whitespace(cells[0].get_text())
                    details = normalize_whitespace(cells[1].get_text())
                    if tareekh_date and tareekh_date not in ["तारेख मिती", "मिती"]:
                        entry = {
                            "date": normalize_date(tareekh_date),
                            "details": details if details else None,
                        }
                        if len(cells) >= 3:
                            event_type = normalize_whitespace(cells[2].get_text())
                            if event_type and event_type not in ["तारेखको किसिम", ""]:
                                entry["type"] = event_type
                        if "type" not in entry:
                            entry["type"] = details if details else "पेशी तारेख"
                        data["timeline"].append(entry)

    return data


def parse_supreme_detail(html: str) -> ParsedEnrichment:
    """Parse a Supreme detail page into a :class:`ParsedEnrichment`.

    Composes the three ported table parsers into the court-agnostic enrichment
    shape: typed ``core_fields`` (case_status/verdict_*/case_subject/hearing_count/
    verdict_judge/registration_*), ``extra_data`` (enrichment_hearings/timeline +
    the non-column ``division``), and side-tagged ``entities``. The status/verdict
    reconciliation is the Supreme-specific part (see the module docstring).
    """
    soup = BeautifulSoup(html, "html.parser")
    basic = parse_basic_info_table(soup)
    parties = parse_parties(soup)
    hearings_timeline = parse_hearings_and_timeline(soup)

    enrichment_hearings = hearings_timeline.get("hearings", [])
    extra_data: dict = {
        "enrichment_hearings": enrichment_hearings,
        "enrichment_timeline": hearings_timeline.get("timeline", []),
    }
    # ``division`` is a legacy Supreme field, NOT a v2 court_cases column.
    if basic.get("division"):
        extra_data["division"] = basic["division"]

    core_fields: dict = {}
    for key in (
        "registration_number",
        "registration_date_bs",
        "registration_date_ad",
        "case_type",
        "case_subject",
        "verdict_judge",
    ):
        if basic.get(key) is not None:
            core_fields[key] = basic[key]

    # hearing_count is a scraped string in the corpus → coerce to the int column.
    if basic.get("hearing_count") is not None:
        core_fields["hearing_count"] = coerce_count(basic["hearing_count"])

    # case_status: Supreme's status is a paren-date or a mis-scraped column header
    # (आदेश /फैसलाको किसिम). Never store the header artifact as a status (DQ-01).
    raw_status = basic.get("case_status")
    parsed_status = parse_case_status(raw_status)
    if raw_status and not is_status_artifact(raw_status):
        core_fields["case_status"] = raw_status

    # verdict_type: the paren-date/header status carries no outcome, so recover it
    # from the final decisive hearing; an explicit basic-info "फैसला" is last resort.
    verdict_type = (
        parsed_status.verdict_type
        or verdict_from_hearings(enrichment_hearings)
        or basic.get("verdict_type")
    )
    if verdict_type:
        core_fields["verdict_type"] = verdict_type

    # verdict date: explicit "फैसला मिती" label, else recovered from the paren-date.
    verdict_date_bs = basic.get("verdict_date_bs") or parsed_status.verdict_date_bs
    verdict_date_ad = basic.get("verdict_date_ad") or parsed_status.verdict_date_ad
    if verdict_date_bs:
        core_fields["verdict_date_bs"] = verdict_date_bs
        core_fields["verdict_date_ad"] = verdict_date_ad

    entities: list[dict] = [
        {"side": "plaintiff", "name": p["name"], "address": p.get("address")}
        for p in parties.get("plaintiffs", [])
    ] + [
        {"side": "defendant", "name": d["name"], "address": d.get("address")}
        for d in parties.get("defendants", [])
    ]

    return ParsedEnrichment(
        core_fields=core_fields, extra_data=extra_data, entities=entities
    )
