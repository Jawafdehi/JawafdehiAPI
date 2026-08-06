"""High Court cause-list + enrichment parsers (ported from ngm ``high_court_*``).

Two-stage on the portal, exactly like Special court: discover the day's benches
for a court, then fetch each bench's case table. Portal:
``supremecourt.gov.np/court/<court>/bench_list?pesi_date=<bs>`` (stage 1) and
``.../cause_list_detail`` (stage 2). The per-case *detail* page
(``.../case_details``) is enriched separately. These are the pure parse halves;
the fetch/orchestration lives in the management command.

Unlike Special/Supreme there are 18 high courts, so ``court_identifier`` is a
PARAMETER (not a module constant). v2 shape rules baked in here:

* ``division`` (the ngm ``फाँट`` field) is NOT a ``court_cases`` column — it goes
  to ``extra_data`` only. The ngm enrichment spider dual-wrote it to both
  ``core_fields`` and ``extra_data``; only the ``extra_data`` half is kept.
* ``hearing_count`` is a typed ``IntegerField`` in v2, so the scraped count is
  coerced with :func:`coerce_count` (the ngm side stored it as a string).
* ``case_status`` free-text is normalised through :mod:`courts.case_status`:
  scraped header artifacts are dropped, and ``verdict_type``/``verdict_date`` are
  derived from the status string (falling back to the final decisive hearing).
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
    extract_judges,
    nepali_to_roman_numerals,
    normalize_date,
    normalize_whitespace,
)
from jawafdehi_shared.dates import bs_to_ad

#: ``onclick="send_data('<bench_id>', '<bench_no>', '<hearing_date>')"`` on each
#: bench row of the stage-1 table.
_SEND_DATA_RE = re.compile(r"send_data\('(\d+)',\s*'([^']+)',\s*'(\d+)'\)")

#: A parenthetical suffix on a cause-list case number (``081-CR-0123 (कैद)``) —
#: stripped before canonicalisation (matches ngm ``_clean_case_number``).
_PARENS_RE = re.compile(r"\s*\([^)]*\)\s*")


# ── stage 1: bench discovery ─────────────────────────────────────────────────
def parse_bench_list(html: str) -> list[dict[str, str]]:
    """Discover a court's benches for the day from the bench-list table (stage 1).

    Each bench row carries an ``onclick="send_data(...)"`` with the ``bench_id`` /
    ``bench_no`` needed to POST the stage-2 cause-list request; the judge name is
    in the second cell. The ``जम्माः`` (total) footer row is skipped.
    """
    soup = BeautifulSoup(html, "html.parser")
    bench_table = soup.find(
        "table", class_="table table-striped table-bordered table-hover"
    )
    if not bench_table:
        return []
    tbody = bench_table.find("tbody")
    rows = tbody.find_all("tr") if tbody else []

    benches: list[dict[str, str]] = []
    for row in rows:
        if "जम्माः" in row.get_text():
            continue
        cells = row.find_all("td")
        if len(cells) < 2:
            continue
        # bs4 types an attribute value as `str | list[str]`, because multi-valued
        # attributes (class, rel) parse to a list. `onclick` is never multi-valued,
        # but join rather than assume: a malformed page should not raise TypeError
        # out of the middle of a cause-list parse.
        raw_onclick = row.get("onclick", "")
        onclick = (
            raw_onclick
            if isinstance(raw_onclick, str)
            else " ".join(raw_onclick or [])
        )
        match = _SEND_DATA_RE.search(onclick)
        if not match:
            continue
        benches.append(
            {
                "bench_id": match.group(1),
                "bench_no": match.group(2),
                # A high-court bench can seat 2-3 judges (one per <br> in this cell);
                # extract_judges keeps them ", "-separated instead of glueing the next
                # judge's honorific onto the previous name (the run-on DQ bug).
                "judge_name": extract_judges(cells[1]) or "",
            }
        )
    return benches


# ── stage 2: per-bench cause list ────────────────────────────────────────────
def parse_bench_page(
    html: str,
    *,
    court_identifier: str,
    date_bs: str,
    bench_id: str | None = None,
    bench_no: str | None = None,
    judge_name: str | None = None,
) -> list[tuple[ParsedCase, ParsedHearing]]:
    """Parse one bench's cause-list table (stage 2) into (case, hearing) rows."""
    soup = BeautifulSoup(html, "html.parser")
    hearing_date_ad = bs_to_ad(date_bs)

    # `bool(x) and ...` rather than `x and ...`: the latter evaluates to `x` (a
    # str) when falsy, and bs4's string matcher is a predicate returning bool.
    # Same truthiness for bs4, honest type for the reader.
    bench_type_elem = soup.find("h4", string=lambda x: bool(x) and "इजलास" in x)
    bench_type = (
        normalize_whitespace(bench_type_elem.get_text()) if bench_type_elem else ""
    )
    bench_roman = nepali_to_roman_numerals(bench_no) if bench_no else ""

    case_table = soup.find("table", class_="table table-bordered table-hover")
    if not case_table:
        return []
    tbody = case_table.find("tbody")
    rows = tbody.find_all("tr", class_="data_row") if tbody else []

    parsed_rows: list[tuple[ParsedCase, ParsedHearing]] = []
    for tr in rows:
        parsed = _parse_row(
            tr.find_all("td"),
            court_identifier=court_identifier,
            date_bs=date_bs,
            hearing_date_ad=hearing_date_ad,
            bench_id=bench_id,
            bench_no=bench_no,
            bench_roman=bench_roman,
            bench_type=bench_type,
            judge_name=judge_name,
        )
        if parsed is not None:
            parsed_rows.append(parsed)
    return parsed_rows


def _clean_case_number(cell) -> str:
    """``<br>``-join a wrapped case number, then strip its parenthetical suffix."""
    for br in cell.find_all("br"):
        br.replace_with(" ")
    case_number = normalize_whitespace(cell.get_text())
    return _PARENS_RE.sub("", case_number).strip()


def _parse_row(
    cells,
    *,
    court_identifier: str,
    date_bs: str,
    hearing_date_ad: date | None,
    bench_id: str | None,
    bench_no: str | None,
    bench_roman: str,
    bench_type: str,
    judge_name: str | None,
) -> tuple[ParsedCase, ParsedHearing] | None:
    if len(cells) < 9:
        return None

    case_number = _clean_case_number(cells[4])
    if not case_number:
        return None
    case_number = best_effort_normalize(case_number)

    # Parties: ``<plaintiff> || <defendant>`` when both present, else plaintiff-only.
    parties = normalize_whitespace(cells[5].get_text())
    if "||" in parties:
        plaintiff_raw, defendant_raw = parties.split("||", 1)
    else:
        plaintiff_raw, defendant_raw = parties, ""

    lawyers_text = normalize_whitespace(cells[6].get_text())
    lawyer_names = None if not lawyers_text or lawyers_text == "--" else lawyers_text

    status_cell = cells[8]
    for br in status_cell.find_all("br"):
        br.replace_with("\n")

    registration_date = normalize_date(normalize_whitespace(cells[2].get_text()))
    case = ParsedCase(
        case_number=case_number,
        court_identifier=court_identifier,
        registration_date_bs=registration_date or None,
        registration_date_ad=bs_to_ad(registration_date),
        # Cap to the column width (as district/special/the enrich path do): a
        # mis-parsed cell must truncate, not dead-letter the whole court's scrape.
        case_type=normalize_whitespace(cells[3].get_text())[:200] or None,
        plaintiff=normalize_whitespace(plaintiff_raw) or None,
        defendant=normalize_whitespace(defendant_raw) or None,
        # ``division`` (फाँट) is not a v2 court_cases column → extra_data only.
        extra_data={"division": normalize_whitespace(cells[1].get_text()) or None},
    )

    hearing = ParsedHearing(
        case_number=case_number,
        court_identifier=court_identifier,
        hearing_date_bs=date_bs,
        hearing_date_ad=hearing_date_ad,
        bench=bench_roman or None,
        bench_type=bench_type or None,
        judge_names=judge_name or None,
        lawyer_names=lawyer_names,
        serial_no=nepali_to_roman_numerals(normalize_whitespace(cells[0].get_text()))
        or None,
        case_status=normalize_whitespace(status_cell.get_text()) or None,
        remarks=normalize_whitespace(cells[7].get_text()) or None,
        extra_data={"bench_id": bench_id, "bench_no": bench_no},
    )
    return case, hearing


# ── enrichment: per-case detail page ─────────────────────────────────────────
def parse_high_detail(html: str) -> ParsedEnrichment:
    """Parse a high-court case-detail page into a :class:`ParsedEnrichment`.

    ``core_fields`` carries the court-owned typed columns, ``extra_data`` the
    low-value legacy fields (``division``, ``review_date``, ``court_name``, …) plus
    the parsed ``enrichment_hearings``; ``entities`` is the flattened
    plaintiff/defendant list.
    """
    soup = BeautifulSoup(html, "html.parser")
    core_fields, extra_data = _parse_detail_fields(soup)
    entities = _parse_detail_entities(soup)
    hearings = _parse_detail_hearings(soup)

    # If structured parties couldn't be parsed, stash the raw party text under the
    # legacy Devanagari keys (the importer's DQ pass lifts these into
    # plaintiff/defendant).
    if not entities:
        _stash_raw_parties(soup, extra_data)

    extra_data["enrichment_hearings"] = hearings

    _normalize_case_status(core_fields, hearings)

    return ParsedEnrichment(
        core_fields=core_fields, extra_data=extra_data, entities=entities
    )


def _parse_detail_fields(soup: BeautifulSoup) -> tuple[dict, dict]:
    core_fields: dict = {}
    extra_data: dict = {}

    for row in soup.find_all("div", class_="row"):
        cols = row.find_all("div", class_="col-xs-6")
        if len(cols) != 2:
            continue
        label_elem = cols[0].find("strong")
        value_elem = cols[1].find("p")
        if not label_elem or not value_elem:
            continue

        # Strip a trailing ':' AND '.' — the portal labels carry a trailing dot
        # (e.g. "दर्ता नँ.") that otherwise buried these under raw extra_data keys.
        label = normalize_whitespace(label_elem.get_text()).rstrip(":.").strip()
        value = normalize_whitespace(value_elem.get_text())
        if not value or value == "--":
            continue
        if "वादी" in label or "प्रतिवादी" in label:
            continue

        if label == "दर्ता नँ":
            core_fields["registration_number"] = value[:100]
        elif label in ("दर्ता मिति", "दर्ता मिती"):
            core_fields["registration_date_bs"] = normalize_date(value)
            core_fields["registration_date_ad"] = bs_to_ad(normalize_date(value))
        elif label == "मुद्दाको किसिम":
            core_fields["case_type"] = value[:200]
            extra_data["case_type_display"] = value
        elif label in ("मुद्दाको स्थिति", "मुद्दाको स्थिती"):
            core_fields["case_status"] = value[:100]
            extra_data["raw_status_display"] = value
        elif label in ("फैसला मिति", "फैसला मिती"):
            if value != "**** ** **":
                core_fields["verdict_date_bs"] = normalize_date(value)
                core_fields["verdict_date_ad"] = bs_to_ad(normalize_date(value))
        elif label == "फैसला गर्ने न्यायाधीश":
            # A multi-judge verdict panel is <br>-separated inside this <p>; keep the
            # judges ", "-separated rather than run-on.
            core_fields["verdict_judge"] = (extract_judges(value_elem) or value)[:500]
        elif label == "पेशी चढेको संख्या":
            # v2: hearing_count is a typed int column — coerce the scraped count.
            core_fields["hearing_count"] = coerce_count(value)
        elif label == "रुजु मिती":
            extra_data["review_date"] = value
        elif label == "फाँटवाला":
            extra_data["division_officer"] = value
        elif label == "फाँट":
            # v2: division is NOT a court_cases column — extra_data only (the ngm
            # spider also dual-wrote core_fields["division"]; drop that half).
            extra_data["division"] = value
        elif label == "अदालत":
            extra_data["court_name"] = value
        else:
            extra_data[label.replace(" ", "_").replace(":", "")] = value

    return core_fields, extra_data


def _parse_detail_entities(soup: BeautifulSoup) -> list[dict]:
    """Extract plaintiff/defendant parties as flat ``{side, name, address}`` dicts."""
    plaintiffs: list[dict] = []
    defendants: list[dict] = []

    plaintiff_panel = None
    defendant_panel = None
    for panel in soup.find_all("div", class_="panel-heading"):
        text = panel.get_text()
        if plaintiff_panel is None and "वादीको विवरण" in text:
            plaintiff_panel = panel
        elif defendant_panel is None and "प्रतिवादीहरु" in text:
            defendant_panel = panel

    # Format 1: panel-based (with address columns); each side is searched
    # independently so a defendant panel is found even with no plaintiff panel.
    if plaintiff_panel or defendant_panel:
        if plaintiff_panel:
            _parse_panel(plaintiff_panel, plaintiffs)
        if defendant_panel:
            _parse_panel(defendant_panel, defendants)
        return _flatten_parties(plaintiffs, defendants)

    # Format 2: simple row-based (वादीहरु / प्रतिवादीहरु in col-xs-6 rows).
    for row in soup.find_all("div", class_="row"):
        cols = row.find_all("div", class_="col-xs-6")
        if len(cols) != 2:
            continue
        label_elem = cols[0].find("strong")
        value_elem = cols[1].find("p")
        if not label_elem or not value_elem:
            continue
        label = normalize_whitespace(label_elem.get_text())
        value = normalize_whitespace(value_elem.get_text())
        if not value or value == "--":
            continue
        if "वादी" in label and "प्रतिवादी" not in label:
            target = plaintiffs
        elif "प्रतिवादी" in label:
            target = defendants
        else:
            continue
        names = value.split(",") if "," in value else value.split("/")
        for name in names:
            name = name.strip()
            if name:
                target.append({"name": name[:500], "address": None})

    return _flatten_parties(plaintiffs, defendants)


def _flatten_parties(plaintiffs: list[dict], defendants: list[dict]) -> list[dict]:
    return [
        {"side": "plaintiff", "name": p["name"], "address": p.get("address")}
        for p in plaintiffs
    ] + [
        {"side": "defendant", "name": d["name"], "address": d.get("address")}
        for d in defendants
    ]


def _parse_panel(panel, target: list[dict]) -> None:
    body = panel.find_next("div", class_="panel-body")
    if not body:
        return
    # Skip the header row (नाम / ठेगाना).
    for row in body.find_all("div", class_="row")[1:]:
        cols = row.find_all("div", recursive=False)
        if len(cols) < 2:
            continue
        name = normalize_whitespace(cols[0].get_text())
        address = normalize_whitespace(cols[1].get_text())
        if name and name != "नाम":
            target.append(
                {
                    "name": name[:500],
                    "address": address[:500] if address and address.strip() else None,
                }
            )


def _stash_raw_parties(soup: BeautifulSoup, extra_data: dict) -> None:
    for row in soup.find_all("div", class_="row"):
        cols = row.find_all("div", class_="col-xs-6")
        if len(cols) != 2:
            continue
        label_elem = cols[0].find("strong")
        value_elem = cols[1].find("p")
        if not label_elem or not value_elem:
            continue
        label = normalize_whitespace(label_elem.get_text())
        value = normalize_whitespace(value_elem.get_text())
        if "वादी" in label and "प्रतिवादी" not in label and value:
            extra_data["वादीहरु"] = value
        elif "प्रतिवादी" in label and value:
            extra_data["प्रतिवादीहरु"] = value


def _parse_detail_hearings(soup: BeautifulSoup) -> list[dict]:
    for table in soup.find_all("table", class_="table"):
        headers = [normalize_whitespace(h.get_text()) for h in table.find_all("th")]
        if "सुनवाइ" not in " ".join(headers):
            continue
        hearings: list[dict] = []
        for row in table.find_all("tr")[1:]:
            cells = row.find_all("td")
            if len(cells) >= 4:
                hearings.append(
                    {
                        "hearing_date": normalize_date(
                            normalize_whitespace(cells[0].get_text())
                        ),
                        "judges": extract_judges(cells[1]) or "",
                        "case_status": normalize_whitespace(cells[2].get_text()),
                        "decision_type": normalize_whitespace(cells[3].get_text()),
                    }
                )
        return hearings
    return []


def _normalize_case_status(core_fields: dict, hearings: list[dict]) -> None:
    """Turn the raw ``case_status`` free-text into typed verdict columns (v2).

    Drops scraped header/label artifacts, derives ``verdict_type`` from the status
    string, and — when the status carries no outcome — falls back to the final
    decisive hearing. A verdict date is recovered from the status only when the
    explicit ``फैसला मिति`` label didn't already supply one.
    """
    raw_status = core_fields.get("case_status")
    if is_status_artifact(raw_status):
        core_fields.pop("case_status", None)
        raw_status = None

    parsed = parse_case_status(raw_status)

    verdict_type = parsed.verdict_type or verdict_from_hearings(hearings)
    if verdict_type:
        core_fields["verdict_type"] = verdict_type

    if not core_fields.get("verdict_date_bs") and parsed.verdict_date_bs:
        core_fields["verdict_date_bs"] = parsed.verdict_date_bs
        core_fields["verdict_date_ad"] = parsed.verdict_date_ad
