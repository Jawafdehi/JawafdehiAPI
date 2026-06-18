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
import sys
from typing import Optional

import requests

# Ensure the api dir is in sys.path so imports work when run as a file
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from casework.common import (
    CaseworkApi,
    add_common_args,
    bootstrap,
    clamp,
    content_from_evidence_entry,
    env_int,
    get_target_cases,
    parse_extraction_response,
    print_summary,
    setup_logging,
)

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
    add_common_args(ap)

    args = ap.parse_args()

    # Set up logging
    setup_logging(args.verbose)

    # Bootstrap Django + LLM (MUST come before importing llm)
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
    cases = list(get_target_cases(api, args, skip_field="key_allegations"))

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
                stats=stats,
            )
        except Exception as exc:
            stats["cases_llm_error"] += 1
            print(f"Unhandled error processing case: {exc}", file=sys.stderr)
            if args.verbose:
                import traceback

                traceback.print_exc()

    # Print summary
    print_summary(stats, args.dry_run, "Allegation extraction")

    # Print usage table
    if usage.calls > 0:
        print()
        print(
            render_usage_table(
                usage.as_dict()["by_provider"], title="allegations usage"
            )
        )


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
        text = content_from_evidence_entry(entry)
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
        press_release=clamp(
            press_release_text,
            env_int("CASEWORK_PRESS_RELEASE_CHARS", 60000),
            "press release",
        ),
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
    """Parse the LLM response into clean allegations (at most 3)."""
    entries = parse_extraction_response(response_text, {"allegations"})
    if not entries:
        return None
    clean = [str(a).strip() for a in entries if isinstance(a, str) and a.strip()]
    return clean[:3] if clean else None


if __name__ == "__main__":
    main()
