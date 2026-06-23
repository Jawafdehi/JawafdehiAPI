#!/usr/bin/env python
"""Enrich missing BIGO values for DRAFT cases using press releases + LLM extraction.

Standalone script to extract a case's BIGO (बिगो, disputed amount / damage claim)
from CIAA press-release sources using LLM extraction, fully over the Jawafdehi HTTP API.
Never touches the database.

Phase A.x of the CIAA Case Enrichment pipeline. Populates ``Case.bigo`` with the
official amount under dispute — the მააქვს claimed by the CIAA/prosecution.

Usage:
    python casework/enrich_missing_bigo.py --dry-run
    python casework/enrich_missing_bigo.py --slug case-0123
    python casework/enrich_missing_bigo.py --limit 10 --verbose
    python casework/enrich_missing_bigo.py --force
"""

import argparse
import json
import logging
import os
import re
import sys
import urllib.parse
from typing import Optional

import requests

# Ensure the api dir is in sys.path so imports work when run as a file
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from casework.common import (
    CaseworkApi,
    add_common_args,
    balanced_object,
    bootstrap,
    clamp,
    env_int,
    get_target_cases,
    print_summary,
    setup_logging,
)

logger = logging.getLogger(__name__)

# Nepali keywords that signal BIGO context
BIGO_CONTEXT_KEYWORDS = (
    "बिगो",
    "मागदाबी",
    "हानि",
    "हानी",
    "नोक्सानी",
    "क्षति",
    "damage claim",
    "loss amount",
    "corruption loss",
)

_NEPALI_TO_ASCII_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")

EXTRACTION_SYSTEM_PROMPT = """\
You extract BIGO (बिगो), the damage claim amount / मागदाबी, from CIAA press release \
content.

Return STRICT JSON only with this schema:
{
  "bigo": <integer or null>,
  "confidence": "high" | "medium" | "low",
  "evidence_quote": "<short quote from text that supports the amount>",
  "press_release_type": "sting_operation" | "appeal_review" | "charge_filing" | "other"
}

CRITICAL RULES (apply in order):

Rule 1 — Output format
BIGO must be an NPR integer only. No commas, no currency symbols (रू/Rs/NPR), no \
paisa suffix, no floats.
CIAA marks paisa with a danda '।' OR a slash '/' (e.g. १,४६,८१,२२५।९० or \
१,४६,८१,२२५/९०). If the extracted amount has a paisa portion, strip everything \
after the '।' or '/' before returning — return the rupee part only.

Rule 2 — Numeral normalization
Before any matching, normalize Devanagari digits to Arabic (०→0, १→1, ... ९→9). CIAA \
PDFs mix both in the same number.
Then strip commas. Then strip the paisa suffix (everything from the first '।' or \
'/'). Then parse as integer. Never let paisa digits merge into the rupee figure — \
e.g. २३,७५,४६,३२४।५७ is 237546324, NOT 2375463245.

Rule 3 — Type-routing first (CHECK THIS BEFORE READING TEXT)
Before reading any text, determine the press release type:
- Sting Operation (रंगेहात, sting, caught red-handed) → return null (high confidence). \
  The amounts in sting releases are physical cash caught during arrest — \
  bribe/unexplained cash — not a formally established bigo.
- Appeal/Review (पुनरावेदन, अपील, appeal, review) → return null (high confidence). \
  These record CIAA appealing a court verdict; bigo was defined at charge-sheet stage \
  and is not re-stated here.
- Charge Filing (अभियोग दायर, charge filed, मुद्दा दर्ता) → proceed to extraction \
  rules below.
- Other → proceed to extraction rules below.

Rule 4 — Null with low confidence
If no reliable bigo signal exists after all checks, return null with low confidence. \
Do not guess.

Rule 5 — No ranges, floats, or formatted strings
Never return ranges (१-५ लाख), floats (1.5 करोड), or formatted strings. Integer only.

Rule 6 — Priority signal hierarchy (apply in order, stop at first match)
1. Title contains "बिगो रू.[AMOUNT] कायम गरी" → extract AMOUNT → high confidence
2. PDF table row under "बिगो रू." column header → extract amount → high confidence
3. PDF body sentence "कूल आय भन्दा कूल व्यय ... [AMOUNT] ले बढी" (excess of \
   expenditure over income) → extract AMOUNT → medium confidence, verify it \
   matches signal 2
4. No match → null

Note: "बिगो बमोजिम जरिवाना" is a reference to an already-stated bigo, not a \
declaration of it — use it only to confirm, not as a primary source.

Rule 7 — Multiple amounts: label is mandatory
When multiple monetary amounts appear in the text, extract only the one explicitly \
labeled as bigo using the hierarchy in Rule 6.
If multiple amounts are present and none carries a clear bigo label, return null — \
do not pick the largest, the first, or the one that "seems right."

Rule 8 — Ignore list (NEVER extract these as bigo)
Amount type | Nepali marker
------------|---------------
Bribe received/demanded | घुस/रिसवत रकम रू.
Unexplained cash seized | स्रोत नखुलेको रकम रू.
Lawful income subtotal | जम्मा/कूल आय रू.
Expenditure subtotal | जम्मा/कूल व्यय रू.
Fine/penalty | जरिवाना रू.
Contract/budget amount | ठेक्का रकम, बजेट रकम
Asset seizure value | जफत गर्ने सम्पत्ति रू.
Co-accused row with — | no amount; asset forfeiture only

IMPORTANT: Income and expenditure subtotals are always larger than bigo in illegal \
property cases. If you accidentally extract either, the number will be bigger than \
the bigo stated in the table. Use this as a sanity check.

Rule 9 — Vague/verbal amounts → null
If the amount is expressed in vague prose (करोडौं, अरबौं, लाखौं) with no \
accompanying numeric, return null.
Word-amount parsing of Nepali number words is error-prone and CIAA's structured \
documents always pair prose amounts with numerics when a formal bigo exists.
"""

EXTRACTION_USER_PROMPT = """\
Extract the BIGO (damage claim amount) from the following CIAA press release.

Case ID: {case_id}
Case title: {case_title}

Source metadata (title, description, filenames, URLs):
{source_context}

Press release markdown:
{markdown}
"""


def main():
    """Main entry point."""
    ap = argparse.ArgumentParser(
        description="Extract CIAA Special Court case BIGO via LLM (DB-free).",
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
    from llm.invoke import invoke_text
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
    cases = list(get_target_cases(api, args, skip_field="bigo"))

    total = len(cases)
    if total == 0:
        print("No CIAA draft cases with missing BIGO to process.", file=sys.stderr)
        sys.exit(0)

    print(f"Found {total} CIAA draft case(s) to process.")
    if args.force:
        print("  --force: re-extracting even for populated cases")
    if args.dry_run:
        print("  [DRY RUN] No changes will be saved.")

    stats = {
        "cases_processed": 0,
        "cases_enriched": 0,
        "cases_skipped": 0,
        "cases_no_content": 0,
        "cases_llm_error": 0,
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
                invoke_text=invoke_text,
                stats=stats,
            )
        except Exception as exc:
            stats["cases_llm_error"] += 1
            print(f"Unhandled error processing case: {exc}", file=sys.stderr)
            if args.verbose:
                import traceback

                traceback.print_exc()

    # Print summary
    print_summary(stats, args.dry_run, "BIGO extraction")

    # Print usage table
    if usage.calls > 0:
        print()
        print(render_usage_table(usage.as_dict()["by_provider"], title="bigo usage"))


def _process_case(
    case: dict,
    idx: int,
    total: int,
    dry_run: bool,
    api: CaseworkApi,
    usage,
    invoke_text,
    stats: dict,
):
    """Process a single case: fetch detail, extract BIGO, PATCH or preview."""
    stats["cases_processed"] += 1
    case_id = case.get("case_id", "?")
    title = case.get("title", "")
    print(f"\n[{idx}/{total}] {case_id} — {title[:80]}")

    # Fetch case detail
    try:
        detail = api.get_case(case.get("slug") or case_id)
    except requests.HTTPError:
        detail = case
        print("  (using summary instead of detail)")

    # Get source content
    source_text, source_context = _get_source_content(detail)

    if not source_text:
        stats["cases_no_content"] += 1
        print("  No press-release source content found — skipping")
        return

    print(f"  Source content: {len(source_text)} chars")

    # Extract BIGO via LLM
    try:
        bigo = _extract_bigo_from_source(
            source_text=source_text,
            source_context=source_context,
            case_id=case_id,
            case_title=title,
            invoke_text=invoke_text,
            usage=usage,
        )
    except Exception as exc:
        stats["cases_llm_error"] += 1
        print(f"  LLM extraction failed: {exc}")
        if logger.level == logging.DEBUG:
            import traceback

            traceback.print_exc()
        return

    if bigo is None:
        stats["cases_skipped"] += 1
        print("  LLM could not extract a reliable BIGO — skipping")
        return

    print(f"  Extracted BIGO: {bigo}")

    if dry_run:
        print("  [DRY RUN] Would PATCH but --dry-run is set")
        return

    # PATCH the BIGO
    try:
        case_slug = detail.get("slug") or case.get("slug")
        api.patch_field(case_slug, "bigo", bigo)
        stats["cases_enriched"] += 1
        print(f"  [UPDATED] {case_id}: BIGO={bigo}")
    except Exception as exc:
        stats["cases_llm_error"] += 1
        print(f"  Failed to PATCH BIGO: {exc}")


def _get_source_content(case: dict) -> tuple[Optional[str], str]:
    """Assemble source document text for press-release sources.

    Returns (source_text, source_context_for_llm_prompt).
    """
    evidence = case.get("evidence") or []
    if not evidence:
        return None, ""

    # Find press-release sources
    sources = []
    for entry in evidence:
        if not isinstance(entry, dict):
            continue
        source = entry.get("source")
        if not isinstance(source, dict):
            continue
        stype = source.get("source_type")
        if stype == "CIAA_PRESS_RELEASE":
            sources.append(entry)

    if not sources:
        return None, ""

    # Use first press release
    entry = sources[0]
    description = (entry.get("description") or "").strip()

    # Prefer long descriptions
    if len(description) > 200:
        return description, _build_source_context_from_entry(entry)

    # Otherwise, try to fetch markdown from source
    source = entry.get("source") or {}
    urls = source.get("urls") or []

    # Use MARKDOWN role link if present
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
                return text, _build_source_context_from_entry(entry)
        except Exception as exc:
            logger.warning("Failed to download markdown link %s: %s", md_link, exc)

    # Fall back to source converter
    convertible = [
        u["link"]
        for u in urls
        if isinstance(u, dict)
        and u.get("link")
        and u.get("role") in ("RAW", "ALTERNATE", "SOURCE_PAGE")
    ]
    if not convertible:
        return None, ""

    try:
        from sourcing import converter as source_converter

        result = source_converter.convert_source({"url": convertible})
        if result.get("status") in ("converted", "attached"):
            text = (result.get("markdown") or "").strip()
            if len(text) > 200:
                return text, _build_source_context_from_entry(entry)
        else:
            logger.warning(
                "Source conversion %s: %s",
                result.get("status"),
                result.get("note"),
            )
    except Exception as exc:
        logger.warning("Source conversion failed: %s", exc)

    return None, ""


def _build_source_context_from_entry(entry: dict) -> str:
    """Build source metadata context for the LLM prompt."""
    source = entry.get("source") or {}
    parts = [
        f"title: {source.get('title') or ''}",
        f"description: {source.get('description') or ''}",
    ]
    urls = source.get("urls") or []
    for u in urls:
        if isinstance(u, dict) and u.get("link"):
            parts.append(f"url: {urllib.parse.unquote(u['link'])}")
    return "\n".join(parts)[: env_int("CASEWORK_BIGO_SOURCE_CHARS", 20000)]


def _extract_bigo_from_source(
    source_text: str,
    source_context: str,
    case_id: str,
    case_title: str,
    invoke_text,
    usage,
) -> Optional[int]:
    """Call the LLM to extract BIGO from source markdown."""
    prompt = EXTRACTION_USER_PROMPT.format(
        case_id=case_id,
        case_title=case_title,
        source_context=source_context,
        markdown=clamp(
            source_text, env_int("CASEWORK_BIGO_FEED_CHARS", 100000), "bigo source"
        ),
    )

    # Call LLM (plain chat, no tools)
    response_text = invoke_text(
        system=EXTRACTION_SYSTEM_PROMPT,
        content=prompt,
        max_tokens=2000,
        tier="premium",
        usage=usage,
    )

    return _parse_bigo_response(response_text)


def _parse_bigo_response(response_text: str) -> Optional[int]:
    """Parse the LLM response into a BIGO integer or None."""
    text = response_text.strip()

    # Try to extract JSON (plain or fenced)
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            bigo_val = obj.get("bigo")
            confidence = str(obj.get("confidence", "")).strip().lower()
            evidence_quote = obj.get("evidence_quote")

            # Skip if low confidence
            if confidence == "low":
                return None

            # Skip if no explicit BIGO context
            if not _is_explicit_bigo_context(evidence_quote):
                return None

            return _coerce_bigo_int(bigo_val)
    except json.JSONDecodeError:
        pass

    # Try fenced JSON
    fenced = re.search(r"```(?:json)?\s*(\{[^}]*\})\s*```", text, re.DOTALL)
    if fenced:
        try:
            obj = json.loads(fenced.group(1))
            if isinstance(obj, dict):
                bigo_val = obj.get("bigo")
                confidence = str(obj.get("confidence", "")).strip().lower()
                evidence_quote = obj.get("evidence_quote")

                if confidence == "low":
                    return None
                if not _is_explicit_bigo_context(evidence_quote):
                    return None

                return _coerce_bigo_int(bigo_val)
        except json.JSONDecodeError:
            pass

    # Scan for balanced JSON objects (string-aware). A brace regex broke on
    # braces inside quoted fields like evidence_quote and on deeper nesting.
    for start in range(len(text)):
        if text[start] != "{":
            continue
        block = balanced_object(text, start)
        if not block:
            continue
        try:
            obj = json.loads(block)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "bigo" in obj:
            bigo_val = obj.get("bigo")
            confidence = str(obj.get("confidence", "")).strip().lower()
            evidence_quote = obj.get("evidence_quote")

            if confidence == "low":
                return None
            if not _is_explicit_bigo_context(evidence_quote):
                return None

            return _coerce_bigo_int(bigo_val)

    return None


def _is_explicit_bigo_context(evidence_quote: any) -> bool:
    """Check if evidence quote contains explicit BIGO context keywords."""
    if not isinstance(evidence_quote, str):
        return False
    normalized_quote = evidence_quote.strip().lower()
    if not normalized_quote:
        return False
    return any(keyword in normalized_quote for keyword in BIGO_CONTEXT_KEYWORDS)


def _coerce_bigo_int(value: any) -> Optional[int]:
    """Coerce a value to a positive integer or None."""
    if value is None:
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float):
        return int(value) if value > 0 else None
    if not isinstance(value, str):
        return None

    normalized = value.translate(_NEPALI_TO_ASCII_DIGITS)
    # CIAA writes paisa after a danda '।', slash '/', or dot '.' (e.g. ...३२४।५७,
    # ...२२५/९०, ...324.57). Stripping all non-digits would fold the paisa digits
    # into the rupee figure and inflate it 10-100x (e.g. 237546324।57 ->
    # 2375463245). Anchor at the first digit before cutting paisa so a leading
    # currency prefix ('रु.') isn't mistaken for a paisa separator. Rupee grouping
    # uses commas only, so cutting at the first '।'/'/'/'.' after the digits is safe.
    first_digit = re.search(r"\d", normalized)
    if not first_digit:
        return None
    rupees = re.split(r"[।/.]", normalized[first_digit.start() :], maxsplit=1)[0]
    digits_only = re.sub(r"[^\d]", "", rupees)
    if not digits_only:
        return None
    bigo = int(digits_only)
    return bigo if bigo > 0 else None


if __name__ == "__main__":
    main()
