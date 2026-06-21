#!/usr/bin/env python
"""Enrich CIAA Special Court case descriptions (DB-free script using the llm package).

Standalone script to generate a case's narrative DESCRIPTION (and optionally
regenerate TITLE) from CIAA source documents using LLM extraction, fully over
the Jawafdehi HTTP API. Never touches the database.

Phase A.4 of the CIAA Case Enrichment pipeline. Populates ``Case.description``
(Markdown) with the case summary — the अभियोगदावी / बयान / फैसला structure of
a corruption case — and optionally regenerates ``Case.title`` into a concise,
searchable headline ending in the special-court case number.
See https://github.com/Jawafdehi/JawafdehiAPI/issues/199.

Usage:
    python casework/enrich_description.py --dry-run
    python casework/enrich_description.py --slug case-0123
    python casework/enrich_description.py --limit 10 --verbose
    python casework/enrich_description.py --fiscal-year 080 --dry-run
    python casework/enrich_description.py --force --skip-title
"""

import argparse
import json
import logging
import os
import sys
import urllib.parse
from typing import Optional

import requests

# Ensure the api dir is in sys.path so imports work when run as a file
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from casework.common import (
    TITLE_RULES,
    VERDICT_SUMMARY_TARGET,
    VERDICT_SUMMARY_TRIGGER,
    CaseworkApi,
    add_common_args,
    balanced_object,
    bootstrap,
    clamp,
    content_from_evidence_entry,
    convert_date_tool,
    env_int,
    format_bigo,
    format_entities,
    format_list,
    get_target_cases,
    print_summary,
    setup_logging,
    special_court_number,
    summarize_verdict,
    title_has_headcount,
    validate_title,
)

logger = logging.getLogger(__name__)

# Source types ordered by usefulness for the description, richest first.
DESCRIPTION_SOURCE_TYPES = (
    "AG_ABHIYOG_PATRA",  # charge sheet — the prosecution claim verbatim
    "CIAA_PRESS_RELEASE",  # the allegation summary + amounts
    "COURT_ORDER",  # verdict — outcome, bench, reasoning
    "COURT_FILING_OTHER",
)

# Source-budget (characters) fed to the description prompt.
# Env-tunable so the runner can widen them for big-context models (claude 1M):
# raise the budget + verdict trigger to feed full court orders instead of an
# LLM-summarised digest. The verdict-summariser + its VERDICT_SUMMARY_* config
# live in casework.common (shared with enrich_timeline).
SOURCE_TEXT_BUDGET = env_int("CASEWORK_SOURCE_TEXT_BUDGET", 60000)

EXTRACTION_SYSTEM_PROMPT = (
    """\
You are a Nepali legal analyst writing the public case summary (description) for \
Jawafdehi, a civic accountability archive of Nepal's anti-corruption cases. The \
case was investigated by the CIAA (अख्तियार दुरुपयोग अनुसन्धान आयोग) and tried at \
the Special Court (विशेष अदालत).

You will be given the case's key allegations, factual timeline, bigo (बिगो) \
amount, named entities, and the full text of the source documents (CIAA press \
release, charge sheet/अभियोगपत्र, and Special Court verdict/फैसला). Write a \
faithful, well-structured Markdown description.

LANGUAGE: Write in formal Nepali (देवनागरी), matching the register of the court \
and government source documents. Keep technical, proper, and forensic terms in \
their original form (English where the source uses English — e.g. "CR", \
"Common Authorship", company names, "forensic") rather than forcing a translation.

STRUCTURE — use these Markdown sections, in this order, but ONLY include a \
section when the sources actually support it (omit sections with no grounding; \
never invent content to fill one):

### क) अभियोगदावीको सार
The prosecution's claim: the core facts, how they breach the law (cite the
ऐन/दफा when the sources state them), the evidence the CIAA relied on, the persons
involved, the बिगो, and the punishment sought. When the CIAA lays out distinct
grounds/findings, present them as a numbered list (**१.** … **२.** …). When there
are multiple defendants with per-person amounts or demands, present them as a
Markdown table (प्रतिवादी | भूमिका/अभियोग | बिगो | मागदावी).

### ख) प्रतिवादीको बयानको सार
For EACH defendant, summarise their statement (बयान) before the authorised
authority or the court in at least ~100 words: whether they admit (स्वीकार) or
deny (इन्कार) the allegation and their reasoning. With several defendants, use a
Markdown table (क्र.सं | प्रतिवादी | भूमिका | बयानको सार).

### ग) विशेष अदालतको फैसलाको सार
The verdict: the judgment date, the bench (इजलास / न्यायाधीशहरू), and the outcome
for each defendant (दोषी / सफाई), with the बिगो/sentence and the court's key
reasoning. Do NOT include procedural or registry orders — the appeal म्याद
(e.g. "३५ दिनभित्र पुनरावेदन गर्न"), धरौटी सदर/फिर्ता, लगत कायम, and similar routine
"अन्य आदेश" are court-procedure, not the substantive फैसला; leave them out. A
विशेष अदालत ruling does NOT set precedent, so do not call its reasoning a नजिर here.

### घ) पुनरावेदनको सार
Only if the sources or a supreme-court reference show an appeal was ACTUALLY
filed: the grounds and legal basis of the appeal and who filed it. The routine
appeal म्याद granted in the verdict (e.g. "३५ दिनभित्र पुनरावेदन गर्न जाने") is NOT
an appeal — do not emit this section for it; OMIT the section entirely unless an
appeal was really lodged.

### ङ) सर्वोच्च अदालतको फैसलाको सार
Only if a Supreme Court judgment is in the sources: date, bench, and final
outcome.

### च) नजिरको सार
Include ONLY when a सर्वोच्च अदालत (Supreme Court) judgment in the sources
establishes a legal principle — ideally one published in the Nepal Kanoon Patrika
(नेपाल कानून पत्रिका). A विशेष अदालत (Special Court) decision is NEVER a precedent;
if no qualifying Supreme Court principle is in the sources, OMIT this section
entirely. State only the key principle.

QUALITY RULES:
- Ground every sentence in the provided sources/case data. Do NOT fabricate
  names, amounts, section numbers, dates, benches, or outcomes. If the verdict is
  not in the sources, write section ग only to the extent the timeline/NGM data
  supports (e.g. "मिति … मा फैसला भएको") and omit unknown specifics.
- Prefer specifics from the documents (exact बिगो, दफा, र.नं./नि.नं., dates,
  named officials) over vague phrasing.
- Use the बिगो figure provided in the case data as the headline amount.
- This is an official public record drawn from government/court documents; do not
  soften, editorialise, or add commentary. Neutral, factual tone only.

"""
    + TITLE_RULES
    + """

OUTPUT FORMAT — return ONLY a single JSON object, no markdown fences, no prose:
{"title": "नेपाली शीर्षक (080-CR-0047)", "description": "### क) …\\n…"}
When title regeneration is disabled you may omit "title" or set it to null.
"""
)

EXTRACTION_USER_PROMPT = """\
Write the Jawafdehi case description for the following CIAA Special Court case.

Current title: {case_title}
Special-court case number (MUST end the regenerated title): {court_number}
Bigo (बिगो), NPR: {bigo}
Court case references: {court_cases}

{title_instruction}

KEY ALLEGATIONS (already curated for this case):
{key_allegations}

FACTUAL TIMELINE (already curated; dates are reliable — use for section ग etc.):
{timeline}

NAMED ENTITIES (accused / related / location):
{entities}

{ngm_section}

SOURCE DOCUMENTS (press release, charge sheet, verdict — the factual basis for
the description; quote specifics from here):

{source_text}

Return ONLY the JSON object described in the system prompt.
"""

TITLE_ON = (
    "Regenerate the title following the TITLE RULES, ending in the case number "
    'above. Return it in the JSON "title" field.'
)
TITLE_OFF = (
    'Do NOT regenerate the title; set "title" to null in the JSON. Only write '
    "the description."
)


def main():
    """Main entry point."""
    ap = argparse.ArgumentParser(
        description="Generate CIAA Special Court case descriptions via LLM (DB-free).",
        epilog="Reads cases and writes results entirely over HTTP via JAWAFDEHI_API_TOKEN.",
    )
    add_common_args(ap)
    ap.add_argument(
        "--skip-title",
        action="store_true",
        help="Do not regenerate the title; only write the description",
    )

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
    from llm.invoke import invoke_text, invoke_with_tools
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
    cases = list(get_target_cases(api, args, skip_field="description"))

    total = len(cases)
    if total == 0:
        print("No CIAA draft cases to process.", file=sys.stderr)
        sys.exit(0)

    print(f"Found {total} CIAA draft case(s) to process.")
    if args.force:
        print("  --force: re-generating even for populated cases")
    if args.fiscal_year:
        print(f"  Fiscal year filter: {args.fiscal_year}")
    if args.skip_title:
        print("  --skip-title: will not regenerate titles")
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
                skip_title=args.skip_title,
                force=args.force,
                api=api,
                usage=usage,
                invoke_text=invoke_text,
                invoke_with_tools=invoke_with_tools,
                stats=stats,
            )
        except Exception as exc:
            stats["cases_llm_error"] += 1
            print(f"Unhandled error processing case: {exc}", file=sys.stderr)
            if args.verbose:
                import traceback

                traceback.print_exc()

    # Print summary
    print_summary(stats, args.dry_run, "Description generation")

    # Print usage table
    if usage.calls > 0:
        print()
        print(
            render_usage_table(
                usage.as_dict()["by_provider"], title="description usage"
            )
        )


def _has_substantial_description(case: dict) -> bool:
    """Check if case already has a substantial description."""
    return len((case.get("description") or "").strip()) >= 600


def _process_case(
    case: dict,
    idx: int,
    total: int,
    dry_run: bool,
    skip_title: bool,
    force: bool,
    api: CaseworkApi,
    usage,
    invoke_text,
    invoke_with_tools,
    stats: dict,
):
    """Process a single case: fetch detail, generate description, PATCH or preview."""
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

    # Idempotency: skip cases with substantial descriptions unless --force
    if not force and _has_substantial_description(detail):
        stats["cases_processed"] -= 1
        stats["cases_already_populated"] += 1
        print("  Already has a substantial description — skipping (use --force)")
        return

    # Get source content
    source_parts = _get_source_parts(detail)
    ngm_data = _get_ngm_data(detail, api)

    if not source_parts:
        stats["cases_no_content"] += 1
        print("  No source content found — skipping")
        return

    print(
        f"  Sources: {', '.join(f'{stype}({len(text)})' for stype, text in source_parts)}"
    )
    if ngm_data:
        print("  NGM data: present")
    else:
        print("  NGM data: none")

    # Assemble and summarize source text
    try:
        source_text = _assemble_source_text(
            source_parts=source_parts,
            invoke_text=invoke_text,
            usage=usage,
        )
    except Exception as exc:
        stats["cases_llm_error"] += 1
        print(f"  Source assembly failed: {exc}")
        if logger.level == logging.DEBUG:
            import traceback

            traceback.print_exc()
        return

    court_number = special_court_number(detail)

    # Generate description via LLM
    try:
        result = _generate_description(
            detail=detail,
            court_number=court_number,
            source_text=source_text,
            ngm_data=ngm_data,
            skip_title=skip_title,
            invoke_with_tools=invoke_with_tools,
            usage=usage,
        )
    except Exception as exc:
        stats["cases_llm_error"] += 1
        print(f"  LLM generation failed: {exc}")
        if logger.level == logging.DEBUG:
            import traceback

            traceback.print_exc()
        return

    if not result or not result.get("description"):
        stats["cases_skipped"] += 1
        print("  LLM returned no description — skipping")
        return

    new_title = result.get("title")
    description = result["description"]

    # Always print the (regenerated or current) title
    print(f"  TITLE: {new_title or title}")
    print(f"  DESCRIPTION: {len(description)} chars")
    for line in description.splitlines()[:5]:
        print(f"    | {line[:70]}")
    if len(description.splitlines()) > 5:
        print(f"    | ... ({len(description.splitlines())} lines total)")

    # Validate title
    title_issue = validate_title(new_title, court_number) if new_title else None
    if title_issue:
        print(f"  TITLE WARNING: {title_issue}")

    has_headcount = bool(new_title and title_has_headcount(new_title))
    if has_headcount:
        print(
            "  TITLE WARNING: contains a defendant headcount "
            "(e.g. 'X जना' / 'X प्रतिवादी') — title NOT written."
        )

    if dry_run:
        print("  [DRY RUN] Would PATCH but --dry-run is set")
        return

    # Decide what to PATCH
    patch_title = (
        new_title
        if (not skip_title and new_title and not title_issue and not has_headcount)
        else None
    )

    # PATCH the description (and optionally the title)
    try:
        case_slug = detail.get("slug") or case.get("slug")
        if patch_title:
            api.patch_field(case_slug, "title", patch_title)
        api.patch_field(case_slug, "description", description)
        stats["cases_enriched"] += 1
        print(f"  [UPDATED] {case_id}")
    except Exception as exc:
        stats["cases_llm_error"] += 1
        print(f"  Failed to PATCH: {exc}")


def _get_source_parts(case: dict) -> list[tuple[str, str]]:
    """Return [(source_type, text)] for description-relevant sources, in priority order."""
    evidence = case.get("evidence") or []
    if not evidence:
        return []

    by_type: dict[str, list[dict]] = {}
    for entry in evidence:
        if not isinstance(entry, dict):
            continue
        source = entry.get("source")
        if not isinstance(source, dict):
            continue
        by_type.setdefault(source.get("source_type"), []).append(entry)

    parts: list[tuple[str, str]] = []
    for stype in DESCRIPTION_SOURCE_TYPES:
        for entry in by_type.get(stype, []):
            text = content_from_evidence_entry(entry)
            if text:
                parts.append((stype, text))
    return parts


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
    """Format the NGM API payload as a prompt section."""
    if not ngm_data:
        return ""

    lines = ["NGM STRUCTURED COURT DATA (ground-truth):", ""]
    if ngm_data.get("registration_date_ad"):
        lines.append(f"- Case registration (AD): {ngm_data['registration_date_ad']}")
    if ngm_data.get("case_status"):
        lines.append(f"- Case status: {ngm_data['case_status']}")
    if ngm_data.get("verdict_date_ad"):
        lines.append(f"- Verdict date (AD): {ngm_data['verdict_date_ad']}")
    if ngm_data.get("verdict_judge"):
        lines.append(f"- Verdict bench: {ngm_data['verdict_judge']}")
    return "\n".join(lines) + "\n"


def _assemble_source_text(
    source_parts: list[tuple[str, str]],
    invoke_text,
    usage,
) -> str:
    """Build source-document block within SOURCE_TEXT_BUDGET.

    Long COURT_ORDER verdicts are summarized in a first LLM pass; charge sheet
    and press release pass through whole. Sections added in priority order until
    budget is spent.
    """
    prepared: list[tuple[str, str]] = []
    for stype, text in source_parts:
        if stype == "COURT_ORDER" and len(text) > VERDICT_SUMMARY_TRIGGER:
            summary = summarize_verdict(text, invoke_text, usage)
            if summary:
                logger.info(
                    "Verdict summarised: %d -> %d chars", len(text), len(summary)
                )
                prepared.append(("COURT_ORDER (फैसला सारांश)", summary))
                continue
            # Summary failed — fall back to truncated head
            prepared.append((stype, text[:VERDICT_SUMMARY_TARGET]))
            continue
        prepared.append((stype, text))

    parts: list[str] = []
    remaining = SOURCE_TEXT_BUDGET
    for label, text in prepared:
        if remaining <= 0:
            logger.warning("Budget spent; dropped a %s source", label)
            break
        chunk = clamp(text, remaining, label)
        parts.append(f"[{label}]\n{chunk}")
        remaining -= len(chunk)
    return "\n\n---\n\n".join(parts)


def _generate_description(
    detail: dict,
    court_number: Optional[str],
    source_text: str,
    ngm_data: Optional[dict],
    skip_title: bool,
    invoke_with_tools,
    usage,
) -> Optional[dict]:
    """Call LLM to generate description (and optionally title)."""
    prompt = EXTRACTION_USER_PROMPT.format(
        case_title=detail.get("title", ""),
        court_number=court_number or "(unknown)",
        bigo=format_bigo(detail.get("bigo")),
        court_cases=", ".join(detail.get("court_cases") or []) or "(none)",
        title_instruction=TITLE_OFF if skip_title else TITLE_ON,
        key_allegations=format_list(detail.get("key_allegations")),
        timeline=json.dumps(detail.get("timeline") or [], ensure_ascii=False),
        entities=format_entities(detail.get("entities")),
        ngm_section=_format_ngm_section(ngm_data),
        source_text=source_text,
    )

    response_text = invoke_with_tools(
        system=EXTRACTION_SYSTEM_PROMPT,
        content=prompt,
        tools=[convert_date_tool()],
        tier="premium",
        usage=usage,
        max_tokens=8000,
        max_iterations=30,
    )

    return _parse_response(response_text)


def _parse_response(response_text: str) -> Optional[dict]:
    """Parse the {title, description} JSON object from the LLM response.

    Tries EVERY '{' position and returns the first balanced block that parses
    to a dict carrying a 'description'.
    """
    text = (response_text or "").strip()
    if "```" in text:
        start = text.find("```")
        nl = text.find("\n", start)
        end = text.find("```", nl + 1) if nl != -1 else -1
        if nl != -1 and end != -1:
            text = text[nl + 1 : end].strip()

    for obj_start in range(len(text)):
        if text[obj_start] != "{":
            continue
        block = balanced_object(text, obj_start)
        if block is None:
            continue
        try:
            obj = json.loads(block)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "description" in obj:
            desc = (obj.get("description") or "").strip()
            title = obj.get("title")
            title = title.strip() if isinstance(title, str) else None
            return {"description": desc, "title": title or None}
    logger.warning("No JSON object with a description found in LLM response")
    return None


if __name__ == "__main__":
    main()
