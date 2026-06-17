#!/usr/bin/env python
"""Enrich CIAA Special Court cases with key allegations (DB-free script using the llm package).

Standalone script to extract 2-3 key allegations from CIAA press releases using LLM
extraction, fully over the Jawafdehi HTTP API. Never touches the database.

Phase A.2 of the CIAA Case Enrichment pipeline. Populates ``Case.key_allegations``
with the primary misconduct allegations extracted from CIAA press release markdown.

Usage:
    python casework/enrich_allegations.py --dry-run
    python casework/enrich_allegations.py --slug case-0123
    python casework/enrich_allegations.py --limit 10 --verbose
    python casework/enrich_allegations.py --fiscal-year 080 --dry-run
    python casework/enrich_allegations.py --force
"""

import argparse
import logging
import os
import re
import sys
from typing import Optional

import requests

# Ensure the api dir is in sys.path so imports work when run as a file
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from casework.common import CaseworkApi, bootstrap

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a Nepali legal analyst extracting structured key allegations
from CIAA (Commission for the Investigation of Abuse of Authority) press releases.

Every allegation MUST:
1. Be factually grounded in the provided press release — NO fabrication
2. Be written in professional, accessible Nepali (नेपाली)
3. Focus on accused entities and alleged acts, not long name lists
4. Group accused people by office/role when many names appear, naming only the principal accused or role group when enough
5. Describe related entities by role when possible (for example, "एक निजी कम्पनी", "निर्माण व्यवसायी", or "सम्बन्धित उपभोक्ता समिति") unless the press release makes a name essential
6. Describe the specific misconduct mechanism (what was done and how)
7. Include the disputed amount (बिगो) when mentioned in the source, using readable Nepali-scale wording when possible (for example, "रु. ३८ करोडभन्दा बढी" instead of "रु. ३८,६७,१७,६४०")
8. Include the time period (date range or fiscal year) when specified
9. Be self-contained — understandable without additional context
10. Follow the established Jawafdehi allegation style (see examples below)

Return 2-3 allegations. Each allegation MUST be exactly one sentence.
The first allegation MUST be the most descriptive overview of the primary allegation.
The first allegation MUST mention the core institution/property/transaction, the alleged scheme, and the financial harm; it MUST NOT spend most of the sentence listing accused names.
The second and third allegations, if present, MUST be shorter supporting allegations that add a different mechanism or actor role; they MUST NOT restate the first allegation with different wording.
Use formal but clear Nepali.

DO NOT:
- Fabricate or embellish beyond the source text
- Use legal jargon without explanation
- State legal conclusions about guilt or innocence
- Write vague statements like "भ्रष्टाचार गरेको"
- Mix multiple unrelated misconducts into one allegation
- Start with a long comma-separated list of accused names when a role group can carry the allegation
- Produce near-duplicate allegations that repeat the same accused list and same misconduct
- Write remedies, requests, or procedural outcomes as allegations, such as asset-return demands, confiscation requests, charge filing, or punishment requests
- End allegations with attribution phrases such as "उल्लेख छ", "भनिएको छ", "जनाइएको छ", "देखिन्छ", or "आरोप छ"
- Include multiple sentences in one allegation
- List related entity names when a descriptive role is enough
- Use long comma-formatted Nepali amounts when a readable crore/lakh approximation is clearer

STRUCTURE the first allegation in Nepali as:
"मुख्य पदाधिकारी/भूमिका समूहले — कुन संस्था/सम्पत्ति/कारोबारमा — के योजना/कृत्य गरे — कसरी — कति रकम/हानि — कुन अवधिमा"
(Principal role group — institution/property/transaction — alleged scheme/action — mechanism — amount/harm — period)

STRUCTURE supporting allegations as shorter statements describing secondary mechanisms, supporting acts, or specific misuse patterns.
Each supporting allegation MUST still describe alleged misconduct by an accused actor, not a legal remedy or requested court outcome.
If the source lists many accused names, compress them into a role group such as "तत्कालीन अध्यक्ष र सञ्चालक समिति सदस्यहरू" unless one person's name is necessary to identify the case.

REFERENCE EXAMPLES from published Jawafdehi cases:

Example 1 (Illegal property accumulation):
"कमल राज गौतमले मिति २०५५/०१/०७ देखि २०७९/१२/२४ सम्म सार्वजनिक पद धारण
गर्दा वैध आयभन्दा रु. २,५१,७८,६८७.७१ बढी सम्पत्ति खर्च तथा लगानी गरी
गैरकानूनी रूपमा सम्पत्ति आर्जन गरेको।"

Example 2 (Procurement fraud):
"प्रतिवादीहरूको मिलेमतोमा काठमाडौं महानगरपालिकाको NCBW-KMC को ठेक्कामा
Pending Litigation नहुने विषयलाई Pending Litigation रहेको भनी गलत मूल्याङ्कन
प्रतिवेदन खडा गरी सार्वजनिक सम्पत्ति बदनियतपूर्वक हानि नोक्सानी पुर्याएको।"

Example 3 (Bribery and money laundering):
"मोहनबहादुर बस्नेतले नगर प्रमुख पदको दुरुपयोग गरी पद्मा कम्पनीहरू र राजु
प्रसाद कँडेललाई कर छुट र जग्गा उपलब्धता लगायत अनुचित लाभ पुर्याई सो बापत
करिब रु. ९.२२ करोड घुस/रिसवत लिएको।"

Example 4 (Embezzlement):
"प्रतिवादीहरूको मिलेमतोमा हुलाक बचत बैङ्कमा बचतकर्ताहरूको निक्षेप रकम
बैङ्क दाखिला नगरी अपचलन गरी हिनामिना गरेको।"
"""

USER_PROMPT_TEMPLATE = """Extract 2-3 key allegation statements from this CIAA press release.

Case title: {case_title}
Bigo amount: {bigo}

Instructions:
- Each allegation must be exactly one complete, self-contained sentence in Nepali
- Do not end any allegation with attribution wording such as "उल्लेख छ", "भनिएको छ", "जनाइएको छ", "देखिन्छ", or "आरोप छ"
- Make the first allegation a descriptive overview of the primary allegation
- Make the first allegation about substance: institution/property/transaction, alleged scheme, mechanism, amount or harm, and period when available
- Make the second and third allegations shorter supporting allegations
- Make each allegation distinct; do not repeat the same accused list and same misconduct in multiple sentences
- Each allegation must describe alleged misconduct by accused actors, not remedies or procedural outcomes such as asset-return demands, confiscation requests, charge filing, or punishment requests
- Focus on accused entities and their acts; do not include related entity names unless essential
- Prefer role descriptions for related entities, such as "एक निजी कम्पनी", "निर्माण व्यवसायी", or "सम्बन्धित उपभोक्ता समिति"
- When many accused names are listed, group them by role such as "तत्कालीन अध्यक्ष र सञ्चालक समिति सदस्यहरू"; include individual names only when needed to identify the principal accused or a distinct act
- Include names and positions of accused entities when available, but do not let name lists dominate the allegation
- Include amounts and time periods when available, but express large amounts readably in Nepali scale when possible, such as "रु. ३८ करोडभन्दा बढी" instead of "रु. ३८,६७,१७,६४०"
- Extract distinct allegations, not variations of the same claim

Press release text:

{press_release}

IMPORTANT: Return ONLY a valid JSON object with an "allegations" key.
Example:
{{"allegations": ["पहिलो मुख्य आरोप...", "दोस्रो मुख्य आरोप..."]}}
No explanations, no markdown, no text outside the JSON object."""


def main():
    """Main entry point."""
    ap = argparse.ArgumentParser(
        description="Extract key allegations from CIAA press releases via LLM (DB-free).",
        epilog="Reads cases and writes results entirely over HTTP via JAWAFDEHI_API_TOKEN.",
    )
    ap.add_argument(
        "--slug",
        type=str,
        action="append",
        dest="slugs",
        help="Process specific case(s) by slug (repeatable)",
    )
    ap.add_argument(
        "--case-id",
        type=str,
        help="Process a specific case by case_id",
    )
    ap.add_argument(
        "--limit",
        type=int,
        help="Maximum number of cases to process",
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="Re-generate allegations even if already populated",
    )
    ap.add_argument(
        "--fiscal-year",
        type=str,
        help="Filter by fiscal year (e.g., '080' or '081')",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview without PATCHing the API",
    )
    ap.add_argument(
        "--provider",
        type=str,
        choices=("proxy", "bedrock"),
        default="proxy",
        help="LLM provider (default: proxy)",
    )
    ap.add_argument(
        "--model",
        type=str,
        default="",
        help=(
            "Model id for the provider (proxy combo name or bedrock model id); "
            "defaults to JAWAFDEHI_LLM_MODEL. Required for proxy."
        ),
    )
    ap.add_argument(
        "--api-base-url",
        type=str,
        default=None,
        help="Jawafdehi API base URL (defaults to JAWAFDEHI_API_BASE_URL or http://127.0.0.1:8000)",
    )
    ap.add_argument(
        "--api-token",
        type=str,
        default=None,
        help="Jawafdehi API token (defaults to JAWAFDEHI_API_TOKEN)",
    )
    ap.add_argument(
        "--verbose",
        action="store_true",
        help="Enable verbose debug logging",
    )

    args = ap.parse_args()

    # Set up logging
    if args.verbose:
        logging.basicConfig(
            level=logging.DEBUG,
            format="%(levelname)s: %(message)s",
        )
    else:
        logging.basicConfig(
            level=logging.INFO,
            format="%(levelname)s: %(message)s",
        )

    # Bootstrap Django + LLM (MUST come before importing llm)
    try:
        bootstrap(args.provider, args.model)
    except Exception as exc:
        print(f"Bootstrap failed: {exc}", file=sys.stderr)
        sys.exit(1)

    # Import after bootstrap
    from cases.management.commands import _enrich_utils
    from llm.invoke import invoke_text
    from llm.usage import UsageAccumulator, render_usage_table

    # Validate config
    try:
        api = CaseworkApi(base_url=args.api_base_url, token=args.api_token)
    except RuntimeError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        sys.exit(1)

    if args.fiscal_year and not re.match(r"^\d{2,3}$", args.fiscal_year):
        print(
            f"Invalid fiscal year: {args.fiscal_year}. "
            "Use 2- or 3-digit format, e.g., '80' or '080'.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Accumulate token usage
    usage = UsageAccumulator()

    # Collect target cases
    cases = _get_target_cases(
        api=api,
        slugs=args.slugs,
        case_id=args.case_id,
        limit=args.limit,
        force=args.force,
        fiscal_year=args.fiscal_year,
    )

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
                invoke_text=invoke_text,
                _enrich_utils=_enrich_utils,
                stats=stats,
            )
        except Exception as exc:
            print(f"Unhandled error processing case: {exc}", file=sys.stderr)
            if args.verbose:
                import traceback

                traceback.print_exc()

    # Print summary
    _print_summary(stats, args.dry_run)

    # Print usage table
    if usage.calls > 0:
        print()
        print(
            render_usage_table(
                usage.as_dict()["by_provider"], title="allegations usage"
            )
        )


def _get_target_cases(
    api: CaseworkApi,
    slugs: Optional[list[str]] = None,
    case_id: Optional[str] = None,
    limit: Optional[int] = None,
    force: bool = False,
    fiscal_year: Optional[str] = None,
) -> list[dict]:
    """Fetch target CIAA Special Court cases to enrich."""
    selected: list[dict] = []

    # If specific slugs provided, fetch them individually
    if slugs:
        for slug in slugs:
            try:
                case = api.get_case(slug)
                if _is_ciaa_special_court_case(case):
                    if not force and case.get("key_allegations"):
                        print(f"  Skipping {slug}: key_allegations already populated")
                        continue
                    selected.append(case)
                    if limit and len(selected) >= limit:
                        return selected
            except requests.HTTPError as exc:
                print(f"  Failed to fetch case {slug}: {exc}", file=sys.stderr)
                continue
        return selected

    # Otherwise, iterate all DRAFT CORRUPTION cases
    params = {"case_type": "CORRUPTION", "state": "DRAFT"}
    for case_summary in api.iter_cases(params=params):
        if case_id and case_summary.get("case_id") != case_id:
            continue
        if not _is_ciaa_special_court_case(case_summary):
            continue
        if fiscal_year and not _matches_fiscal_year(case_summary, fiscal_year):
            continue

        if not force and case_summary.get("key_allegations"):
            continue

        selected.append(case_summary)
        if limit and len(selected) >= limit:
            return selected

    return selected


def _is_ciaa_special_court_case(case: dict) -> bool:
    """Check if case is tried at CIAA Special Court."""
    court_cases = case.get("court_cases") or []
    return isinstance(court_cases, list) and any(
        isinstance(ref, str) and ref.startswith("special:") for ref in court_cases
    )


def _matches_fiscal_year(case: dict, fiscal_year: str) -> bool:
    """Check if case's court reference matches the fiscal year."""
    fy_normalized = fiscal_year.lstrip("0") or "0"
    for entry in case.get("court_cases") or []:
        if not isinstance(entry, str):
            continue
        case_number = entry.split(":")[-1] if ":" in entry else entry
        if "-CR-" in case_number:
            prefix = case_number.split("-CR-")[0].lstrip("0") or "0"
            if prefix == fy_normalized:
                return True
    return False


def _process_case(
    case: dict,
    idx: int,
    total: int,
    dry_run: bool,
    api: CaseworkApi,
    usage,
    invoke_text,
    _enrich_utils,
    stats: dict,
):
    """Process a single case: fetch detail, extract allegations, PATCH or preview."""
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

    # Get press release content
    press_release_text = _get_press_release_content(detail)

    if not press_release_text:
        stats["cases_no_content"] += 1
        print("  No press release content found — skipping")
        return

    print(f"  Source content: {len(press_release_text)} chars")

    # Get bigo (disputed amount) for context
    bigo = detail.get("bigo")
    bigo_display = f"रु {bigo:,}" if bigo else "उल्लेख छैन"

    # Extract allegations via LLM
    try:
        allegations = _extract_allegations(
            press_release_text=press_release_text,
            case_title=title,
            bigo=bigo_display,
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

    if not allegations:
        stats["cases_skipped"] += 1
        print("  LLM returned no allegations — skipping")
        return

    print(f"  Extracted {len(allegations)} allegation(s)")
    for i, allegation in enumerate(allegations, 1):
        print(f"    {i}. {allegation[:80]}")

    if dry_run:
        print("  [DRY RUN] Would PATCH but --dry-run is set")
        return

    # PATCH the allegations
    try:
        case_slug = detail.get("slug") or case.get("slug")
        api.patch_field(case_slug, "key_allegations", allegations)
        stats["cases_enriched"] += 1
        print(f"  [UPDATED] {case_id}")
    except Exception as exc:
        stats["cases_llm_error"] += 1
        print(f"  Failed to PATCH allegations: {exc}")


def _content_from_evidence_entry(entry: dict) -> Optional[str]:
    """Usable text for one evidence entry: description, an existing MARKDOWN link,
    or a fresh likhit conversion of the source — same logic as enrich_timeline.py."""
    description = (entry.get("description") or "").strip()
    if len(description) > 200:
        return description

    source = entry.get("source") or {}
    urls = source.get("urls") or []

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
        except Exception:  # noqa: BLE001
            pass

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
    except Exception:  # noqa: BLE001
        pass
    return None


def _get_press_release_content(case: dict) -> Optional[str]:
    """Press-release markdown from case evidence (converts the source if needed)."""
    for entry in case.get("evidence") or []:
        if not isinstance(entry, dict):
            continue
        source = entry.get("source")
        if not isinstance(source, dict):
            continue
        if source.get("source_type") != "CIAA_PRESS_RELEASE":
            continue
        text = _content_from_evidence_entry(entry)
        if text:
            return text
    return None


def _extract_allegations(
    press_release_text: str,
    case_title: str,
    bigo: str,
    invoke_text,
    usage,
) -> Optional[list[str]]:
    """Call the LLM (without tools) to extract allegations from press release."""
    prompt = USER_PROMPT_TEMPLATE.format(
        case_title=case_title,
        bigo=bigo,
        press_release=press_release_text[:60000],
    )

    # Call LLM without tools (plain text generation)
    response_text = invoke_text(
        system=SYSTEM_PROMPT,
        content=prompt,
        max_tokens=2000,
        tier="premium",
        usage=usage,
    )

    return _parse_allegations_response(response_text)


def _parse_allegations_response(response_text: str) -> Optional[list[str]]:
    """Parse the LLM response into clean allegations."""
    import json

    raw_text = response_text.strip()

    # Try to extract JSON from the response
    # Strategy 1: look for fenced JSON
    if "```" in raw_text:
        start = raw_text.find("```")
        if start != -1:
            nl = raw_text.find("\n", start)
            if nl != -1:
                end = raw_text.find("```", nl)
                if end != -1:
                    raw_text = raw_text[nl + 1 : end].strip()

    # Strategy 2: find JSON object wrapper
    obj_start = raw_text.find("{")
    if obj_start != -1:
        obj_end = raw_text.rfind("}")
        if obj_end != -1:
            json_str = raw_text[obj_start : obj_end + 1]
            try:
                data = json.loads(json_str)
                if isinstance(data, dict):
                    allegations = data.get("allegations")
                    if isinstance(allegations, list):
                        clean = [
                            str(a).strip()
                            for a in allegations
                            if isinstance(a, str) and a.strip()
                        ]
                        if clean:
                            return clean[:3]
            except json.JSONDecodeError:
                pass

    # Strategy 3: try to find bare array
    arr_start = raw_text.find("[")
    if arr_start != -1:
        arr_end = raw_text.rfind("]")
        if arr_end != -1:
            json_str = raw_text[arr_start : arr_end + 1]
            try:
                allegations = json.loads(json_str)
                if isinstance(allegations, list):
                    clean = [
                        str(a).strip()
                        for a in allegations
                        if isinstance(a, str) and a.strip()
                    ]
                    if clean:
                        return clean[:3]
            except json.JSONDecodeError:
                pass

    return None


def _print_summary(stats: dict, dry_run: bool):
    """Print final stats summary."""
    print("\n" + "=" * 60)
    print(f"{'[DRY RUN] ' if dry_run else ''}Allegation extraction complete.")
    print(f"  Cases processed:        {stats['cases_processed']}")
    print(f"  Cases enriched:         {stats['cases_enriched']}")
    print(f"  Cases skipped:          {stats['cases_skipped']}")
    print(f"  No source content:      {stats['cases_no_content']}")
    print(f"  LLM errors:             {stats['cases_llm_error']}")


if __name__ == "__main__":
    main()
