"""
LLM-assisted extraction of defendant entities from CIAA 28th annual report
narrative sections (2.6.3 Public Damage, 2.6.4 Illegal Benefit, 2.6.5 Illegal Income).

Standalone script (no Django). Usage:

    # Extract all sections to stdout (JSON only)
    python3 scripts/ciaa_extraction/extract_narrative.py

    # Extract a specific section
    python3 scripts/ciaa_extraction/extract_narrative.py --section 2.6.3

    # Output to file
    python3 scripts/ciaa_extraction/extract_narrative.py --output extracted.json

    # Dry-run / preview chunks (no LLM call)
    python3 scripts/ciaa_extraction/extract_narrative.py --dry-run

    # Limit chunks
    python3 scripts/ciaa_extraction/extract_narrative.py --limit 3

    # Custom model
    python3 scripts/ciaa_extraction/extract_narrative.py --llm-model claude-sonnet-4-5

    # Explicit report path (takes precedence over ANNUAL_REPORT_PATH env var)
    python3 scripts/ciaa_extraction/extract_narrative.py --report-path /path/to/report.md
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
from pathlib import Path

import requests

SECTION_LABELS = {
    "2.6.3": "सार्वजनिक सम्पत्तिको हानि नोक्सानी",
    "2.6.4": "गैरकानूनी लाभ",
    "2.6.5": "गैरकानूनी सम्पत्ति आर्जन",
}

# Serial number patterns at line start. The markdown mixes Devanagari (१. / १।)
# and Arabic (1.) serials depending on OCR quality.
# Devanagari digits followed by danda OR full-stop.
_DV_SERIAL = re.compile(r"^[\s\xa0]*([१२३४५६७८९०]+)[।.]\s", re.MULTILINE)
# Arabic 1-2 digit serials (avoid matching dates like 2074.4.2 / amounts like 2,60,000).
_AR_SERIAL_SAFE = re.compile(r"^[ \t]*(\d{1,2})[\.।]\s+", re.MULTILINE)

# Map digits between Devanagari and Arabic.
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


def _log(msg: str) -> None:
    print(msg, file=sys.stderr)


class PermanentLLMError(Exception):
    """Non-retryable failure (auth, client 4xx, malformed-but-returned body)."""


class TransientLLMError(Exception):
    """Retryable failure (network, timeout, 5xx, 429)."""


def _dv_to_int(text: str) -> int:
    return int(text.translate(_DV_TO_AR))


def _find_serials(text: str) -> list[tuple[int, int, int]]:
    """Find serial markers, returning (serial_number, start_pos, end_pos) sorted by pos.

    Handles both Devanagari (१. / १।) and Arabic (1.) serials.
    """
    markers: list[tuple[int, int, int]] = []  # (pos, serial, end_pos)

    for m in _DV_SERIAL.finditer(text):
        markers.append((m.start(), _dv_to_int(m.group(1)), m.end()))

    for m in _AR_SERIAL_SAFE.finditer(text):
        markers.append((m.start(), int(m.group(1)), m.end()))

    markers.sort(key=lambda x: x[0])

    # Only valid case serials (1-99), not year numbers like 2068.
    return [(serial, start, end) for start, serial, end in markers if 1 <= serial <= 99]


def _split_cases(text: str) -> list[tuple[int, str]]:
    """Split a section into per-case paragraphs by serial numbers."""
    serials = _find_serials(text)
    if not serials:
        return []

    cases: list[tuple[int, str]] = []
    for i, (serial, _start, end) in enumerate(serials):
        body_start = end
        body_end = serials[i + 1][1] if i + 1 < len(serials) else len(text)
        body = text[body_start:body_end].strip()
        if body:
            cases.append((serial, body))
    return cases


def _is_page_artifact(line: str) -> bool:
    stripped = line.strip()
    if stripped.isdigit() and len(stripped) <= 3:
        return True
    if "परिच्छेद-२" in stripped:
        return True
    if not stripped:
        return True
    return False


def _clean_section_text(text: str) -> str:
    lines = [line for line in text.split("\n") if not _is_page_artifact(line)]
    cleaned = "\n".join(lines)
    # Remove code fences and their content (tabular data embedded in narrative).
    cleaned = re.sub(r"```text.*?```", "", cleaned, flags=re.DOTALL)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _section_to_dv(section: str) -> str:
    """Convert Arabic section number to Devanagari variant, e.g. "2.6.3" -> "2.6.३"."""
    parts = section.rsplit(".", 1)
    if len(parts) == 2 and parts[1].isdigit():
        dv_digit = str(int(parts[1])).translate(_AR_TO_DV)
        return f"{parts[0]}.{dv_digit}"
    return section


def _read_section(file_path: Path, section: str) -> str | None:
    """Read a section from header line up to the next section header.

    Handles Devanagari digits in section numbers (e.g. 2.6.३ for section 2.6.3),
    both when entering a section and when locating the next header to stop at.
    """
    content = file_path.read_text(encoding="utf-8")

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

    # Next 2.6.X header where X is one or more Arabic OR Devanagari digits,
    # so a Devanagari-numbered next header (e.g. 2.6.४) ends the current section.
    next_header = re.compile(r"^2\.6\.[\d१२३४५६७८९०]+[\s:-]", re.MULTILINE)
    next_match = next_header.search(content, start + 1)
    end = next_match.start() if next_match else len(content)

    return _clean_section_text(content[start:end])


def _retry_on_transient(fn, *, max_retries: int):
    """Call fn() retrying only TransientLLMError with exponential backoff + jitter.

    PermanentLLMError (and any other exception) propagates immediately.
    """
    for attempt in range(1, max_retries + 2):  # 1 initial + N retries
        try:
            return fn()
        except TransientLLMError as exc:
            if attempt > max_retries:
                raise
            wait = min(2 ** (attempt - 1), 120)
            wait *= 1.0 + random.uniform(-0.2, 0.2)  # noqa: S311 — not crypto
            wait = max(0.0, wait)
            _log(
                f"    Attempt {attempt}/{max_retries} failed: {exc}. "
                f"Retrying in {wait:.1f}s..."
            )
            time.sleep(wait)
    raise AssertionError("unreachable")


def _try_parse_response(raw: str) -> dict | None:
    """Parse an LLM HTTP body, handling SSE/streaming interleaved with JSON."""
    # Strategy 1: plain JSON.
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict) and "choices" in obj:
            return obj
    except json.JSONDecodeError:
        pass

    # Strategy 2: SSE lines with data: prefix.
    if "data:" in raw:
        assembled: list[str] = []
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
                piece = choices[0].get("delta", {}).get("content", "")
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

    # Strategy 3: multiple JSON objects separated by newlines.
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("data:"):
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, dict) and "choices" in obj:
                return obj
        except json.JSONDecodeError:
            continue

    return None


def _parse_llm_json(text: str) -> dict:
    """Extract the JSON object emitted by the model from its message content."""
    text = text.strip()
    if text.startswith("```"):
        start = text.find("\n")
        end = text.rfind("```")
        if start != -1 and end != -1:
            text = text[start + 1 : end].strip()

    brace_start = text.find("{")
    if brace_start == -1:
        raise PermanentLLMError(f"No JSON object found in LLM response: {text[:200]}")
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
        raise PermanentLLMError("Unmatched brace in LLM response")
    try:
        return json.loads(text[brace_start : brace_end + 1])
    except json.JSONDecodeError as exc:
        raise PermanentLLMError(f"Malformed JSON in LLM response: {exc}") from exc


def _call_llm_for_case(
    *,
    session: requests.Session,
    section_label: str,
    serial: int,
    case_text: str,
    model: str,
    base_url: str,
    api_key: str,
    timeout: int,
    use_anthropic: bool,
) -> dict:
    """Call the LLM once to extract defendant data from a single case paragraph."""
    user_prompt = f"""Extract defendants from CIAA annual report case {serial} in section {section_label}.

CASE TEXT (Nepali):
{case_text}"""

    if use_anthropic:
        import anthropic

        client = anthropic.Anthropic(api_key=api_key)
        try:
            response = client.messages.create(
                model=model if "claude" in model else "claude-3-5-sonnet-20241022",
                max_tokens=2000,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_prompt}],
                temperature=0.1,
            )
        except anthropic.APIStatusError as exc:
            status = getattr(exc, "status_code", None)
            if status == 429 or (status is not None and status >= 500):
                raise TransientLLMError(f"Anthropic {status}: {exc}") from exc
            raise PermanentLLMError(f"Anthropic {status}: {exc}") from exc
        except (anthropic.APIConnectionError, anthropic.APITimeoutError) as exc:
            raise TransientLLMError(str(exc)) from exc
        return _parse_llm_json(response.content[0].text)

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

    try:
        resp = session.post(url, headers=headers, json=payload, timeout=timeout)
    except (requests.Timeout, requests.ConnectionError) as exc:
        raise TransientLLMError(str(exc)) from exc

    status = resp.status_code
    if status == 429 or status >= 500:
        raise TransientLLMError(f"HTTP {status} from LLM proxy")
    if status >= 400:
        raise PermanentLLMError(f"HTTP {status} from LLM proxy: {resp.text[:300]}")

    raw = resp.text.strip()
    data = _try_parse_response(raw)
    if data is None:
        raise PermanentLLMError(f"LLM returned unparseable response: {raw[:500]}")

    choices = data.get("choices", [])
    if not choices:
        raise PermanentLLMError(f"LLM returned no choices: {data}")

    content = choices[0].get("message", {}).get("content", "")
    if not content:
        raise PermanentLLMError("LLM returned empty content")

    return _parse_llm_json(content)


def _resolve_report_path(args: argparse.Namespace) -> Path:
    """Resolve report path from --report-path then ANNUAL_REPORT_PATH env var."""
    candidate = args.report_path or os.environ.get("ANNUAL_REPORT_PATH")
    if not candidate:
        raise SystemExit(
            "No annual report path. Pass --report-path or set the "
            "ANNUAL_REPORT_PATH env var."
        )
    path = Path(candidate)
    if not path.exists():
        raise SystemExit(f"Annual report not found at {path}.")
    return path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Extract defendant entities from CIAA 28th annual report narrative "
            "sections via LLM."
        )
    )
    parser.add_argument(
        "--section",
        default=None,
        help="Section to extract: 2.6.3, 2.6.4, 2.6.5 (default: all)",
    )
    parser.add_argument(
        "--output",
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
        default=os.environ.get("JAWAFDEHI_ALLEGATION_MODEL", "ocg/qwen3.6-plus"),
        help="LLM model name",
    )
    parser.add_argument(
        "--report-path",
        default=None,
        help="Path to the annual report markdown (overrides ANNUAL_REPORT_PATH)",
    )
    parser.add_argument(
        "--llm-base-url",
        default=os.environ.get(
            "JAWAFDEHI_LLM_PROXY_URL",
            "https://llm-proxy.jawafdehi.org/v1",
        ),
    )
    parser.add_argument(
        "--llm-api-key",
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
        help="Number of retries on transient LLM failure",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    sections_to_extract = (
        [args.section] if args.section else ["2.6.3", "2.6.4", "2.6.5"]
    )

    is_dry_run = args.dry_run
    api_key = (
        args.llm_api_key
        or os.environ.get("JAWAFDEHI_LLM_API_KEY")
        or os.environ.get("ANTHROPIC_API_KEY")
        or os.environ.get("OPENCODE_API_KEY")
    )
    if not is_dry_run and not api_key:
        raise SystemExit(
            "No API key found. Set --llm-api-key, JAWAFDEHI_LLM_API_KEY, "
            "ANTHROPIC_API_KEY, or OPENCODE_API_KEY."
        )

    report_path = _resolve_report_path(args)

    # Single reused session for HTTP connection pooling across all calls.
    session = requests.Session()

    all_results: dict[str, list[dict]] = {}
    total_defendants = 0
    total_cases_processed = 0
    total_errors = 0

    for section in sections_to_extract:
        section_label = SECTION_LABELS.get(section, section)
        _log(f"\n{'=' * 60}")
        _log(f"Section {section}: {section_label}")

        raw = _read_section(report_path, section)
        if raw is None:
            _log(f"  Section {section} not found")
            continue

        cases = _split_cases(raw)
        _log(f"  Found {len(cases)} case paragraphs")

        if args.limit:
            cases = cases[: args.limit]

        section_results: list[dict] = []
        for serial, case_text in cases:
            _log(f"  Processing case {serial}...")
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

            try:
                result = _retry_on_transient(
                    lambda st=section_label, sr=serial, ct=case_text: _call_llm_for_case(
                        session=session,
                        section_label=st,
                        serial=sr,
                        case_text=ct,
                        model=args.llm_model,
                        base_url=args.llm_base_url,
                        api_key=api_key,
                        timeout=args.llm_timeout,
                        use_anthropic=args.use_anthropic,
                    ),
                    max_retries=args.retries,
                )
            except Exception as exc:  # noqa: BLE001 — log and continue per case
                _log(f"    Failed: {exc}")
                total_errors += 1
                continue

            section_results.append(result)
            n_def = len(result.get("defendants", []))
            total_defendants += n_def
            _log(f"    → {n_def} defendant(s) extracted")

        all_results[section] = section_results

    _log(f"\n{'=' * 60}")
    _log("SUMMARY")
    if is_dry_run:
        _log(f"  Cases previewed: {total_cases_processed}")
    else:
        _log(f"  Cases processed: {total_cases_processed}")
        _log(f"  Defendants extracted: {total_defendants}")
        _log(f"  Errors: {total_errors}")

    output = {
        "source": str(report_path),
        "sections_extracted": sections_to_extract,
        "total_cases": total_cases_processed,
        "total_defendants": total_defendants,
        "section_data": all_results,
    }

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(output, f, ensure_ascii=False, indent=2)
        _log(f"\nOutput written to {args.output}")
    else:
        json.dump(output, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
