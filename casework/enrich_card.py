#!/usr/bin/env python
"""Enrich a case's listing-card fields — TITLE and SHORT_DESCRIPTION — in one
DB-free pass over the Jawafdehi HTTP API (no source documents fetched).

Both fields derive from the case's ALREADY-enriched `description` (falling back to
`key_allegations`), so this is a cheap, single LLM call that produces both:

  * title           — a concise, searchable Nepali headline that MUST end with the
                      special-court case number in parens, with no defendant
                      headcount. Regenerated only when the current title fails that
                      format contract (or with --force).
  * short_description — the one-line card teaser the public list renders verbatim
                      (CaseListSerializer ships it with no title/description
                      fallback, so a blank one means an empty card). Regenerated
                      when blank/placeholder per the cheap adequacy judge (or
                      --force).

Combining them is deliberate: same source (the existing description) and one LLM
call yields both fields.

Usage:
    python casework/enrich_card.py --dry-run
    python casework/enrich_card.py --slug case-0123
    python casework/enrich_card.py --priority --force
    python casework/enrich_card.py --only title --dry-run
"""

import argparse
import json
import logging
import os
import re
import sys
from typing import Optional

# Ensure the api dir is in sys.path so imports work when run as a file
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from casework.common import (
    CaseworkApi,
    add_common_args,
    balanced_object,
    bootstrap,
    get_target_cases,
    judge_description_adequacy,
    print_summary,
    setup_logging,
)

logger = logging.getLogger(__name__)

# A court case number like 080-CR-0047 / 081-WO-1234 (case-insensitive, anchored).
COURT_RE = re.compile(r"(?<![\dA-Za-z])\d{2,3}-[A-Za-z]{1,3}-\d{3,4}(?![\dA-Za-z])")
# Defendant-headcount the title must avoid (digit + जना/व्यक्ति/प्रतिवादी). The
# court number itself won't match — it has no such trailing noun.
HEADCOUNT_RE = re.compile(r"[०-९0-9]+\s*(जना|व्यक्ति|प्रतिवादी)")

DESCRIPTION_SNIPPET_BUDGET = 3000
MAX_SHORT_DESCRIPTION_CHARS = 320

SYSTEM_PROMPT = """\
You are a Nepali editor writing the public listing-card fields for a case in \
Jawafdehi, a civic accountability archive of Nepal's anti-corruption cases \
investigated by the CIAA and tried at the Special Court (विशेष अदालत). You are \
given the case's current title, bigo (बिगो) amount, key allegations, and a snippet \
of its full description. Produce up to two fields in formal Nepali (देवनागरी; keep \
English technical/proper terms — "CR", company names — as-is).

TITLE (when requested):
- A concise, engaging, SEARCHABLE headline naming the real subject — the
  institution/scheme and/or the principal accused, ideally the बिगो amount or the
  nature of the offence. Vary construction across cases; be catchy but strictly
  factual, grounded only in the provided data.
- NEVER put a defendant HEADCOUNT in the title (no "<संख्या> जना", "X प्रतिवादी",
  "समेत X जना", etc.). Many defendants → name the ONE principal accused/institution
  with "लगायत"/"सहित" and NO number.
- The title MUST end with the special-court case number in parentheses, exactly as
  given, e.g. "… (080-CR-0047)". Keep it under ~160 characters.

SHORT_DESCRIPTION (when requested):
- A SINGLE punchy teaser, 1–2 sentences, ideally under 200 characters — the essence
  (who/what/how much) at a glance. Plain prose: no court number, no markdown, no
  headings/bullets. Neutral, factual, grounded in the provided data.

Never invent names, amounts, sections, dates, or outcomes.

OUTPUT FORMAT — return ONLY a single JSON object, no markdown fences, no prose.
Include only the requested key(s); set an unrequested key to null:
{"title": "नेपाली शीर्षक (080-CR-0047)", "short_description": "एक-वाक्य सारांश"}
"""

USER_PROMPT = """\
{request_note}

Current title: {title}
Special-court case number (MUST end any regenerated title): {court_number}
Bigo (बिगो), NPR: {bigo}

KEY ALLEGATIONS:
{key_allegations}

DESCRIPTION (snippet — the basis for both fields):
{description}

Return ONLY the JSON object described in the system prompt.
"""


def main():
    ap = argparse.ArgumentParser(
        description="Enrich case listing-card fields (title + short_description) "
        "via LLM, DB-free over HTTP.",
    )
    add_common_args(ap)
    ap.add_argument(
        "--only",
        choices=("title", "short", "both"),
        default="both",
        help="Which card field(s) to (re)generate (default: both).",
    )
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

    # short_description is the natural skip signal: nothing populates it today, so
    # virtually every card is processed and both fields get evaluated. A populated
    # short_description skips the case unless --force.
    cases = list(get_target_cases(api, args, skip_field="short_description"))
    total = len(cases)
    if total == 0:
        print("No CIAA draft cases to process.", file=sys.stderr)
        sys.exit(0)

    print(f"Found {total} CIAA draft case(s) to process. Target: {args.only}")
    if args.dry_run:
        print("  [DRY RUN] No changes will be saved.")

    stats = {
        "cases_processed": 0,
        "titles_enriched": 0,
        "short_descriptions_enriched": 0,
        "fields_already_ok": 0,
        "cases_no_content": 0,
        "cases_llm_error": 0,
    }

    for idx, case in enumerate(cases, 1):
        try:
            _process_case(
                case=case,
                idx=idx,
                total=total,
                only=args.only,
                force=args.force,
                dry_run=args.dry_run,
                api=api,
                usage=usage,
                invoke_text=invoke_text,
                stats=stats,
            )
        except Exception as exc:  # noqa: BLE001 — one case must not abort the batch
            stats["cases_llm_error"] += 1
            print(f"Unhandled error processing case: {exc}", file=sys.stderr)
            if args.verbose:
                import traceback

                traceback.print_exc()

    print_summary(stats, args.dry_run, "Card (title + short_description) enrichment")
    if usage.calls > 0:
        print()
        print(render_usage_table(usage.as_dict()["by_provider"], title="card usage"))


def _process_case(
    case, idx, total, only, force, dry_run, api, usage, invoke_text, stats
):
    stats["cases_processed"] += 1
    case_id = case.get("case_id", "?")
    current_title = case.get("title", "")
    print(f"\n[{idx}/{total}] {case_id} — {current_title[:80]}")

    try:
        detail = api.get_case(case.get("slug") or case_id)
    except Exception:  # noqa: BLE001
        detail = case
        print("  (using summary instead of detail)")

    current_title = detail.get("title", current_title)
    court_number = _special_court_number(detail)

    # Decide which fields need work.
    want_title = only in ("title", "both")
    want_short = only in ("short", "both")

    # Without a court number the title can never satisfy the format contract
    # (must end in "(<court-number>)"), so regenerating would only burn an LLM
    # call on a title that's guaranteed to be rejected — skip it.
    need_title = (
        want_title
        and bool(court_number)
        and (force or not _title_is_valid(current_title, court_number))
    )
    need_short = False
    if want_short:
        if force:
            need_short = True
        else:
            adequate, reason = judge_description_adequacy(
                detail.get("short_description") or "",
                kind="case card short description (one-line teaser)",
                invoke_text=invoke_text,
                usage=usage,
                context=f"title={current_title[:80]}",
            )
            need_short = not adequate
            if adequate:
                print(f"  short_description already adequate ({reason})")

    if not need_title and not need_short:
        stats["fields_already_ok"] += 1
        print("  Nothing to do (use --force to regenerate)")
        return

    description = (detail.get("description") or "").strip()
    allegations = detail.get("key_allegations") or []
    if not description and not allegations:
        stats["cases_no_content"] += 1
        print("  No description or allegations to derive from — skipping")
        return

    try:
        result = _generate(
            detail=detail,
            court_number=court_number,
            need_title=need_title,
            need_short=need_short,
            invoke_text=invoke_text,
            usage=usage,
        )
    except Exception as exc:  # noqa: BLE001
        stats["cases_llm_error"] += 1
        print(f"  LLM generation failed: {exc}")
        return
    if not result:
        stats["cases_llm_error"] += 1
        print("  LLM returned no parseable result — skipping")
        return

    case_slug = detail.get("slug") or case.get("slug")

    # ── title ──
    if need_title:
        new_title = (result.get("title") or "").strip()
        issue = _validate_title(new_title, court_number) if new_title else "no title"
        if not new_title or issue or _title_has_headcount(new_title):
            why = issue or "contains a defendant headcount"
            print(f"  TITLE rejected ({why}) — not writing")
        else:
            print(f"  TITLE: {new_title}")
            if dry_run:
                print("    [DRY RUN] would PATCH /title")
            else:
                try:
                    api.patch_field(case_slug, "title", new_title)
                    stats["titles_enriched"] += 1
                    print("    [TITLE UPDATED]")
                except Exception as exc:  # noqa: BLE001
                    stats["cases_llm_error"] += 1
                    print(f"    title PATCH failed: {exc}")

    # ── short_description ──
    if need_short:
        new_short = (result.get("short_description") or "").strip()
        if not new_short:
            print("  SHORT rejected (empty) — not writing")
        elif len(new_short) > MAX_SHORT_DESCRIPTION_CHARS:
            print(
                f"  SHORT rejected (too long: {len(new_short)} > "
                f"{MAX_SHORT_DESCRIPTION_CHARS}) — not writing"
            )
        else:
            print(f"  SHORT: {new_short}")
            if dry_run:
                print("    [DRY RUN] would PATCH /short_description")
            else:
                try:
                    api.patch_field(case_slug, "short_description", new_short)
                    stats["short_descriptions_enriched"] += 1
                    print("    [SHORT UPDATED]")
                except Exception as exc:  # noqa: BLE001
                    stats["cases_llm_error"] += 1
                    print(f"    short_description PATCH failed: {exc}")


def _generate(detail, court_number, need_title, need_short, invoke_text, usage):
    wanted = []
    if need_title:
        wanted.append("title")
    if need_short:
        wanted.append("short_description")
    request_note = (
        "Produce the title AND the short_description."
        if len(wanted) == 2
        else f"Only the {wanted[0]} is needed; set the other key to null."
    )
    description = (detail.get("description") or "").strip()
    prompt = USER_PROMPT.format(
        request_note=request_note,
        title=detail.get("title", ""),
        court_number=court_number or "(unknown)",
        bigo=_format_bigo(detail.get("bigo")),
        key_allegations=_format_list(detail.get("key_allegations")),
        description=description[:DESCRIPTION_SNIPPET_BUDGET] or "(none)",
    )
    response_text = invoke_text(
        system=SYSTEM_PROMPT,
        content=prompt,
        max_tokens=1000,
        tier="cheap",
        usage=usage,
    )
    return _parse_response(response_text)


def _parse_response(response_text: str) -> Optional[dict]:
    """Parse {title, short_description} from the response, scanning every '{' so
    leading prose with braces doesn't abort the parse."""
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
        if isinstance(obj, dict) and ("title" in obj or "short_description" in obj):
            return obj
    logger.warning("No card JSON object found in LLM response")
    return None


def _special_court_number(case: dict) -> Optional[str]:
    for ref in case.get("court_cases") or []:
        if isinstance(ref, str) and ref.startswith("special:"):
            return ref.split(":", 1)[1]
    for ref in case.get("court_cases") or []:
        if isinstance(ref, str) and ":" in ref:
            return ref.split(":", 1)[1]
    return None


def _title_is_valid(title: str, court_number: Optional[str]) -> bool:
    """A title is already valid when it ends with its special-court number in
    parens AND carries no defendant headcount."""
    if not title or not court_number:
        return False
    return _validate_title(title, court_number) is None and not _title_has_headcount(
        title
    )


def _validate_title(title: str, court_number: Optional[str]) -> Optional[str]:
    nums = {m.group(0).upper() for m in COURT_RE.finditer(title)}
    if not nums:
        return "no court case number"
    if court_number and court_number.upper() not in nums:
        return (
            f"title number(s) {sorted(nums)} do not include the special-court "
            f"number {court_number}"
        )
    if court_number:
        expected = f"({court_number.upper()})"
        if not title.upper().rstrip().endswith(expected):
            return f"title must end with the case number in parentheses, e.g. '… {expected}'"
    return None


def _title_has_headcount(title: str) -> bool:
    return bool(HEADCOUNT_RE.search(title or ""))


def _format_bigo(bigo) -> str:
    try:
        value = int(bigo)
    except (TypeError, ValueError):
        return "(unknown)"
    return f"{value:,}" if value > 0 else "(unknown)"


def _format_list(items) -> str:
    if not items:
        return "(none provided)"
    return "\n".join(f"- {x}" for x in items)


if __name__ == "__main__":
    main()
