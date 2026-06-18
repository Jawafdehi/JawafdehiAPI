#!/usr/bin/env python
"""Enrich CIAA Special Court case timelines (DB-free script using the llm package).

Standalone script to extract a case's FACTUAL TIMELINE from CIAA source
documents using LLM extraction, fully over the Jawafdehi HTTP API. Never
touches the database.

Phase A.3 of the CIAA Case Enrichment pipeline. Populates ``Case.timeline``
with the factual milestones of a corruption case — NOT routine court-procedure
logs.

Usage:
    python casework/enrich_timeline.py --dry-run
    python casework/enrich_timeline.py --slug case-0123
    python casework/enrich_timeline.py --limit 10 --verbose
    python casework/enrich_timeline.py --fiscal-year 080 --dry-run
    python casework/enrich_timeline.py --force
"""

import argparse
import logging
import os
import sys
import urllib.parse
from typing import Optional

import requests

# Ensure the api dir is in sys.path so imports work when run as a file
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from casework.common import (
    CaseworkApi,
    add_common_args,
    bootstrap,
    convert_date_tool,
    get_target_cases,
    is_valid_iso_date,
    parse_extraction_response,
    print_summary,
    setup_logging,
)

logger = logging.getLogger(__name__)

# Source types (matched as plain strings from the API payload).
# Ordered by usefulness for factual milestones.
MILESTONE_SOURCE_TYPES = (
    "AG_ABHIYOG_PATRA",  # charge sheet — richest factual detail
    "CIAA_PRESS_RELEASE",  # complaint / investigation / chargesheet dates
    "COURT_ORDER",  # verdict
    "COURT_FILING_OTHER",
)

EXTRACTION_SYSTEM_PROMPT = """\
You are a Nepali legal analyst reconstructing the FACTUAL TIMELINE of a \
corruption case investigated by Nepal's CIAA (अख्तियार दुरुपयोग अनुसन्धान आयोग) \
and tried at the Special Court (विशेष अदालत).

Your goal is to capture the factual milestones of the case — what happened and \
when — NOT the routine court-procedure log. The court's hearing-by-hearing \
progression (पेशी/sunwai) is already tracked separately as the case's Pragati \
Bibaran by case number, so DO NOT emit one entry per hearing. Use the hearing \
records only to anchor the dates of the milestones below.

MILESTONES TO EXTRACT (include each only when grounded in the sources):
1. Factual incident period — the span the alleged offence covers, BEFORE the
   complaint (the CIAA "jaanch awadhi" / जाँच अवधि, or the period the accused
   held office or the conduct occurred). Emit as a SINGLE entry with both
   "date" (start) and "end_date" (end).
2. Complaint (उजुरी निवेदन) — when the complaint was registered at the CIAA.
3. CIAA investigation (अनुसन्धान) — when the CIAA began/decided to investigate,
   if distinct from the complaint.
4. Press release (प्रेस विज्ञप्ति) — when the CIAA publicly announced the case.
5. Chargesheet filed / case registered (अभियोगपत्र/आरोपपत्र दायर, मुद्दा दर्ता)
   — when the CIAA filed the chargesheet at the Special Court.
6. Interim court order (अन्तरिम आदेश) — any interim order dates, if issued.
7. Special Court verdict (विशेष अदालतको फैसला) — judgment date and outcome
   (conviction / acquittal "सफाई" / partial).
8. Supreme Court appeal (सर्वोच्च अदालतमा पुनरावेदन) — when an appeal was filed.
9. Supreme Court verdict (सर्वोच्च अदालतको फैसला) — final judgment.

ENTRY FORMAT — each entry is a JSON object:
- "date": AD date "YYYY-MM-DD" (Gregorian). REQUIRED.
- "date_bs": the Bikram Sambat date "YYYY-MM-DD" as it appears in the source.
  REQUIRED — every Nepali legal document states dates in BS; record it.
- "end_date": AD "YYYY-MM-DD" — ONLY for the incident-period entry (milestone 1).
- "end_date_bs": the BS date for end_date — only when end_date is present.
- "title": short Nepali label (देवनागरी, 4-12 words) naming the milestone.
- "description": 1-3 Nepali sentences with specifics (amounts, section numbers,
  press-release number, bench, outcome). Optional but strongly encouraged.

DATE CONVERSION TOOL (MANDATORY):
You have a `convert_date` tool that converts between AD (Gregorian) and BS
(Bikram Sambat) using Nepal's official calendar. LLMs routinely get BS<->AD
conversion wrong by days or months — so you MUST NOT convert dates in your head.
- For every date taken from a source document (stated in BS), call convert_date
  with mode="bs_to_ad" to get the AD "date"; keep the original BS as "date_bs".
- For every date taken from the NGM hearing records (in AD), call convert_date
  with mode="ad_to_bs" to get "date_bs"; keep the AD as "date".
- Batch dates into one tool call where possible (the tool accepts a list).
- Use ONLY the tool's output for "date"/"date_bs"; never adjust or round it.
- Verify every entry's "date" and "date_bs" are a matching pair the tool
  returned before emitting it.

QUALITY RULES:
- Order entries chronologically, earliest first.
- Every entry must be grounded in the provided sources. Do NOT fabricate dates,
  events, amounts, or outcomes.
- Omit a milestone entirely if the sources do not support it. Fewer, accurate
  entries are better than padded ones.
- Do NOT emit routine hearing/पेशी entries — synthesize them into milestone 7.
"""

EXTRACTION_USER_PROMPT = """\
Reconstruct the factual timeline for the following CIAA Special Court case.

Case title: {case_title}

Instructions:
- Extract the factual milestones defined in the system prompt that the sources
  support.
- Every entry needs "date" (AD YYYY-MM-DD), "date_bs" (BS YYYY-MM-DD), and a
  Nepali "title".
- Express the factual incident period (milestone 1) as ONE entry with "date" +
  "end_date" (and "date_bs" + "end_date_bs").
- Convert every date with the convert_date tool — do not convert dates yourself.
  Source dates are BS (use bs_to_ad); NGM dates are AD (use ad_to_bs).
- Use the NGM hearing data only to anchor milestone dates (chargesheet
  registration, verdict). Do NOT create one entry per hearing.
- Order entries chronologically; only include milestones the sources support.

Return ONLY a valid JSON array of entry objects. No markdown, no prose.
Format:
[{{"date": "YYYY-MM-DD", "date_bs": "YYYY-MM-DD", "title": "नेपाली शीर्षक", "description": "विवरण"}}]

{ngm_section}

DOCUMENT TEXT (chargesheet, press release, court order — use for milestones,
narrative, amounts, and any dates not in the NGM data):

{source_text}
"""


def main():
    """Main entry point."""
    ap = argparse.ArgumentParser(
        description="Extract CIAA Special Court case timelines via LLM (DB-free).",
        epilog="Reads cases and writes results entirely over HTTP via JAWAFDEHI_API_TOKEN.",
    )
    add_common_args(ap)

    args = ap.parse_args()

    # Set up logging
    setup_logging(args.verbose)

    # Bootstrap Django + LLM (MUST come before importing llm/sourcing)
    try:
        bootstrap(args.provider, args.model)
    except Exception as exc:
        print(f"Bootstrap failed: {exc}", file=sys.stderr)
        sys.exit(1)

    # Import after bootstrap
    from llm.invoke import invoke_with_tools
    from llm.usage import UsageAccumulator, render_usage_table

    # Validate config
    try:
        api = CaseworkApi(base_url=args.api_base_url, token=args.api_token)
    except RuntimeError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        sys.exit(1)

    # Accumulate token usage
    usage = UsageAccumulator()

    # Collect target cases
    cases = list(get_target_cases(api, args, skip_field="timeline"))

    total = len(cases)
    if total == 0:
        print("No CIAA draft cases to process.", file=sys.stderr)
        sys.exit(0)

    print(f"Found {total} CIAA draft case(s) to process.")
    if args.force:
        print("  --force: re-generating even for populated cases")
    if args.fiscal_year:
        print(f"  Fiscal year filter: {args.fiscal_year}")
    if args.dry_run:
        print("  [DRY RUN] No changes will be saved.")

    stats = {
        "cases_processed": 0,
        "cases_enriched": 0,
        "cases_skipped": 0,
        "cases_no_content": 0,
        "cases_llm_error": 0,
        "cases_already_populated": 0,
    }

    # Process each case
    for idx, case in enumerate(cases, 1):
        try:
            _process_case(
                case=case,
                idx=idx,
                total=total,
                dry_run=args.dry_run,
                api=api,
                usage=usage,
                invoke_with_tools=invoke_with_tools,
                stats=stats,
            )
        except Exception as exc:
            print(f"Unhandled error processing case: {exc}", file=sys.stderr)
            if args.verbose:
                import traceback

                traceback.print_exc()

    # Print summary
    print_summary(stats, args.dry_run, "Timeline extraction")

    # Print usage table
    if usage.calls > 0:
        print()
        print(
            render_usage_table(usage.as_dict()["by_provider"], title="Timeline usage")
        )


def _process_case(
    case: dict,
    idx: int,
    total: int,
    dry_run: bool,
    api: CaseworkApi,
    usage,
    invoke_with_tools,
    stats: dict,
):
    """Process a single case: fetch detail, extract timeline, PATCH or preview."""
    stats["cases_processed"] += 1
    case_id = case.get("case_id", "?")
    title = case.get("title", "")
    print(f"\n[{idx}/{total}] {case_id} — {title[:80]}")

    # Fetch case detail (for enriched evidence with source URLs)
    try:
        detail = api.get_case(case.get("slug") or case_id)
    except requests.HTTPError:
        detail = case
        print("  (using summary instead of detail)")

    # Get source content
    source_text = _get_source_content(detail)
    ngm_data = _get_ngm_data(detail, api)

    if not source_text and not ngm_data:
        stats["cases_no_content"] += 1
        print("  No source content found — skipping")
        return

    if source_text:
        print(f"  Source content: {len(source_text)} chars")
    if ngm_data:
        hearings = ngm_data.get("hearings") or []
        print(f"  NGM data: {len(hearings)} hearing(s)")
    else:
        print("  NGM data: none")

    # Extract timeline via LLM
    try:
        entries = _extract_timeline(
            source_text=source_text or "",
            case_title=title,
            invoke_with_tools=invoke_with_tools,
            usage=usage,
            ngm_data=ngm_data,
        )
    except Exception as exc:
        stats["cases_llm_error"] += 1
        print(f"  LLM extraction failed: {exc}")
        if logger.level == logging.DEBUG:
            import traceback

            traceback.print_exc()
        return

    if not entries:
        stats["cases_skipped"] += 1
        print("  LLM returned no timeline entries — skipping")
        return

    print(f"  Extracted {len(entries)} entry(s)")
    for i, entry in enumerate(entries, 1):
        span = f" → {entry['end_date']}" if entry.get("end_date") else ""
        print(
            f"    {i}. {entry.get('date', '?')}{span} — {entry.get('title', '?')[:80]}"
        )

    if dry_run:
        print("  [DRY RUN] Would PATCH but --dry-run is set")
        return

    # PATCH the timeline
    try:
        case_slug = detail.get("slug") or case.get("slug")
        api.patch_field(case_slug, "timeline", entries)
        stats["cases_enriched"] += 1
        print(f"  [UPDATED] {case_id}")
    except Exception as exc:
        stats["cases_llm_error"] += 1
        print(f"  Failed to PATCH timeline: {exc}")


def _get_source_content(case: dict) -> Optional[str]:
    """Assemble source document text for the milestone-relevant source types."""
    evidence = case.get("evidence") or []
    if not evidence:
        return None

    # Group evidence by source_type to honour milestone priority
    by_type: dict[str, list[dict]] = {}
    for entry in evidence:
        if not isinstance(entry, dict):
            continue
        source = entry.get("source")
        if not isinstance(source, dict):
            continue
        stype = source.get("source_type")
        by_type.setdefault(stype, []).append(entry)

    content_parts: list[str] = []
    for stype in MILESTONE_SOURCE_TYPES:
        for entry in by_type.get(stype, []):
            text = _content_from_evidence_entry(entry)
            if text:
                content_parts.append(text)

    if not content_parts:
        return None
    return "\n\n---\n\n".join(content_parts)


def _content_from_evidence_entry(entry: dict) -> Optional[str]:
    """Return usable text for one evidence entry.

    Order of preference:
    1. The already-extracted evidence description when long enough.
    2. An existing MARKDOWN-role link on the source.
    3. Otherwise, create markdown with the shared source converter.
    """
    description = (entry.get("description") or "").strip()
    if len(description) > 200:
        return description

    source = entry.get("source") or {}
    urls = source.get("urls") or []

    # Use an existing MARKDOWN link if present
    md_link = next(
        (
            u["link"]
            for u in urls
            if isinstance(u, dict) and u.get("role") == "MARKDOWN" and u.get("link")
        ),
        None,
    )
    if md_link:
        try:
            from sourcing import jds_client

            content, _ = jds_client.download_source_file(md_link)
            text = content.decode("utf-8", errors="replace")
            if len(text) > 200:
                return text
        except Exception as exc:
            logger.warning("Failed to download markdown link %s: %s", md_link, exc)

    # Otherwise, use the source converter
    convertible = [
        u["link"]
        for u in urls
        if isinstance(u, dict)
        and u.get("link")
        and u.get("role") in ("RAW", "ALTERNATE", "SOURCE_PAGE")
    ]
    if not convertible:
        return None

    try:
        from sourcing import converter as source_converter

        result = source_converter.convert_source({"url": convertible})
        if result.get("status") in ("converted", "attached"):
            text = (result.get("markdown") or "").strip()
            if len(text) > 200:
                return text
        else:
            logger.warning(
                "Source conversion %s: %s",
                result.get("status"),
                result.get("note"),
            )
    except Exception as exc:
        logger.warning("Source conversion failed: %s", exc)

    return None


def _get_ngm_data(case: dict, api: CaseworkApi) -> Optional[dict]:
    """Fetch NGM hearing records for the case's special-court reference."""
    special_ref = next(
        (
            ref.split(":", 1)[1]
            for ref in (case.get("court_cases") or [])
            if isinstance(ref, str) and ref.startswith("special:")
        ),
        None,
    )
    if not special_ref:
        return None

    try:
        quoted = urllib.parse.quote(f"special:{special_ref}", safe=":")
        data = api.get(f"/ngm/court_case/{quoted}")
    except requests.HTTPError as exc:
        logger.warning("NGM query failed for %s: %s", special_ref, exc)
        return None

    if not isinstance(data, dict) or data.get("error"):
        return None
    return data


def _format_ngm_section(ngm_data: Optional[dict]) -> str:
    """Format the flat NGM API payload as a prompt section."""
    if not ngm_data:
        return ""

    lines = [
        "NGM STRUCTURED HEARING DATA (ground-truth AD dates — convert to BS "
        "with the convert_date tool; use only to anchor milestone dates):",
        "",
    ]
    reg_date = ngm_data.get("registration_date_ad")
    verdict_date = ngm_data.get("verdict_date_ad")
    case_status = ngm_data.get("case_status")

    if reg_date:
        lines.append(f"- Case registration: {reg_date}")
    if case_status:
        lines.append(f"- Case status: {case_status}")

    hearings = ngm_data.get("hearings") or []
    if hearings:
        lines.append(f"- Hearings ({len(hearings)} records):")
        for h in hearings:
            h_date = h.get("hearing_date_ad", "")
            h_decision = h.get("decision_type") or ""
            h_remarks = (h.get("remarks") or "")[:200]
            line = f"  * {h_date}"
            if h_decision:
                line += f" — {h_decision}"
            if h_remarks:
                line += f" — {h_remarks}"
            lines.append(line)

    if verdict_date:
        lines.append(f"- Verdict date: {verdict_date}")
        verdict_judge = ngm_data.get("verdict_judge")
        if verdict_judge:
            lines.append(f"  Judge: {verdict_judge}")

    return "\n".join(lines) + "\n"


def _extract_timeline(
    source_text: str,
    case_title: str,
    invoke_with_tools,
    usage,
    ngm_data: Optional[dict] = None,
) -> Optional[list[dict]]:
    """Call the LLM (with the convert_date tool) to extract timeline entries."""
    prompt = EXTRACTION_USER_PROMPT.format(
        case_title=case_title,
        ngm_section=_format_ngm_section(ngm_data),
        source_text=source_text[:40000],
    )

    # Call LLM with tool support
    response_text = invoke_with_tools(
        system=EXTRACTION_SYSTEM_PROMPT,
        content=prompt,
        tools=[convert_date_tool()],
        max_tokens=4000,
        tier="premium",
        usage=usage,
        max_iterations=30,
    )

    return _parse_timeline_response(response_text)


def _parse_timeline_response(response_text: str) -> Optional[list[dict]]:
    """Parse the LLM response into clean, validated timeline entries."""
    raw = parse_extraction_response(response_text, wrapper_keys={"timeline", "entries"})
    if raw is None:
        return None

    clean = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        entry = _clean_entry(item)
        if entry is not None:
            clean.append(entry)

    if not clean:
        return None
    clean.sort(key=lambda e: e["date"])
    return clean


def _clean_entry(item: dict) -> Optional[dict]:
    """Validate and normalise a single LLM-produced entry."""
    date_val = str(item.get("date") or "").strip()
    title_val = str(
        item.get("title") or item.get("event") or item.get("name") or ""
    ).strip()
    if not date_val or not title_val:
        return None
    if not is_valid_iso_date(date_val):
        logger.warning("Dropping entry with non-ISO date: %s", date_val)
        return None

    entry: dict = {"date": date_val, "title": title_val}

    desc_val = str(
        item.get("description") or item.get("desc") or item.get("detail") or ""
    ).strip()
    if desc_val:
        entry["description"] = desc_val

    date_bs = str(item.get("date_bs") or "").strip()
    if date_bs:
        entry["date_bs"] = date_bs

    end_date = str(item.get("end_date") or "").strip()
    if end_date:
        if not is_valid_iso_date(end_date):
            logger.warning("Dropping invalid end_date %s; keeping entry", end_date)
        elif end_date < date_val:
            logger.warning(
                "Dropping end_date %s before date %s; keeping entry", end_date, date_val
            )
        else:
            entry["end_date"] = end_date
            end_date_bs = str(item.get("end_date_bs") or "").strip()
            if end_date_bs:
                entry["end_date_bs"] = end_date_bs

    return entry


if __name__ == "__main__":
    main()
