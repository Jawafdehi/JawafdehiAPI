#!/usr/bin/env python3
"""Extract all per-case data from CIAA Cases Progress Tracker Google Sheets into normalized JSON.

Usage:
    export GOOGLE_CREDENTIALS_FILE=... (optional, uses ~/.google_workspace_mcp/credentials/credentials.json by default)
    python scripts/extract_ciaa_sheet.py [--output-dir /path/to/output]

Output: cases_FY_XXYY.json per fiscal year in the output directory (default: ./data/ciaa/).
"""

import argparse
import json
import os
import re
from datetime import datetime
from pathlib import Path

# Google Sheets
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

# Nepali date conversion
from nepali_datetime import date as nepali_date

SPREADSHEET_ID = "1O8U9VA1FSCSocGwJHYgbm8sVoMkjYnQbOADvOlBhSJ0"

# Sheet definitions: (tab_name, fiscal_year_label, is_per_case, header_row_index, format_type)
SHEET_DEFS = [
    # Per-case detailed sheets (these contain actual case records)
    ("2081/2082", "2081_2082", True, 0, "detailed_v2"),
    ("2080/2081", "2080_2081", True, 0, "detailed_v1"),
    ("2076/2077", "2076_2077", True, 1, "bribery_simple"),
    ("75/76", "2075_2076", True, 1, "bribery_simple"),
    ("2073/2074", "2073_2074", True, 1, "bribery_simple"),
    # The '080/081 cases to work' tab is a subset of 2080/2081
    ("080/081 cases to work", "2080_2081_work", True, 5, "work_list"),
    # Summary/aggregate sheets - NOT per-case data
    ("79/80", "2079_2080", False, 1, "summary"),
    ("2078/2079", "2078_2079", False, 1, "summary"),
    ("74/75", "2074_2075", False, 1, "summary"),
    ("71/72", "2071_2072", False, 1, "summary"),
    # Special sheets
    ("CaseProgress", "case_progress", False, 0, "case_progress"),
    ("पनुरावेदन", "appeals", True, 1, "appeals"),
    ("082/083", "2082_2083", True, 0, "minimal"),
    ("Sheet39", "sheet39", False, 0, "empty"),
]


def get_credentials():
    """Get Google API credentials from the MCP token file."""
    creds_file = os.environ.get(
        "GOOGLE_CREDENTIALS_FILE",
        os.path.expanduser("~/.google_workspace_mcp/credentials/credentials.json"),
    )
    if not os.path.exists(creds_file):
        raise FileNotFoundError(
            f"Credentials file not found: {creds_file}\n"
            "Set GOOGLE_CREDENTIALS_FILE or ensure the default path exists."
        )
    try:
        with open(creds_file) as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in credentials file: {creds_file}") from e

    required_keys = {
        "token",
        "refresh_token",
        "token_uri",
        "client_id",
        "client_secret",
        "scopes",
    }
    missing = required_keys - data.keys()
    if missing:
        raise ValueError(f"Credentials file missing required keys: {missing}")

    creds = Credentials(
        token=data["token"],
        refresh_token=data["refresh_token"],
        token_uri=data["token_uri"],
        client_id=data["client_id"],
        client_secret=data["client_secret"],
        scopes=data["scopes"],
    )
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception as e:
            print(
                f"Warning: failed to refresh credentials: {e}",
                file=__import__("sys").stderr,
            )
    return creds


def parse_nepali_date_bs(bs_str):
    """Parse a BS date string to (year, month, day) or None.
    Handles formats: YYYY/MM/DD, YYYY-MM-DD, YYYY.MM.DD, M/D/YYYY, MM/DD/YYYY
    """
    if not bs_str or not isinstance(bs_str, str):
        return None
    bs_str = bs_str.strip()
    if not bs_str:
        return None

    # Try YYYY/MM/DD, YYYY-MM-DD, YYYY.MM.DD
    m = re.match(r"(\d{4})[/\-\.](\d{1,2})[/\-\.](\d{1,2})$", bs_str)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 2000 <= y <= 2100:
            return (y, mo, d)

    # Try M/D/YYYY or MM/DD/YYYY (e.g., "4/6/2076" = Baisakh 6, 2076)
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})$", bs_str)
    if m:
        mo, d, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 2000 <= y <= 2100:
            return (y, mo, d)

    return None


def bs_to_ad(year_bs, month_bs, day_bs):
    """Convert BS date to AD date string."""
    try:
        d = nepali_date(year_bs, month_bs, day_bs)
        ad = d.to_datetime_date()
        return ad.strftime("%Y-%m-%d")
    except Exception:
        return None


def convert_date_field(bs_str):
    """Parse a BS date string to AD date, returning both formats."""
    parsed = parse_nepali_date_bs(bs_str)
    if parsed:
        y, mo, d = parsed
        ad_date = bs_to_ad(y, mo, d)
        bs_formatted = f"{y}-{mo:02d}-{d:02d}"
        return {"bs": bs_formatted, "ad": ad_date}
    return {"bs": bs_str if bs_str else None, "ad": None}


def clean_text(text):
    """Clean and normalize text fields."""
    if not text:
        return ""
    if isinstance(text, str):
        # Unescape common escape sequences from Sheets API
        text = text.replace("\\n", "\n").replace("\\t", " ")
        # Normalize whitespace
        text = re.sub(r"\s*\n\s*", "\n", text)
        return text.strip()
    return str(text)


def parse_bigo(value):
    """Parse bigo amount string to numeric (float or None).
    Handles: "1,00,000 ", "13,160,000.00", "2602556.11", "1,47,10,85,483", ""
    """
    if not value or not isinstance(value, str):
        return None
    value = value.strip().replace(",", "")
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def extract_defendants_v1(row, headers):
    """Extract defendants from the 'detailed_v1' format (2080/2081).
    In this format, multi-defendant rows have empty key fields but repeat defendant info.
    """
    return {
        "name": clean_text(row.get("प्रतिवादीको_नाम", "")),
        "position_office": clean_text(row.get("पद_र_कार्यालय", "")),
        "offence_law": clean_text(row.get("कसुर_सजाय_मागदाबी", "")),
        "bigo": parse_bigo(row.get("बिगो_रकम")),
    }


def extract_defendants_v2(row, headers):
    """Extract defendants from the 'detailed_v2' format (2081/2082)."""
    return {
        "name": clean_text(row.get("प्रतिवादीको_नाम", "")),
        "position_office": clean_text(row.get("पद_र_कार्यालय", "")),
        "offence_law": clean_text(row.get("कसुर_सजाय_मागदाबी", "")),
        "bigo": parse_bigo(row.get("बिगो_रकम")),
    }


def extract_defendants_simple(row, headers):
    """Extract defendant info from the 'bribery_simple' format (2076/2077, 75/76, 2073/2074)."""
    return {
        "name": clean_text(row.get("प्रतिवादीको_नाम", "")),
        "position_office": clean_text(row.get("पद_र_कार्यालय", "")),
        "bigo": parse_bigo(row.get("बिगो_रकम")),
    }


def normalize_row(headers, values, sheet_name, format_type):
    """Convert a raw row of values to a normalized case dict."""

    # Build keyed dict from header-value pairs with normalized headers
    row = {}
    for i, h in enumerate(headers):
        if h and isinstance(h, str):
            norm_h = re.sub(r"[\s\-]+", "_", h.strip())
            row[norm_h] = values[i] if i < len(values) else ""
        elif h:
            row[h] = values[i] if i < len(values) else ""

    case = {
        "_source_sheet": sheet_name,
        "_source_format": format_type,
    }

    if format_type == "detailed_v2":
        # 2081/2082: 12 columns
        case.update(
            {
                "serial_no": clean_text(row.get("क्र_सं", "")),
                "complaint_summary": clean_text(row.get("उजुरीको_व्यहोरा", "")),
                "investigation_finding": clean_text(
                    row.get("अनुसन्धानबाट_पुष्टि_भएको_व्यहोरा", "")
                ),
                "commission_decision_date": convert_date_field(
                    row.get("आयोगको_निर्णय.आयोगको_निर्णय_मिति", "")
                ),
                "indictment_filing_date": convert_date_field(
                    row.get("आयोगको_निर्णय.आरोपपत्र_दायर_मिति", "")
                ),
                "case_number": clean_text(row.get("आयोगको_निर्णय.मुद्दा_नं", "")),
                "defendant_count": row.get("आयोगको_निर्णय.प्रतिवादी_सङ्ख्या", ""),
                "defendant": extract_defendants_v2(row, headers),
                "case_type": clean_text(row.get("case type", "")),
            }
        )

    elif format_type == "detailed_v1":
        # 2080/2081: columns up to W (illegal benefit)
        case.update(
            {
                "serial_no": clean_text(row.get("क्र_सं", "")),
                "complaint_summary": clean_text(row.get("उजुरीको_व्यहोरा", "")),
                "investigation_finding": clean_text(
                    row.get("अनुसन्धानबाट_पुष्टि_भएको_व्यहोरा", "")
                ),
                "commission_decision_date": convert_date_field(
                    row.get("आयोगको_निर्णय_मिति", "")
                ),
                "indictment_filing_date": convert_date_field(
                    row.get("आरोपपत्र_दायर_मिति", "")
                ),
                "case_number": clean_text(row.get("मुद्दा_नं", "")),
                "defendant_count": row.get("प्रतिवादी_सङ्ख्या", ""),
                "defendant": extract_defendants_v1(row, headers),
                "case_type": clean_text(row.get("illegal benefit", "")),
            }
        )

    elif format_type == "bribery_simple":
        # 2076/2077, 75/76, 2073/2074: simpler format
        case.update(
            {
                "serial_no": clean_text(row.get("क्र_सं", "")),
                "commission_decision_date": convert_date_field(
                    row.get("आयोगको_निर्णय_मिति", "")
                ),
                "indictment_filing_date": convert_date_field(
                    row.get("आरोपपत्र_दायर_मिति", "")
                ),
                "defendant_count": row.get("प्रतिवादी_सङ्ख्या", ""),
                "defendant": extract_defendants_simple(row, headers),
            }
        )

    elif format_type == "work_list":
        # 080/081 cases to work: prioritized subset
        # Header normalization replaces spaces/hyphens with underscores
        case.update(
            {
                "serial_no": clean_text(row.get("क्र.सं.", "")),
                "case_number": clean_text(row.get("मुद्दा_नं", "")),
                "complaint_summary": clean_text(row.get("उजुरीको_व्यहोरा", "")),
                "investigation_finding": clean_text(
                    row.get("अनुसन्धानबाट_पुष्टि_भएको_व्यहोरा", "")
                ),
                "defendant_name": clean_text(row.get("प्रतिवादीको_नाम_(पहिलो)", "")),
                "bigo": parse_bigo(row.get("बिगो_रकम_(रु.)", "")),
                "full_text_available": clean_text(row.get("पुर्णपाठ", "")),
            }
        )

    elif format_type == "appeals":
        case.update(
            {
                "serial_no": clean_text(row.get("क्र_सं", "")),
                "special_court_case_no": clean_text(
                    row.get("विशेष_अदालतको_मुद्दा_नं", "")
                ),
                "special_court_verdict_date": convert_date_field(
                    row.get("विशेष_अदालतको_फैसला_मिति", "")
                ),
                "commission_appeal_decision_date": convert_date_field(
                    row.get("आयोगको_पुनरावेदन_गर्ने_निर्णय_मिति", "")
                ),
                "appeal_registration_date": convert_date_field(
                    row.get("पुनरावेदन_दर्ता_मिति", "")
                ),
                "defendant_details": clean_text(
                    row.get("प्रत्यार्थी_प्रतिवादीको_विवरण", "")
                ),
                "defendant_count": row.get("प्रत्यार्थी_प्रतिवादीको_सङ्ख्या", ""),
                "supreme_court_case_no": clean_text(
                    row.get("सर्वोच्च_अदालतको_मुद्दा_नं", "")
                ),
            }
        )

    elif format_type == "minimal":
        # 082/083 only has case number (e.g., "080-CR-0058")
        case.update(
            {
                "case_number": values[0] if values else "",
            }
        )

    return case


def is_header_row(row_values):
    """Check if a row looks like a header (all text, no data)."""
    if not row_values:
        return False
    # Look for known header patterns
    header_keywords = {
        "क्र_सं",
        "क्र.सं.",
        "क्र.सं",
        "उजुरीको_व्यहोरा",
        "उजुरीको व्यहोरा",
        "आयोगको_निर्णय_मिति",
        "मुद्दा_नं",
        "मुद्दा नं",
        "प्रतिवादीको_नाम",
        "बिगो_रकम",
        "case type",
        "bribery",
        "विषय",
        "description",
    }
    text = " ".join(str(v) for v in row_values if v)
    return any(kw in text for kw in header_keywords)


def extract_sheet(service, sheet_name):
    """Extract all rows from a sheet."""
    range_name = f"'{sheet_name}'!A:ZZ"
    result = (
        service.spreadsheets()
        .values()
        .get(
            spreadsheetId=SPREADSHEET_ID,
            range=range_name,
            valueRenderOption="FORMATTED_VALUE",
        )
        .execute()
    )
    return result.get("values", [])


def group_into_cases(rows, headers, sheet_name, format_type):
    """Group multi-defendant rows into single case objects.

    In detailed_v1 and detailed_v2 formats, the first row of a case
    has serial_no and case identifiers filled in, while subsequent
    defendant rows have those fields empty.
    """
    if format_type not in ("detailed_v1", "detailed_v2"):
        # For simple formats, each row is one case (with sometimes
        # multiple defendants in one field)
        cases = []
        for vals in rows:
            case = normalize_row(headers, vals, sheet_name, format_type)
            # Skip empty or header-only rows
            if not any(v for v in vals if v and str(v).strip()):
                continue
            if is_header_row(vals):
                continue
            cases.append(case)
        return cases

    # Group multi-defendant case rows
    cases = []
    current_case = None
    row_count = 0

    for vals in rows:
        # Skip empty rows
        if not vals or not any(v for v in vals if v and str(v).strip()):
            continue

        # Build keyed row
        row = {}
        for i, h in enumerate(headers):
            if h:
                row[h] = vals[i] if i < len(vals) else ""

        # Check if this is a new case (has serial_no or case_number)
        sn_key = (
            "क्र_सं" if "क्र_सं" in row else ("क्र.सं." if "क्र.सं." in row else None)
        )
        case_no = clean_text(
            row.get("मुद्दा_नं", "") or row.get("आयोगको_निर्णय.मुद्दा_नं", "")
        )

        is_new = False
        if sn_key:
            sn = clean_text(row.get(sn_key, ""))
            if sn and re.match(r"^[\d१२३४५६७८९०]+$", sn):
                is_new = True
        if not is_new and case_no:
            is_new = True

        if is_new:
            if current_case:
                cases.append(current_case)
            current_case = normalize_row(headers, vals, sheet_name, format_type)
            row_count += 1
        elif current_case is not None:
            # Add as additional defendant
            extract_fn = (
                extract_defendants_v2
                if format_type == "detailed_v2"
                else extract_defendants_v1
            )
            d = extract_fn(row, headers)
            if d["name"] or d["position_office"]:
                if "additional_defendants" not in current_case:
                    current_case["additional_defendants"] = []
                current_case["additional_defendants"].append(d)

    if current_case:
        cases.append(current_case)

    return cases


def main():
    parser = argparse.ArgumentParser(
        description="Extract CIAA sheet data to normalized JSON"
    )
    parser.add_argument(
        "--output-dir", default="./data/ciaa", help="Output directory for JSON files"
    )
    parser.add_argument(
        "--format", default="json", help="Output format (json or jsonl)"
    )
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    print("Authenticating with Google Sheets API...")
    creds = get_credentials()
    service = build("sheets", "v4", credentials=creds)

    summary = {}

    for tab_name, fy_label, is_per_case, header_row, fmt_type in SHEET_DEFS:
        print(f"\nReading {tab_name} ({fmt_type})...")
        raw = extract_sheet(service, tab_name)

        if not raw:
            print(f"  WARNING: No data in {tab_name}")
            summary[fy_label] = {"rows": 0, "cases": 0}
            continue

        # Find the actual header row (might not be row 0)
        header_idx = header_row
        headers = raw[header_idx] if header_idx < len(raw) else []

        # For detailed sheets, clean up header names
        if fmt_type in ("detailed_v1", "detailed_v2"):
            # [''', 'bribery'] style headers at the top - find the real header
            if not any("क्र" in str(h) for h in headers):
                for i, r in enumerate(raw[header_idx:], start=header_idx):
                    if any("क्र" in str(c) for c in r):
                        headers = r
                        header_idx = i
                        break

        print(f"  Raw rows: {len(raw)}, Header row: {header_idx}")
        print(f"  Headers: {headers}")

        # For per-case sheets, extract cases
        if is_per_case:
            data_rows = raw[header_idx + 1 :]
            print(f"  Data rows (after header): {len(data_rows)}")
            cases = group_into_cases(data_rows, headers, tab_name, fmt_type)
            summary[fy_label] = {"rows": len(data_rows), "cases": len(cases)}

            ext = "jsonl" if args.format == "jsonl" else "json"
            output_file = output_dir / f"cases_{fy_label}.{ext}"
            if args.format == "jsonl":
                with open(output_file, "w", encoding="utf-8") as f:
                    for case in cases:
                        f.write(
                            json.dumps(case, ensure_ascii=False, default=str) + "\n"
                        )
            else:
                with open(output_file, "w", encoding="utf-8") as f:
                    json.dump(cases, f, ensure_ascii=False, indent=2, default=str)
            print(f"  Wrote {len(cases)} cases to {output_file}")
        else:
            # Summary sheets: save raw data with metadata
            output_file = output_dir / f"summary_{fy_label}.json"
            with open(output_file, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "sheet": tab_name,
                        "fiscal_year": fy_label,
                        "format": fmt_type,
                        "headers": headers,
                        "rows": raw[header_idx:],
                    },
                    f,
                    ensure_ascii=False,
                    indent=2,
                    default=str,
                )
            print(f"  Wrote summary to {output_file}")

    # Save extraction manifest
    manifest = {
        "spreadsheet_id": SPREADSHEET_ID,
        "spreadsheet_name": "CIAA Cases Progress Tracker",
        "extraction_date": datetime.now().isoformat(),
        "sheets": summary,
    }
    manifest_file = output_dir / "extraction_manifest.json"
    with open(manifest_file, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    print(f"\nManifest: {manifest_file}")

    total_cases = sum(v.get("cases", 0) for v in summary.values())
    total_rows = sum(v.get("rows", 0) for v in summary.values())
    print(f"\nTotal: {total_rows} raw rows, {total_cases} case records extracted")
    print("Done.")


if __name__ == "__main__":
    main()
