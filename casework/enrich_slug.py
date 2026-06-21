#!/usr/bin/env python
"""Propose clean, readable URL slugs for CIAA Special Court cases via an LLM (DB-free).

For each DRAFT CIAA Special Court case this asks the LLM, with short instructions,
for a single human-readable English slug that MUST include the special court case
number (e.g. ``sunil-poudel-land-fraud-080-cr-0047``). The slug is lightly
validated with the same ``validate_slug`` rule the API enforces (lowercase
letters/numbers/hyphens, starts with a letter, max 50 chars).

Default behaviour WRITES A PROPOSALS FILE (``--output``, default
``slug-enrichment-proposals.json``) — a list of records each carrying the current
slug, the proposed slug, and whether it is valid — for a human to review and patch
manually afterwards. Pass ``--apply`` to PATCH valid proposals directly (the API
only allows a slug change while the case is in DRAFT).

Like the sibling enrichers, this reads cases entirely over the Jawafdehi HTTP API
and never touches the database.

Usage:
    python casework/enrich_slug.py --priority --dry-run
    python casework/enrich_slug.py --priority --output slugs.json
    python casework/enrich_slug.py --slug case-0123 --apply
    python casework/enrich_slug.py --court-case 081-CR-0095 --apply
"""

import argparse
import json
import logging
import os
import sys

import requests

# Ensure the api dir is in sys.path so imports work when run as a file
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from casework.common import (
    CaseworkApi,
    add_common_args,
    balanced_object,
    bootstrap,
    format_bigo,
    format_entities,
    get_target_cases,
    print_summary,
    setup_logging,
    special_court_number,
)

logger = logging.getLogger(__name__)

DEFAULT_OUTPUT = "slug-enrichment-proposals.json"

SYSTEM_PROMPT = """\
You write short, readable URL slugs (in English) for cases in Jawafdehi, a civic \
archive of Nepal's anti-corruption cases.

Rules:
- Use only lowercase letters, numbers and hyphens; start with a letter; max 50 characters.
- The slug MUST include the special court case number, given to you as a hyphenated
  token (e.g. 080-cr-0047).
- Use the main accused's name and the nature of the offence; transliterate Nepali
  names to plain English (no Devanagari, no accents).
- Keep it concise and readable.

Return ONLY a JSON object, no prose: {"slug": "sunil-poudel-land-fraud-080-cr-0047"}
"""

USER_PROMPT = """\
Write the URL slug for this CIAA Special Court case.

Title: {title}
Special court case number (MUST appear in the slug): {court_number}
Bigo (बिगो), NPR: {bigo}

Named entities (accused / related / location):
{entities}

Return ONLY the JSON object described in the system prompt.
"""


def parse_slug(response_text):
    """Pull the slug out of an LLM response.

    Prefers a ``{"slug": "..."}`` JSON object (scanning every ``{`` so leading
    prose with braces doesn't abort the parse); falls back to a bare single-line
    token. Returns the slug string (stripped, lowercased) or None.
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
        if isinstance(obj, dict) and isinstance(obj.get("slug"), str):
            slug = obj["slug"].strip().lower()
            if slug:
                return slug

    # Bare single-line token fallback (a frugal model may emit just the slug).
    if text and "{" not in text and "\n" not in text and " " not in text:
        return text.strip().lower()
    return None


def _validate_slug(slug):
    """Return None if `slug` passes the API's validate_slug rule, else the error
    message. Imported lazily so Django is configured (by bootstrap/pytest) first."""
    from django.core.exceptions import ValidationError

    from cases.validators import validate_slug

    try:
        validate_slug(slug)
    except ValidationError as exc:
        return "; ".join(exc.messages)
    return None


def _court_suffix(court_number):
    """The slugified court number used both in the prompt and the idempotency
    check, e.g. '080-CR-0047' -> '080-cr-0047'."""
    from django.utils.text import slugify

    return slugify(court_number or "")


def _build_parser():
    ap = argparse.ArgumentParser(
        description=(
            "Propose readable URL slugs (court case id included) for CIAA Special "
            "Court cases via an LLM; writes a proposals file by default."
        ),
        epilog="Reads over HTTP via JAWAFDEHI_API_TOKEN; --apply PATCHes valid slugs.",
    )
    add_common_args(ap)
    ap.add_argument(
        "--output",
        default=DEFAULT_OUTPUT,
        help=f"Proposals file to write (default {DEFAULT_OUTPUT}).",
    )
    ap.add_argument(
        "--apply",
        action="store_true",
        help="PATCH valid proposals directly (DRAFT-only) in addition to writing the file.",
    )
    # Slugs are short transliterations — a real Opus via the local `claude -p`
    # subscription harness writes cleaner names than the opus->deepseek proxy.
    ap.set_defaults(provider="claude_cli")
    return ap


def main():
    args = _build_parser().parse_args()

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

    # skip_field=None: select every matching case; the "slug already looks
    # enriched" idempotency check happens per-case in _process_case.
    cases = list(get_target_cases(api, args, skip_field=None))
    total = len(cases)
    if total == 0:
        print("No CIAA draft cases to process.", file=sys.stderr)
        sys.exit(0)

    print(f"Found {total} CIAA draft case(s) to process.")
    if args.apply:
        print("  --apply: valid proposals will be PATCHed (DRAFT-only)")
    if args.dry_run:
        print("  [DRY RUN] No changes will be saved.")

    stats = {
        "cases_processed": 0,
        "slugs_proposed": 0,
        "slugs_invalid": 0,
        "slugs_applied": 0,
        "cases_skipped": 0,
        "cases_llm_error": 0,
    }
    proposals = []

    for idx, case in enumerate(cases, 1):
        try:
            _process_case(
                case=case,
                idx=idx,
                total=total,
                args=args,
                api=api,
                usage=usage,
                invoke_text=invoke_text,
                stats=stats,
                proposals=proposals,
            )
        except Exception as exc:
            stats["cases_llm_error"] += 1
            print(f"Unhandled error processing case: {exc}", file=sys.stderr)
            if args.verbose:
                import traceback

                traceback.print_exc()

    _write_proposals(args.output, proposals)
    print(f"\nWrote {len(proposals)} proposal(s) to {args.output}")

    print_summary(stats, args.dry_run, "Slug enrichment")

    if usage.calls > 0:
        print()
        print(render_usage_table(usage.as_dict()["by_provider"], title="slug usage"))


def _process_case(case, idx, total, args, api, usage, invoke_text, stats, proposals):
    """Process one case: regenerate the slug, validate, record a proposal, apply."""
    stats["cases_processed"] += 1
    case_id = case.get("case_id", "?")
    slug_current = case.get("slug") or ""
    print(f"\n[{idx}/{total}] {case_id} — {slug_current}")

    try:
        detail = api.get_case(slug_current or case_id)
    except requests.HTTPError:
        detail = case
        print("  (using summary instead of detail)")

    slug_current = detail.get("slug") or slug_current
    court_number = special_court_number(detail)
    if not court_number:
        stats["cases_skipped"] += 1
        print("  No special court number — skipping")
        return

    court_suffix = _court_suffix(court_number)
    if not args.force and slug_current.endswith(court_suffix):
        stats["cases_processed"] -= 1
        stats["cases_skipped"] += 1
        print("  Slug already ends with the court number — skipping (use --force)")
        return

    try:
        slug_proposed = _generate_slug(detail, court_suffix, invoke_text, usage)
    except Exception as exc:
        stats["cases_llm_error"] += 1
        print(f"  LLM generation failed: {exc}")
        if logger.level == logging.DEBUG:
            import traceback

            traceback.print_exc()
        return

    if not slug_proposed:
        stats["cases_skipped"] += 1
        print("  LLM returned no slug — skipping")
        return

    print(f"  SLUG: {slug_proposed}")

    error = _validate_slug(slug_proposed)
    valid = error is None
    if not valid:
        stats["slugs_invalid"] += 1
        print(f"  INVALID ({error}) — recorded but will not be applied")
    else:
        stats["slugs_proposed"] += 1

    proposals.append(
        {
            "case_id": case_id,
            "slug_current": slug_current,
            "slug_proposed": slug_proposed,
            "valid": valid,
            "validation_error": error,
        }
    )

    if args.apply and valid and not args.dry_run:
        try:
            api.patch_field(slug_current, "slug", slug_proposed)
            stats["slugs_applied"] += 1
            print(f"  [UPDATED] {case_id}")
        except Exception as exc:
            stats["cases_llm_error"] += 1
            print(f"  Failed to PATCH: {exc}")


def _generate_slug(detail, court_suffix, invoke_text, usage):
    """Build the slug prompt from the case fields and call the LLM."""
    prompt = USER_PROMPT.format(
        title=detail.get("title", ""),
        court_number=court_suffix or "(unknown)",
        bigo=format_bigo(detail.get("bigo")),
        entities=format_entities(detail.get("entities")),
    )
    response_text = invoke_text(
        system=SYSTEM_PROMPT,
        content=prompt,
        tier="premium",
        usage=usage,
        max_tokens=300,
    )
    return parse_slug(response_text)


def _write_proposals(path, proposals):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(proposals, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


if __name__ == "__main__":
    main()
