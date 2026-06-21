#!/usr/bin/env python3
"""Extract structured JSON from 28th CIAA annual report (FY 2074/75) likhit markdown.

Supports three section types:
  Type A: Simple bribery table  (2.6.1)
  Type C: Mixed narrative + defendant table (2.6.6 revenue leakage, 2.6.7 misc)
"""

import argparse
import json
import re

FISCAL_YEAR = "2074/75"
REPORT_NAME = "28th CIAA Annual Report"


def to_arabic(text):
    table = str.maketrans("०१२३४५६७८९", "0123456789")
    return text.translate(table)


def arabic_int(s):
    s = to_arabic(str(s)).strip()
    s = s.replace(",", "").replace("¸", "").replace(" ", "")
    if not s:
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


def norm_date(s):
    s = to_arabic(str(s)).strip()
    m = re.search(r"(\d{4})[।\./](\d{1,2})[।\./](\d{1,2})", s)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return s


def parse_bigo(s):
    s = to_arabic(str(s)).strip()
    s = s.replace("रु.", "").replace("रु", "")
    s = s.replace("।-", ".").replace("।", ".")
    s = s.replace(",", "")
    s = s.replace(" ", "").replace("-", "")
    s = s.rstrip(".")
    if not s or s == "-":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def extract_bribery_cases(lines, start_idx):
    """Type A: Extract bribery cases from the सि.नं. table in section 2.6.1."""
    cases = []
    i = start_idx
    table_started = False

    while i < len(lines):
        line = lines[i]

        if "सि.नं." in line and "आयोगको निर्णय" in line and "प्रतिवादीको नाम" in line:
            table_started = True
            i += 1
            continue

        if table_started and line.strip().startswith("```"):
            break

        if table_started:
            m = re.match(r"([\d०-९]+)[।\.]\s*\|?\s*(.*)", to_arabic(line))
            if m:
                serial = int(m.group(1))
                rest = m.group(2)
                parts = rest.split("|")
                if len(parts) >= 3:
                    decision_date = norm_date(parts[0])
                    filing_date = norm_date(parts[1])
                    name_pos = parts[2].strip()
                    # With only 3 parts there is no separate bigo column;
                    # parts[-1] would alias name_pos, so leave bigo empty.
                    bigo_text = parts[3].strip() if len(parts) >= 4 else ""

                    j = i + 1
                    extra_name = []
                    while (
                        j < len(lines)
                        and not re.match(r"([\d०-९]+)[।\.]", to_arabic(lines[j]))
                        and not lines[j].strip().startswith("```")
                        and not lines[j].strip().startswith("सि.नं")
                    ):
                        extra_name.append(lines[j].strip().rstrip(","))
                        j += 1

                    full_name_pos = name_pos + " " + " ".join(extra_name)

                    cases.append(
                        {
                            "serial": serial,
                            "decision_date_bs": decision_date,
                            "filing_date_bs": filing_date,
                            "defendants_raw": full_name_pos.strip(),
                            "bigo_amount_npr": parse_bigo(bigo_text),
                        }
                    )
                    i = j
                    continue

        if i > start_idx and "2.6.२" in line:
            break
        i += 1

    return cases


def extract_type_c_defendant_table(lines, table_start_line, case_context):
    """Type C: Extract defendants from a mixed narrative+table section.

    Handles multi-line rows where columns (Serial|Name|Karsur|Magdabi|Bigo)
    span across lines. Each new defendant starts with a Devanagari numeral.
    """
    defendants = []
    i = table_start_line

    # Find the opening ```text
    while i < len(lines):
        if lines[i].strip().startswith("```"):
            i += 1
            break
        i += 1

    # Collect lines grouped by defendant
    defendant_blocks = []
    current_block = None

    while i < len(lines):
        line = lines[i]

        if line.strip().startswith("```"):
            if current_block:
                defendant_blocks.append(current_block)
                current_block = None
            break

        stripped = line.strip()
        if not stripped:
            i += 1
            continue

        arabic_stripped = to_arabic(stripped)

        if "क्र.स." in stripped or stripped in ("रु.", "रु"):
            i += 1
            continue

        m = re.match(r"^([\d०-९]+)[।\.]", arabic_stripped)
        if m:
            if current_block:
                defendant_blocks.append(current_block)
            current_block = {
                "serial": int(m.group(1)),
                "lines": [stripped],
            }
        elif current_block is not None:
            current_block["lines"].append(stripped)

        i += 1

    if current_block:
        defendant_blocks.append(current_block)

    for block in defendant_blocks:
        name_parts = []
        karsur_parts = []
        magdabi_parts = []
        bigo_value = None

        for idx, line in enumerate(block["lines"]):
            clean = line
            if idx == 0:
                clean = re.sub(r"^[\d०-९]+[।\.]\s*", "", line)

            if "|" in line:
                cols = [c.strip() for c in clean.split("|")]

                if idx == 0:
                    # First line: cols = [name, karsur, magdabi, bigo] or [name, bigo].
                    # A 2-col row must be handled exactly once: cols[1] is the bigo,
                    # not a karsur, so the column-count cases are mutually exclusive.
                    if cols:
                        name_parts.append(cols[0])
                    if len(cols) == 2:
                        bigo_value = parse_bigo(cols[1])
                    elif len(cols) >= 3:
                        karsur_parts.append(cols[1])
                        magdabi_parts.extend(cols[2:-1])
                        bigo_value = parse_bigo(cols[-1])
                else:
                    # Continuation line with pipes: align from col 0
                    if cols:
                        name_parts.append(cols[0])
                    if len(cols) >= 3:
                        karsur_parts.append(cols[1])
                        magdabi_parts.append(cols[2])
                    elif len(cols) == 2:
                        karsur_parts.append(cols[1])
            else:
                # No pipe — likely name continuation
                name_parts.append(clean)

        defendant = {
            "serial": block["serial"],
            "name_pad": " ".join(name_parts).strip(),
            "karsur": " ".join(karsur_parts).strip(),
            "magdabi": " ".join(magdabi_parts).strip(),
            "bigo_amount_npr": bigo_value,
            "case_type": case_context.get("case_type", "unknown"),
            "case_type_nepali": case_context.get("case_type_nepali", ""),
            "fy": FISCAL_YEAR,
            "description": case_context.get("description", ""),
            "decision_date_bs": case_context.get("decision_date_bs"),
            "filing_date_bs": case_context.get("filing_date_bs"),
        }
        defendants.append(defendant)

    return defendants


def extract_case_narrative(lines, table_start_line):
    """Extract case metadata from narrative text immediately preceding a table.

    Scans backwards from table_start_line to find decision date,
    filing date, and case description.
    """
    context = {}
    search_end = max(0, table_start_line - 60)
    narrative_lines = []

    for i in range(table_start_line - 1, search_end, -1):
        line = lines[i]
        normalized_line = to_arabic(line)

        # Extract filing date: "मिति 2074।8।12 मा विशेष अदालत...आरोपपत्र दायर"
        # Digit classes accept Devanagari too so dates like २०७५.४.२ match even
        # if an unnormalized line slips through.
        m_file = re.search(
            r"मिति\s+([\d०-९]{4}[।\.][\d०-९]{1,2}[।\.][\d०-९]{1,2})\s+मा\s+विशेष\s+अदालत.*?आरोपपत्र\s+दायर",
            normalized_line,
        )
        if m_file and "filing_date_bs" not in context:
            context["filing_date_bs"] = norm_date(m_file.group(1))

        # Extract decision date: "आयोगको मिति २०७५।२।७ को"
        m_dec = re.search(
            r"आयोगको\s+मिति\s+([\d०-९]{4}[।\.][\d०-९]{1,2}[।\.][\d०-९]{1,2})",
            normalized_line,
        )
        if m_dec and "decision_date_bs" not in context:
            context["decision_date_bs"] = norm_date(m_dec.group(1))

        stripped = line.strip()
        if stripped and not stripped.isdigit():
            narrative_lines.append(stripped)

        # Stop at case number marker (trailing space optional: a standalone
        # marker like "१." loses its whitespace after stripping).
        if re.match(r"^[\d०-९]+[।\.](\s|$)", to_arabic(stripped)):
            break

    context["description"] = "\n".join(reversed(narrative_lines[:5]))
    return context


def extract_summary_table(lines, start_idx):
    """Extract summary statistics table."""
    summary = {}
    i = start_idx

    while i < len(lines):
        line = lines[i]
        if line.strip().startswith("```"):
            i += 1
            continue

        if line.startswith("जम्मा"):
            parts = [p.strip() for p in line.split("|")]
            if len(parts) >= 5:
                summary["total"] = {
                    "cases": arabic_int(parts[2]),
                    "defendants_male": (
                        arabic_int(parts[3].split()[0]) if parts[3] else None
                    ),
                    "defendants_female": (
                        arabic_int(parts[3].split()[1])
                        if len(parts[3].split()) > 1
                        else None
                    ),
                    "defendants_total": (
                        arabic_int(parts[3].split()[2])
                        if len(parts[3].split()) > 2
                        else None
                    ),
                    "bigo_amount_npr": parse_bigo(parts[4]) if len(parts) > 4 else None,
                }
            break

        m = re.match(r"[\d०-९]+[।\.]\s*\|?\s*(.*)", to_arabic(line))
        if m:
            parts = [p.strip() for p in m.group(1).split("|")]
            if len(parts) >= 4:
                case_type_nepali = parts[0]
                case_type = _map_case_type(case_type_nepali)

                j = i + 1
                while j < len(lines) and re.match(
                    r"^(हानि|नोक्सानी|पुर्याएको)", lines[j].strip()
                ):
                    case_type_nepali += " " + lines[j].strip()
                    j += 1
                    i = j - 1

                # Column layout (serial stripped by the regex above):
                #   parts[0]=case type, parts[1]=cases,
                #   parts[2]=defendants "male female total" (single combined cell),
                #   parts[3]=bigo
                defendants_field = (
                    parts[2].split() if len(parts) > 2 and parts[2] else []
                )
                summary[case_type] = {
                    "case_type_nepali": case_type_nepali.strip(),
                    "cases": arabic_int(parts[1]),
                    "defendants_male": (
                        arabic_int(defendants_field[0]) if defendants_field else None
                    ),
                    "defendants_female": (
                        arabic_int(defendants_field[1])
                        if len(defendants_field) > 1
                        else None
                    ),
                    "defendants_total": (
                        arabic_int(defendants_field[2])
                        if len(defendants_field) > 2
                        else None
                    ),
                    "bigo_amount_npr": (
                        parse_bigo(parts[3]) if len(parts) > 3 else None
                    ),
                }

        i += 1

    return summary


def find_all_code_blocks(lines, section_start, section_end):
    """Find all ```text code blocks within a section."""
    blocks = []
    i = section_start
    while i < section_end:
        if lines[i].strip().startswith("```"):
            blocks.append(i)
            i += 1
            while i < section_end and not lines[i].strip().startswith("```"):
                i += 1
            i += 1
        else:
            i += 1
    return blocks


def _map_case_type(nepali_type):
    mapping = {
        "घुस": "bribery",
        "रिसवत": "bribery",
        "झुठा": "fake_certificate",
        "शैक्षिक": "fake_certificate",
        "सार्वजनिक": "public_damage",
        "सम्पत्तिको": "public_damage",
        "गैरकानूनी": "illegal_income",
        "सम्पत्ति": "illegal_income",
        "लाभ": "illegal_benefit",
        "राजस्व": "revenue_leakage",
        "चुहावट": "revenue_leakage",
        "विविध": "other",
    }
    for key, val in mapping.items():
        if key in nepali_type:
            return val
    return "other"


DEFAULT_OUTPUT = "ciaa-28th-2074-75.json"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Extract structured JSON from the 28th CIAA annual report markdown."
    )
    parser.add_argument("input_markdown", help="path to the likhit markdown file")
    parser.add_argument(
        "output_json",
        nargs="?",
        default=DEFAULT_OUTPUT,
        help="output JSON path (default: %(default)s)",
    )
    args = parser.parse_args()

    with open(args.input_markdown) as f:
        text = f.read()

    lines = text.split("\n")

    result = {
        "fy": FISCAL_YEAR,
        "report": REPORT_NAME,
        "source": "likhit-markdown",
        "summary": {},
        "cases": [],
        "extraction_notes": [],
    }

    # Find key sections
    summary_idx = None
    bribery_idx = None
    fake_cert_idx = None
    public_damage_idx = None
    illegal_benefit_idx = None
    illegal_income_idx = None
    revenue_leakage_idx = None
    misc_idx = None
    next_section_idx = None

    for i, line in enumerate(lines):
        arabic = to_arabic(line)
        if "आ.व.०७४/७५ को मुद्दासम्बन्धी निर्णय विवरण" in line:
            summary_idx = i
        elif "2.६.1 घुस" in line or "2.६.1 घुस" in arabic:
            bribery_idx = i
        elif "2.6.२ झुठा" in line:
            fake_cert_idx = i
        elif "2.6.३ सार्वजनिक" in line:
            public_damage_idx = i
        elif "2.6.४ गैरकानूनी लाभ" in line:
            illegal_benefit_idx = i
        elif "2.6.५ गैरकानूनी सम्पत्ति" in line:
            illegal_income_idx = i
        elif "2.6.6 राजस्व चुहावट" in arabic:
            revenue_leakage_idx = i
        elif "2.6.7 विविध" in line:
            misc_idx = i
        elif "2.7 पुनरावेदन" in line:
            next_section_idx = i

    # Extract summary
    if summary_idx:
        result["summary"] = extract_summary_table(lines, summary_idx)

    # Type A: Bribery
    if bribery_idx:
        bribery_cases = extract_bribery_cases(lines, bribery_idx)
        for c in bribery_cases:
            c["case_type"] = "bribery"
            c["case_type_nepali"] = "घुस (रिसवत)"
            c["fy"] = FISCAL_YEAR
        result["cases"].extend(bribery_cases)
        result["extraction_notes"].append(
            f"Extracted {len(bribery_cases)} bribery cases"
        )

    # Type C: Revenue leakage (2.6.6)
    if revenue_leakage_idx:
        rl_end = misc_idx or next_section_idx or len(lines)
        blocks = find_all_code_blocks(lines, revenue_leakage_idx, rl_end)
        rl_defendants = []
        for block_start in blocks:
            case_ctx = extract_case_narrative(lines, block_start)
            case_ctx["case_type"] = "revenue_leakage"
            case_ctx["case_type_nepali"] = "राजस्व चुहावट"
            defendants = extract_type_c_defendant_table(lines, block_start, case_ctx)
            rl_defendants.extend(defendants)
        result["cases"].extend(rl_defendants)
        result["extraction_notes"].append(
            f"Extracted {len(rl_defendants)} defendants from revenue leakage section"
        )

    # Type C: Misc (2.6.7)
    if misc_idx:
        misc_end = next_section_idx if next_section_idx else len(lines)
        blocks = find_all_code_blocks(lines, misc_idx, misc_end)
        misc_defendants = []
        for block_start in blocks:
            case_ctx = extract_case_narrative(lines, block_start)
            case_ctx["case_type"] = "other"
            case_ctx["case_type_nepali"] = "विविध"
            defendants = extract_type_c_defendant_table(lines, block_start, case_ctx)
            misc_defendants.extend(defendants)
        result["cases"].extend(misc_defendants)
        result["extraction_notes"].append(
            f"Extracted {len(misc_defendants)} defendants from misc section"
        )

    # Count remaining sections
    if fake_cert_idx:
        result["extraction_notes"].append(
            f"Fake certificate section found (line {fake_cert_idx})"
        )
    if public_damage_idx:
        result["extraction_notes"].append(
            f"Public damage section found (line {public_damage_idx})"
        )
    if illegal_benefit_idx:
        result["extraction_notes"].append(
            f"Illegal benefit section found (line {illegal_benefit_idx})"
        )
    if illegal_income_idx:
        result["extraction_notes"].append(
            f"Illegal income section found (line {illegal_income_idx})"
        )

    output_path = args.output_json
    with open(output_path, "w") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"Written {len(result['cases'])} defendant records to {output_path}")
    print(f"Summary: {result['summary']}")
    for note in result["extraction_notes"]:
        print(f"  - {note}")
