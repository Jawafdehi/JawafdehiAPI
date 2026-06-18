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
    content_from_evidence_entry,
    convert_date_tool,
    get_target_cases,
    print_summary,
    setup_logging,
)

logger = logging.getLogger(__name__)

# Source types ordered by usefulness for the description, richest first.
DESCRIPTION_SOURCE_TYPES = (
    "AG_ABHIYOG_PATRA",  # charge sheet — the prosecution claim verbatim
    "CIAA_PRESS_RELEASE",  # the allegation summary + amounts
    "COURT_ORDER",  # verdict — outcome, bench, reasoning
    "COURT_FILING_OTHER",
)

# A court case number like 080-CR-0047 / 081-WO-1234. Case-insensitive to match
# the review gate's detector. The negative lookbehind/ahead anchor the token.
COURT_RE = re.compile(r"(?<![\dA-Za-z])\d{2,3}-[A-Za-z]{1,3}-\d{3,4}(?![\dA-Za-z])")

# Source-budget (characters) fed to the description prompt.
SOURCE_TEXT_BUDGET = 60000
VERDICT_SUMMARY_TRIGGER = 12000
VERDICT_SUMMARY_TARGET = 8000

VERDICT_SUMMARY_SYSTEM_PROMPT = """\
You are a Nepali legal analyst. You are given the full text of a Special Court \
(विशेष अदालत) judgment (फैसला) in a CIAA corruption case. Produce a faithful \
Nepali summary (देवनागरी, government/court register; keep English technical terms \
as-is) that a downstream writer will use to draft the "विशेष अदालतको फैसलाको सार" \
section of a public case record.

Capture ONLY what the judgment states — never infer or invent:
- फैसला मिति (judgment date) and the इजलास / न्यायाधीशहरू (the bench, by name).
- नि.नं. / मुद्दा नं. and the parties (वादी / प्रतिवादीहरू).
- For EACH defendant: the outcome — दोषी (convicted, with कैद/जरिवाना/बिगो असुल) or
  सफाई (acquitted) — and the court's key reasoning for it.
- Any legal principle / नजिर the court established.
- The disputed बिगो the court accepted or rejected, and why.

Be specific (names, दफा, amounts, dates) but concise — aim for about \
%(target)d characters. Output plain Nepali prose/short lists, NOT JSON.
""" % {"target": VERDICT_SUMMARY_TARGET}

EXTRACTION_SYSTEM_PROMPT = """\
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
for each defendant (दोषी / सफाई). Briefly state the court's reasoning and any
legal principle (नजिर) it established.

### घ) पुनरावेदनको सार
Only if the sources or a supreme-court reference show an appeal: the grounds and
legal basis of the appeal and who filed it.

### ङ) सर्वोच्च अदालतको फैसलाको सार
Only if a Supreme Court judgment is in the sources: date, bench, and final
outcome.

### च) नजिरको सार
Only if the judgment establishes a precedent: state the key principle only.

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

TITLE RULES (when asked to regenerate the title):
- Produce a concise, engaging, SEARCHABLE Nepali headline that names the real
  subject of the case — the institution/scheme and/or the principal accused, and
  ideally the बिगो amount or the nature of the offence.
- Vary the construction across cases; do not use a rigid template. Be catchy but
  strictly factual.
- NEVER put a defendant HEADCOUNT in the title. Forbidden: any "<संख्या> जना",
  "समेत X जना", "X प्रतिवादी(माथि/मा)", "तीन/चार… अध्यक्षसहित", or similar count
  of people. This applies even when there are many defendants.
    * Many defendants → name the ONE principal accused (or the institution) and
      use "लगायत" / "सहित" with NO number, e.g.
      "…सचिव संजय शर्मासहित…", NOT "…सचिवसमेत १२ जना…".
    * BAD:  "…पदाधिकारीसमेत १२ जना सबैले सफाई" / "…२४९ प्रतिवादीमाथि…"
      GOOD: "…सामुदायिक वनका पदाधिकारीसहित सबैलाई सफाई" /
            "…तत्कालीन अध्यक्ष <नाम> लगायतमाथि भ्रष्टाचार अभियोग"
- The title MUST end with the special-court case number in parentheses, exactly
  as given to you, e.g. "… (080-CR-0047)".
- Keep it under ~160 characters.

OUTPUT FORMAT — return ONLY a single JSON object, no markdown fences, no prose:
{"title": "नेपाली शीर्षक (080-CR-0047)", "description": "### क) …\\n…"}
When title regeneration is disabled you may omit "title" or set it to null.
"""

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


def _special_court_number(case: dict) -> Optional[str]:
    """Extract special-court case number from court_cases."""
    for ref in case.get("court_cases") or []:
        if isinstance(ref, str) and ref.startswith("special:"):
            return ref.split(":", 1)[1]
    # Fall back to any court number present
    for ref in case.get("court_cases") or []:
        if isinstance(ref, str) and ":" in ref:
            return ref.split(":", 1)[1]
    return None


def _process_case(
    case: dict,
    idx: int,
    total: int,
    dry_run: bool,
    skip_title: bool,
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
    if _has_substantial_description(detail):
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

    court_number = _special_court_number(detail)

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
    title_issue = _validate_title(new_title, court_number) if new_title else None
    if title_issue:
        print(f"  TITLE WARNING: {title_issue}")

    title_has_headcount = bool(new_title and _title_has_headcount(new_title))
    if title_has_headcount:
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
        if (
            not skip_title and new_title and not title_issue and not title_has_headcount
        )
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
            summary = _summarize_verdict(text, invoke_text, usage)
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
        chunk = text[:remaining]
        parts.append(f"[{label}]\n{chunk}")
        remaining -= len(chunk)
    return "\n\n---\n\n".join(parts)


def _summarize_verdict(verdict_text: str, invoke_text, usage) -> Optional[str]:
    """First-pass LLM summary of a long Special Court verdict document."""
    try:
        result = invoke_text(
            system=VERDICT_SUMMARY_SYSTEM_PROMPT,
            content="Summarise this Special Court judgment as instructed.\n\n"
            + verdict_text[:120000],
            tier="premium",
            usage=usage,
            max_tokens=4000,
        )
        return result.strip() if result else None
    except Exception as exc:
        logger.warning("Verdict summarisation failed: %s", exc)
        return None


def _format_bigo(bigo) -> str:
    """Render the bigo for the prompt."""
    try:
        value = int(bigo)
    except (TypeError, ValueError):
        return "(unknown)"
    return f"{value:,}" if value > 0 else "(unknown)"


def _format_list(items) -> str:
    """Format a list of items for the prompt."""
    if not items:
        return "(none provided)"
    return "\n".join(f"- {x}" for x in items)


def _format_entities(entities) -> str:
    """Format entities for the prompt."""
    if not entities:
        return "(none provided)"
    lines = []
    for e in entities:
        if not isinstance(e, dict):
            continue
        name = e.get("display_name") or ""
        etype = e.get("type") or ""
        notes = e.get("notes") or ""
        line = f"- [{etype}] {name}"
        if notes:
            line += f" — {notes}"
        lines.append(line)
    return "\n".join(lines) or "(none provided)"


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
        bigo=_format_bigo(detail.get("bigo")),
        court_cases=", ".join(detail.get("court_cases") or []) or "(none)",
        title_instruction=TITLE_OFF if skip_title else TITLE_ON,
        key_allegations=_format_list(detail.get("key_allegations")),
        timeline=json.dumps(detail.get("timeline") or [], ensure_ascii=False),
        entities=_format_entities(detail.get("entities")),
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


def _validate_title(title: str, court_number: Optional[str]) -> Optional[str]:
    """Validate the regenerated title."""
    nums = {m.group(0).upper() for m in COURT_RE.finditer(title)}
    if not nums:
        return "regenerated title has no court case number"
    if court_number and court_number.upper() not in nums:
        return (
            f"title number(s) {sorted(nums)} do not include the special-court "
            f"number {court_number}"
        )
    if court_number:
        expected = f"({court_number.upper()})"
        if not title.upper().rstrip().endswith(expected):
            return (
                f"title must end with the special-court case number "
                f"in parentheses, e.g. '… {expected}'"
            )
    return None


_HEADCOUNT_RE = re.compile(r"[०-९0-9]+\s*(जना|व्यक्ति|प्रतिवादी)")


def _title_has_headcount(title: str) -> bool:
    """Check if title contains a defendant headcount."""
    return bool(_HEADCOUNT_RE.search(title or ""))


if __name__ == "__main__":
    main()
