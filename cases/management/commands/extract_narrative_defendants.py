"""
LLM-assisted extraction of defendant entities from CIAA 28th annual report
narrative sections (2.6.3 Public Damage, 2.6.4 Illegal Benefit, 2.6.5 Illegal Income).

Usage:

    # Extract all sections to stdout
    python manage.py extract_narrative_defendants

    # Extract a specific section
    python manage.py extract_narrative_defendants --section 2.6.3

    # Output to file
    python manage.py extract_narrative_defendants --output extracted.json

    # Dry-run / preview chunks (no LLM call)
    python manage.py extract_narrative_defendants --dry-run

    # Limit chunks
    python manage.py extract_narrative_defendants --limit 3

    # Custom model
    python manage.py extract_narrative_defendants --llm-model claude-sonnet-4-5
"""

import json
import logging
import os
import re
import sys
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

logger = logging.getLogger(__name__)

# --- config ---

ANNUAL_REPORT_PATH = Path(
    os.environ.get(
        "ANNUAL_REPORT_PATH",
        "/home/ubuntu/jawafdehi-meta/paperspace/tmp/corruption-case-db/"
        "annual-reports/likhit-markdown/28th-annual-report-2074-75.md",
    )
)

SECTION_LABELS = {
    "2.6.3": "सार्वजनिक सम्पत्तिको हानि नोक्सानी",
    "2.6.4": "गैरकानूनी लाभ",
    "2.6.5": "गैरकानूनी सम्पत्ति आर्जन",
}

# Devanagari serial number regex — matches १. २. ... ९. १०. etc.
# Devanagari and Arabic serial number patterns at line start
# The markdown uses mixed: १. २. ... and 2. 3. 4. ... depending on OCR quality
_DV_SERIAL = re.compile(r"^([\s\xa0]*)([१२३४५६७८९०]+)।", re.MULTILINE)
_AR_SERIAL = re.compile(r"^[ \t]*(\d+)\.\s", re.MULTILINE)
# But we must avoid matching dates (2074.4.2) and amounts (2,60,000) — only
# match single or two-digit serials at line start
_AR_SERIAL_SAFE = re.compile(r"^[ \t]*(\d{1,2})[\.।]\s+", re.MULTILINE)

# Map digits between Devanagari and Arabic
_DV_TO_AR = str.maketrans("१२३४५६७८९०", "1234567890")
_AR_TO_DV = str.maketrans("1234567890", "१२३४५६७८९०")

SYSTEM_PROMPT = """You are a Nepali legal document analyst. Extract defendant information from CIAA case descriptions written in Nepali prose.

For each case paragraph, extract ALL defendants mentioned. Each defendant is a person who is charged/accused in the case.

Output a JSON object with this structure:
{
  "case": {
    "serial_number": 1,
    "section": "2.6.3",
    "section_label": "सार्वजनिक सम्पत्तिको हानि नोक्सानी"
  },
  "defendants": [
    {
      "name": "Full name of defendant in Devanagari",
      "position": "Position/title at time of offense (e.g., तत्कालीन गा.वि.स. सचिव)",
      "office": "Office/institution where they worked",
      "charge_sections": ["भ्रष्टाचार निवारण ऐन, २०५९ को दफा १७", "दफा ३(१)"],
      "bigo": "Disputed amount in Nepali format (e.g., रु.2,45,000।-)",
      "bigo_numeric": "Bigo as pure number (e.g., 245000)",
      "decision_date_bs": "BS date of CIAA decision (e.g., 2074.4.2)",
      "filing_date_bs": "BS date of case filing in Special Court (e.g., 2074.4.4)",
      "is_primary": true
    }
  ]
}

EXTRACTION RULES:
1. Names: Extract the FULL name exactly as written in the document. Include middle initials.
2. Position: Extract the position title (e.g., तत्कालीन गा.वि.स. सचिव, प्रधानाध्यापक, लेखापाल). The prefix तत्कालीन means "then" — include it.
3. Office: Extract the office/location context (e.g., जिल्ला हुलाक कार्यालय, रौतहट).
4. Charge sections: Extract all दफा (section) references from the Corruption Prevention Act (भ्रष्टाचार निवारण ऐन).
5. Bigo: Extract the रु. amount. Normalize by removing commas but keep the।- suffix.
6. Bigo numeric: Convert the Nepali amount to a plain integer (remove commas, रु., ।-).
7. Decisions/filing dates: Extract BS dates (usually in 2074.XX.XX format).
8. is_primary: Mark the primary defendant(s) — usually the first-named or the person with highest authority.
9. Single-defendant cases: mark is_primary = true.
10. Multiple defendants: mark at least one is_primary = true.

CRITICAL: If a case has multiple defendants (e.g., a committee chair, secretary, and treasurer), extract ALL of them with their respective roles and bigo amounts. Do NOT merge defendants.

IMPORTANT: Return ONLY the JSON object. No additional text, no markdown fences, no commentary."""


def _dv_to_int(text: str) -> int:
    """Convert Devanagari digits to integer."""
    return int(text.translate(_DV_TO_AR))


def _find_serials(text: str) -> list[tuple[int, int, int]]:
    """Find all serial number markers in text.

    Returns list of (serial_number, start_pos, end_pos) sorted by position.
    Handles both Devanagari (१. २. ...) and Arabic (1. 2. ...) digits.
    """
    markers: list[tuple[int, int, int, int]] = []  # (pos, digit_type, serial, end_pos)

    # Devanagari serials
    for m in _DV_SERIAL.finditer(text):
        serial = _dv_to_int(m.group(2))
        markers.append((m.start(), 1, serial, m.end()))

    # Arabic serials at line start (1-2 digit to avoid dates/amounts)
    for m in _AR_SERIAL_SAFE.finditer(text):
        serial = int(m.group(1))
        markers.append((m.start(), 2, serial, m.end()))

    # Sort by position
    markers.sort(key=lambda x: x[0])

    # Filter: only valid case serials (1-99), not year numbers like 2068
    result = [(s, start, end) for start, _dt, s, end, in markers if 1 <= s <= 99]
    return result


def _split_cases(text: str) -> list[tuple[int, str]]:
    """Split a section into per-case paragraphs by serial numbers.

    Returns list of (serial_number, paragraph_text).
    Handles mixed Devanagari and Arabic serial digits.
    """
    serials = _find_serials(text)
    if not serials:
        return []

    cases: list[tuple[int, str]] = []
    for i, (serial, start, end) in enumerate(serials):
        # Body starts after the marker ends, ends before next marker (or end of text)
        body_start = end
        body_end = serials[i + 1][1] if i + 1 < len(serials) else len(text)
        body = text[body_start:body_end].strip()
        if body:
            cases.append((serial, body))
    return cases


def _is_page_artifact(line: str) -> bool:
    """Detect page-number / chapter-repeat artifacts."""
    stripped = line.strip()
    if stripped.isdigit() and len(stripped) <= 3:
        return True
    if "परिच्छेद-२" in stripped:
        return True
    # Empty lines
    if not stripped:
        return True
    return False


def _clean_section_text(text: str) -> str:
    """Remove page artifacts and normalize whitespace from a section block."""
    lines = []
    for line in text.split("\n"):
        if _is_page_artifact(line):
            continue
        lines.append(line)
    # Remove code fences and their content (tabular data embedded in narrative)
    cleaned = "\n".join(lines)
    cleaned = re.sub(r"```text.*?```", "", cleaned, flags=re.DOTALL)
    # Collapse multiple blank lines
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _section_to_dv(section: str) -> str:
    """Convert Arabic section number to Devanagari variant for matching.

    E.g., "2.6.3" -> "2.6.३"
    """
    parts = section.rsplit(".", 1)
    if len(parts) == 2 and parts[1].isdigit():
        dv_digit = str(int(parts[1])).translate(_AR_TO_DV)
        return f"{parts[0]}.{dv_digit}"
    return section


def _read_section(file_path: Path, section: str) -> str | None:
    """Read a section from the annual report markdown.

    Extracts from section header line up to the next section header.
    Handles Devanagari digits in section numbers (e.g., 2.6.३ for section 2.6.3).
    """
    content = file_path.read_text(encoding="utf-8")

    # Try exact match first, then Devanagari variant
    header_pattern = re.compile(rf"^{re.escape(section)}\s", re.MULTILINE)
    match = header_pattern.search(content)

    if not match:
        dv_section = _section_to_dv(section)
        if dv_section != section:
            header_pattern = re.compile(rf"^{re.escape(dv_section)}\s", re.MULTILINE)
            match = header_pattern.search(content)

    if not match:
        return None

    start = match.start()

    # Find next section header (any 2.6.X with Arabic or Devanagari digit)
    next_header = re.compile(r"^2\.6\.[\d\dवबगदन]+[\s:-]", re.MULTILINE)
    next_match = next_header.search(content, start + 1)
    end = next_match.start() if next_match else len(content)

    raw = content[start:end]
    return _clean_section_text(raw)


def _call_llm_for_case(
    section_label: str,
    serial: int,
    case_text: str,
    model: str,
    base_url: str,
    api_key: str,
    timeout: int,
    use_anthropic: bool = False,
) -> dict:
    """Call LLM to extract defendant data from one case paragraph."""
    user_prompt = f"""Extract defendants from CIAA annual report case {serial} in section {section_label}.

CASE TEXT (Nepali):
{case_text}"""

    if use_anthropic:
        import anthropic

        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=model if "claude" in model else "claude-3-5-sonnet-20241022",
            max_tokens=2000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
            temperature=0.1,
        )
        content = response.content[0].text
        return _parse_llm_json(content)

    import requests

    url = f"{base_url.rstrip('/')}/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
    }
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.1,
        "max_tokens": 2000,
    }

    resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
    resp.raise_for_status()

    raw = resp.text.strip()
    data = _try_parse_response(raw)
    if data is None:
        raise Exception(f"LLM returned unparseable response: {raw[:500]}")

    choices = data.get("choices", [])
    if not choices:
        raise Exception(f"LLM returned no choices: {data}")

    content = choices[0].get("message", {}).get("content", "")
    if not content:
        raise Exception("LLM returned empty content")

    return _parse_llm_json(content)


def _try_parse_response(raw: str) -> dict | None:
    """Try to parse LLM response, handling SSE/streaming interleaved with JSON."""
    # Strategy 1: plain JSON
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict) and "choices" in obj:
            return obj
    except json.JSONDecodeError:
        pass

    # Strategy 2: SSE lines with data: prefix
    if "data:" in raw:
        assembled = []
        final_meta = None
        for line in raw.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            payload = line[len("data:") :].strip()
            if payload == "[DONE]":
                break
            try:
                chunk = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if not isinstance(chunk, dict):
                continue
            final_meta = chunk
            choices = chunk.get("choices", [])
            if choices and isinstance(choices[0], dict):
                delta = choices[0].get("delta", {})
                piece = delta.get("content", "")
                if piece:
                    assembled.append(piece)
        if assembled and final_meta is not None:
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "".join(assembled),
                        },
                        "finish_reason": "stop",
                        "index": 0,
                    }
                ]
            }

    # Strategy 3: multiple JSON objects separated by newlines
    lines = [
        line.strip()
        for line in raw.splitlines()
        if line.strip() and not line.startswith("data:")
    ]
    for line in lines:
        try:
            obj = json.loads(line)
            if isinstance(obj, dict) and "choices" in obj:
                return obj
        except json.JSONDecodeError:
            continue

    return None


def _parse_llm_json(text: str) -> dict:
    """Extract JSON object from LLM response."""
    text = text.strip()
    # Strip markdown fences
    if text.startswith("```"):
        start = text.find("\n")
        end = text.rfind("```")
        if start != -1 and end != -1:
            text = text[start + 1 : end].strip()
    # Find outer JSON object
    brace_start = text.find("{")
    if brace_start == -1:
        raise CommandError(f"No JSON object found in LLM response: {text[:200]}")
    depth = 0
    brace_end = -1
    for i, ch in enumerate(text[brace_start:], brace_start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                brace_end = i
                break
    if brace_end == -1:
        raise CommandError("Unmatched brace in LLM response")
    return json.loads(text[brace_start : brace_end + 1])


class Command(BaseCommand):
    help = "Extract defendant entities from CIAA 28th annual report narrative sections via LLM"

    def add_arguments(self, parser):
        parser.add_argument(
            "--section",
            type=str,
            default=None,
            help="Section to extract: 2.6.3, 2.6.4, 2.6.5 (default: all)",
        )
        parser.add_argument(
            "--output",
            type=str,
            default=None,
            help="Output JSON file path (default: stdout)",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print case chunks without calling LLM",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Process only N case paragraphs",
        )
        parser.add_argument(
            "--llm-model",
            type=str,
            default=os.environ.get("JAWAFDEHI_ALLEGATION_MODEL", "ocg/qwen3.6-plus"),
            help="LLM model name",
        )
        parser.add_argument(
            "--llm-base-url",
            type=str,
            default=os.environ.get(
                "JAWAFDEHI_LLM_PROXY_URL",
                "https://llm-proxy.jawafdehi.org/v1",
            ),
        )
        parser.add_argument(
            "--llm-api-key",
            type=str,
            default=None,
            help="Override API key from env",
        )
        parser.add_argument(
            "--llm-timeout",
            type=int,
            default=300,
            help="LLM API timeout in seconds",
        )
        parser.add_argument(
            "--use-anthropic",
            action="store_true",
            help="Use direct Anthropic API",
        )
        parser.add_argument(
            "--retries",
            type=int,
            default=3,
            help="Number of retries on LLM failure",
        )

    def handle(self, *args, **options):
        sections_to_extract = (
            [options["section"]] if options["section"] else ["2.6.3", "2.6.4", "2.6.5"]
        )
        api_key = (
            options["llm_api_key"]
            or os.environ.get("JAWAFDEHI_LLM_API_KEY")
            or os.environ.get("ANTHROPIC_API_KEY")
            or os.environ.get("OPENCODE_API_KEY")
        )
        if not api_key:
            raise CommandError(
                "No API key found. Set --llm-api-key, JAWAFDEHI_LLM_API_KEY, "
                "ANTHROPIC_API_KEY, or OPENCODE_API_KEY."
            )

        output_path = options.get("output")
        is_dry_run = options["dry_run"]
        limit = options["limit"]

        if not ANNUAL_REPORT_PATH.exists():
            raise CommandError(
                f"Annual report not found at {ANNUAL_REPORT_PATH}. "
                "Set ANNUAL_REPORT_PATH env var to override."
            )

        all_results: dict[str, list[dict]] = {}
        total_defendants = 0
        total_cases_processed = 0
        total_errors = 0

        for section in sections_to_extract:
            section_label = SECTION_LABELS.get(section, section)
            self.stdout.write(f"\n{'='*60}")
            self.stdout.write(f"Section {section}: {section_label}")

            raw = _read_section(ANNUAL_REPORT_PATH, section)
            if raw is None:
                self.stdout.write(self.style.WARNING(f"  Section {section} not found"))
                continue

            cases = _split_cases(raw)
            self.stdout.write(f"  Found {len(cases)} case paragraphs")

            if limit:
                cases = cases[:limit]

            section_results = []
            for serial, case_text in cases:
                self.stdout.write(f"  Processing case {serial}...")
                total_cases_processed += 1

                if is_dry_run:
                    section_results.append(
                        {
                            "serial": serial,
                            "narrative_chars": len(case_text),
                            "narrative_preview": case_text[:150],
                        }
                    )
                    continue

                result = self._extract_with_retry(
                    case_text=case_text,
                    section=section,
                    section_label=section_label,
                    serial=serial,
                    api_key=api_key,
                    options=options,
                )
                if result:
                    section_results.append(result)
                    n_def = len(result.get("defendants", []))
                    total_defendants += n_def
                    self.stdout.write(f"    → {n_def} defendant(s) extracted")
                else:
                    total_errors += 1

            all_results[section] = section_results

        # Summary
        self.stdout.write(f"\n{'='*60}")
        self.stdout.write("SUMMARY")
        if is_dry_run:
            self.stdout.write(f"  Cases previewed: {total_cases_processed}")
        else:
            self.stdout.write(f"  Cases processed: {total_cases_processed}")
            self.stdout.write(f"  Defendants extracted: {total_defendants}")
            self.stdout.write(f"  Errors: {total_errors}")

        # Output
        output = {
            "source": str(ANNUAL_REPORT_PATH),
            "sections_extracted": sections_to_extract,
            "total_cases": total_cases_processed,
            "total_defendants": total_defendants,
            "section_data": all_results,
        }

        if output_path:
            with open(output_path, "w", encoding="utf-8") as f:
                json.dump(output, f, ensure_ascii=False, indent=2)
            self.stdout.write(self.style.SUCCESS(f"\nOutput written to {output_path}"))
        else:
            json.dump(output, sys.stdout, ensure_ascii=False, indent=2)
            sys.stdout.write("\n")

    def _extract_with_retry(
        self,
        case_text: str,
        section: str,
        section_label: str,
        serial: int,
        api_key: str,
        options: dict,
    ) -> dict | None:
        """Extract with retry logic."""
        import time

        max_retries = options["retries"]
        model = options["llm_model"]
        base_url = options["llm_base_url"]
        timeout = options["llm_timeout"]

        for attempt in range(1, max_retries + 1):
            try:
                return _call_llm_for_case(
                    section_label=section_label,
                    serial=serial,
                    case_text=case_text,
                    model=model,
                    base_url=base_url,
                    api_key=api_key,
                    timeout=timeout,
                    use_anthropic=options.get("use_anthropic", False),
                )
            except Exception as e:
                if attempt < max_retries:
                    wait = 2**attempt
                    self.stdout.write(
                        self.style.WARNING(
                            f"    Attempt {attempt}/{max_retries} failed: {e}. "
                            f"Retrying in {wait}s..."
                        )
                    )
                    time.sleep(wait)
                else:
                    self.stdout.write(
                        self.style.ERROR(
                            f"    Failed after {max_retries} attempts: {e}"
                        )
                    )
                    return None
