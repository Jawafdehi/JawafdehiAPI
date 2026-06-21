#!/usr/bin/env python
"""Regenerate CIAA Special Court case TITLES from already-enriched fields (DB-free).

Standalone, no-source-fetch counterpart to enrich_description's title pass: it
rewrites ``Case.title`` from the case's existing description / key allegations /
entities / बिगो, WITHOUT downloading or converting any source documents. A case
that already has a good description has no other path to a fixed title, because
enrich_description skips its (expensive, source-fetching) description+title pass
once a substantial description exists — this script is that path.

Shares the TITLE RULES, the court-number validator and the headcount guard with
enrich_description via ``casework.common``, so titles stay consistent across both.

Like the sibling enrichers, this reads cases and PATCHes the title entirely over
the Jawafdehi HTTP API and never touches the database. Default behaviour WRITES;
pass ``--dry-run`` to preview. A title that already passes validation (ends with
its special-court number in parentheses, carries no headcount) is skipped unless
``--force``.

Usage:
    python casework/enrich_title.py --dry-run
    python casework/enrich_title.py --slug case-0123
    python casework/enrich_title.py --priority --limit 20 --dry-run
    python casework/enrich_title.py --court-case 081-CR-0095 --force
"""

import argparse
import logging
import os
import sys

import requests

# Ensure the api dir is in sys.path so imports work when run as a file
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from casework.common import (
    TITLE_RULES,
    CaseworkApi,
    add_common_args,
    bootstrap,
    format_bigo,
    format_entities,
    format_list,
    get_target_cases,
    parse_title,
    print_summary,
    setup_logging,
    special_court_number,
    title_has_headcount,
    validate_title,
)

logger = logging.getLogger(__name__)

# A snippet of the existing description is plenty of context for a headline and
# keeps this single, no-source-fetch call token-frugal.
DESCRIPTION_SNIPPET_BUDGET = 2000

# get_target_cases skips a case when summary[skip_field] is truthy, but
# "title already valid" is not a case field — so select on a key cases never
# carry (nothing is skipped at selection) and do the already-valid idempotency
# check per-case in _process_case instead.
_NO_SKIP_FIELD = "__title_always_consider__"

SYSTEM_PROMPT = (
    """\
You are a Nepali legal editor writing the public headline (title) for a case in \
Jawafdehi, a civic accountability archive of Nepal's anti-corruption cases. The \
case was investigated by the CIAA (अख्तियार दुरुपयोग अनुसन्धान आयोग) and tried at \
the Special Court (विशेष अदालत).

You are given the case's current title, its बिगो amount, key allegations, named \
entities, and a snippet of the already-written case description. From these, write \
ONE Nepali (देवनागरी) headline following the rules below.

"""
    + TITLE_RULES
    + """

OUTPUT FORMAT — return ONLY a single JSON object, no markdown fences, no prose:
{"title": "नेपाली शीर्षक (080-CR-0047)"}
"""
)

USER_PROMPT = """\
Write the Jawafdehi case title for the following CIAA Special Court case.

Current title: {current_title}
Special-court case number (MUST end the regenerated title): {court_number}
Bigo (बिगो), NPR: {bigo}

KEY ALLEGATIONS:
{key_allegations}

NAMED ENTITIES (accused / related / location):
{entities}

DESCRIPTION (snippet — the factual basis for the headline):
{description}

Return ONLY the JSON object described in the system prompt.
"""


def main():
    ap = argparse.ArgumentParser(
        description=(
            "Regenerate CIAA Special Court case titles via LLM "
            "(DB-free, no source fetch)."
        ),
        epilog="Reads and writes entirely over HTTP via JAWAFDEHI_API_TOKEN.",
    )
    add_common_args(ap)
    args = ap.parse_args()

    setup_logging(args.verbose)

    try:
        bootstrap(args.provider, args.model)
    except Exception as exc:
        print(f"Bootstrap failed: {exc}", file=sys.stderr)
        sys.exit(1)

    from llm.invoke import invoke_text
    from llm.usage import UsageAccumulator, render_usage_table

    try:
        api = CaseworkApi(base_url=args.api_base_url, token=args.api_token)
    except RuntimeError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        sys.exit(1)

    usage = UsageAccumulator()

    cases = list(get_target_cases(api, args, skip_field=_NO_SKIP_FIELD))
    total = len(cases)
    if total == 0:
        print("No CIAA draft cases to process.", file=sys.stderr)
        sys.exit(0)

    print(f"Found {total} CIAA draft case(s) to process.")
    if args.force:
        print("  --force: regenerating even titles that already pass validation")
    if args.fiscal_year:
        print(f"  Fiscal year filter: {args.fiscal_year}")
    if args.dry_run:
        print("  [DRY RUN] No changes will be saved.")

    stats = {
        "cases_processed": 0,
        "cases_enriched": 0,
        "cases_skipped": 0,
        "cases_llm_error": 0,
        "cases_already_valid": 0,
    }

    for idx, case in enumerate(cases, 1):
        try:
            _process_case(
                case=case,
                idx=idx,
                total=total,
                dry_run=args.dry_run,
                force=args.force,
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

    print_summary(stats, args.dry_run, "Title generation")

    if usage.calls > 0:
        print()
        print(render_usage_table(usage.as_dict()["by_provider"], title="title usage"))


def _title_is_valid(title, court_number) -> bool:
    """A title is 'already valid' when it ends with its special-court number in
    parens AND carries no defendant headcount — the contract the review gate
    enforces. Such titles are skipped unless --force."""
    return (
        bool(title)
        and bool(court_number)
        and validate_title(title, court_number) is None
        and not title_has_headcount(title)
    )


def _process_case(case, idx, total, dry_run, force, api, usage, invoke_text, stats):
    """Process one case: fetch detail, regenerate the title, validate, PATCH."""
    stats["cases_processed"] += 1
    case_id = case.get("case_id", "?")
    current_title = case.get("title", "")
    print(f"\n[{idx}/{total}] {case_id} — {current_title[:80]}")

    try:
        detail = api.get_case(case.get("slug") or case_id)
    except requests.HTTPError:
        detail = case
        print("  (using summary instead of detail)")

    current_title = detail.get("title", current_title)
    court_number = special_court_number(detail)

    if not force and _title_is_valid(current_title, court_number):
        stats["cases_processed"] -= 1
        stats["cases_already_valid"] += 1
        print("  Current title already valid — skipping (use --force)")
        return

    try:
        new_title = _generate_title(detail, court_number, invoke_text, usage)
    except Exception as exc:
        stats["cases_llm_error"] += 1
        print(f"  LLM generation failed: {exc}")
        if logger.level == logging.DEBUG:
            import traceback

            traceback.print_exc()
        return

    if not new_title:
        stats["cases_skipped"] += 1
        print("  LLM returned no title — skipping")
        return

    print(f"  TITLE: {new_title}")

    title_issue = validate_title(new_title, court_number)
    if title_issue:
        stats["cases_skipped"] += 1
        print(f"  TITLE REJECTED ({title_issue}) — not writing")
        return
    if title_has_headcount(new_title):
        stats["cases_skipped"] += 1
        print("  TITLE REJECTED (contains a defendant headcount) — not writing")
        return

    if dry_run:
        print("  [DRY RUN] Would PATCH but --dry-run is set")
        return

    try:
        api.patch_field(detail.get("slug") or case.get("slug"), "title", new_title)
        stats["cases_enriched"] += 1
        print(f"  [UPDATED] {case_id}")
    except Exception as exc:
        stats["cases_llm_error"] += 1
        print(f"  Failed to PATCH: {exc}")


def _generate_title(detail, court_number, invoke_text, usage):
    """Build the title prompt from the case's already-enriched fields and call
    the LLM. No source documents are fetched."""
    description = (detail.get("description") or "").strip()
    prompt = USER_PROMPT.format(
        current_title=detail.get("title", ""),
        court_number=court_number or "(unknown)",
        bigo=format_bigo(detail.get("bigo")),
        key_allegations=format_list(detail.get("key_allegations")),
        entities=format_entities(detail.get("entities")),
        description=description[:DESCRIPTION_SNIPPET_BUDGET] or "(none)",
    )
    response_text = invoke_text(
        system=SYSTEM_PROMPT,
        content=prompt,
        tier="premium",
        usage=usage,
        max_tokens=1000,
    )
    return parse_title(response_text)


if __name__ == "__main__":
    main()
