#!/usr/bin/env python
"""Extract CIAA Special Court case related/location entities via LLM (DB-free).
EXTRACTION ONLY -- this port never writes to ``/entities``. Read the
ARCHITECTURE BLOCK below before assuming that is an oversight.

Ported from the deleted `casework/enrich_related_entities.py` (recovered at
donor commit `0321a85`, 553 lines). Reads a case's press-release AND/OR
court-order source text entirely over the Jawafdehi HTTP API and asks the
premium LLM tier to extract related/location entities plus short accused-person
notes, in one response.

================================ ARCHITECTURE BLOCK =================================
The donor is architecturally dead against the current write schema, and this
port does NOT bridge that gap -- on purpose, after an explicit escalation and
decision, not because the gap went unnoticed.

THE DONOR'S WRITE SHAPE (0321a85, no longer valid against this branch):
    entity_id = api.create_entity(display_name=name, nes_id="")        # donor line 510
    entities_to_patch.append({"entity": entity_id,
                               "relationship_type": rel_type_enum,
                               "notes": notes})                        # donor lines 522-528
    api.patch_field(slug, "entities", entities_to_patch)                # donor line 543
The donor blindly minted a brand-new entity for every LLM-extracted name, with
NO NES resolution at all -- keyed by a flat `entity` id, no `nes_id`, no
`outcome`. `CaseworkApi.create_entity` does not exist anywhere on this branch
and never has during this porting project (`casework/common/api.py` only
exposes `get`, `iter_cases`, `get_case`, `patch_field`, `replace_list`).

THE CURRENT SCHEMA (`cases/caseworker_serializers.py::EntityPatchItemSerializer`,
~lines 148-190): every `/entities` item MUST be
    {"nes_id": <canonical NES @id IRI, validated by is_valid_entity_iri>,
     "relationship_type": ..., "outcome"?: <ACCUSED-role only>, "notes": ...}
"The bind holds the canonical NES entity id directly; entities are owned by
NES and must already exist there (no display-name fallback)" (serializer
comment, verbatim). Posting a donor-shaped item here either 422s (missing/
invalid `nes_id`) or -- far worse -- would require a name-matching shortcut
that could silently bind the WRONG NES entity to a corruption case. That is a
defamation risk, not a data-quality nit.

WHY NO RESOLVER WAS BUILT HERE: turning an LLM-extracted Nepali name into a
confirmed `nes_id` needs a matching/confidence design with no donor precedent.
The closest analogue in this codebase, the still-live (but itself
`NotImplementedError`'d in its own `handle()`) `cases/management/commands/
enrich_ciaa_related_entities.py`, invents its own `_link_nes()` search +
0.8-confidence-threshold heuristic with zero test coverage of false-positive
binds. Bolting an equally untested heuristic onto this DB-free port -- inside
a task scoped as a "port" -- is exactly the invented-behavior shape this
project has repeatedly shipped (see the phantom `missing_details` and
`validate_timeline_items` functions caught in tasks 14b/14c). Per explicit
instruction, this port does NOT add `create_entity`, a resolver, a fuzzy
matcher, or a `--force-bind` escape hatch -- not even disabled behind a flag.

WHAT THIS MODULE DOES INSTEAD: it ports the donor's extraction machinery
byte-for-byte -- system prompt, both truncation limits (including the
asymmetric `PRESS_RELEASE_CHARS_NO_COURT` branch), prompt-budget enforcement,
and the press-OR-court content selection (either source alone is sufficient,
matching `STAGES["entities"].requires_materials` and the donor's own
`_get_content_for_case`/caller gate at donor line 404: "No press release or
court order content -- skipping") -- so extraction quality can be measured and
reviewed. `main()` calls the LLM, parses `entities` + `accused_notes`, and
reports per-case AND aggregate counts of what WOULD be bound -- but never
calls `api.patch_field` or `api.replace_list`. A run that extracts 200
entities and binds zero says so plainly in the final summary (see the
unconditional "TOTAL entities bound to cases: 0" line), not just in per-case
logs.

ALSO NOT PORTED: the brief's suggested `validate_entity_item` function
(canonical-`nes_id` + accused-only-`outcome` validation) does not exist
ANYWHERE in the donor's history -- `git log --all -p -- casework/
enrich_related_entities.py` never defines it. It matches the CURRENT
`EntityPatchItemSerializer`'s rules, not any donor behavior, so per this
task's "donor is the source of truth" mandate it is not implemented here.
Same phantom-function shape as `normalise_missing_details` (14b) and
`validate_timeline_items`-under-a-different-name (14c) -- flagged for the
dispatcher, not silently added.
=======================================================================================

Usage:
    python casework/enrich_related_entities.py --dry-run
    python casework/enrich_related_entities.py --slug case-0123
    python casework/enrich_related_entities.py --limit 10 --verbose
    python casework/enrich_related_entities.py --apply   # still writes NOTHING
"""

import argparse
import logging
import os
import sys
import time

from casework.common.api import CaseworkApi
from casework.common.cli import (
    add_common_args,
    configure_run_logging,
    log_event,
    log_run_footer,
    log_run_header,
    print_summary,
    setup_logging,
)
from casework.common.llm import bootstrap, tier_for
from casework.common.materials import source_text
from casework.common.parse import parse_extraction_response
from casework.common.pipeline import (
    COURT_TYPES,
    PRESS_TYPES,
    STAGES,
    RunReport,
    unmet_prerequisites,
)
from casework.common.select import select_cases

log = logging.getLogger("casework.enrich_related_entities")

STAGE = STAGES["entities"]

# ── Slicing constants (verbatim from the donor's `env_int(NAME, default)`
# defaults). The donor read these via an `env_int()` helper that lived in the
# deleted `casework/common.py` and was never re-created in the Task 5-11
# common package (see `enrich_missing_bigo.py`'s identical note) -- fixed at
# the donor's own defaults.
COURT_ORDER_FULL_THRESHOLD = 8_000
COURT_ORDER_HEAD_CHARS = 4_000
COURT_ORDER_TAIL_CHARS = 2_000
COURT_ORDER_THAHAR_CHARS = 12_000

PRESS_RELEASE_CHARS = 3_000
PRESS_RELEASE_CHARS_NO_COURT = 18_000

PROMPT_HARD_MAX = 25_000

SYSTEM_PROMPT = """You are a Nepali legal research assistant helping to build a public transparency database of court cases.
Analyze the provided Nepali legal documents (press release and/or court order excerpts) and extract structured data.

You must extract THREE things in a single response:

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PART 1 — LOCATION ENTITIES (relationship_type="location")
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Extract the district(s), municipality, or province WHERE THE CASE EVENTS occurred
or where the key assets/funds at issue are located.

STRICT RULES:
- Extract ONLY where the case events happened or where the assets are.
- DO NOT extract accused home addresses, birthplaces, or permanent addresses.
- DO NOT extract the location of courts or government inquiry offices.
- Extract 1 location for simple cases. Extract 2-3 only if the case genuinely spans
  multiple districts.
- Leave notes BLANK ("") for all location entities.
- The entity_name should include context in the format: "Organisation/Activity - Location"

Examples of CORRECT location entity names:
- "साझा भण्डार सहकारी - सुर्खेत जिल्ला"
- "स्वास्थ्य उपकरण खरिद - जनकपुरधाम"
- "भरत ताल निर्माण परियोजना - सर्लाही जिल्ला"
- "नापी कार्यालय - खैरहनी नगरपालिका"
- (if no specific activity context, just the location name: "काठमाडौं")

Examples of WRONG location names:
- "तनहुँ जिल्ला" ← accused home address, SKIP
- "काठमाडौं" ← if only reason is court/CIAA office, SKIP

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PART 2 — RELATED ENTITIES (relationship_type="related")
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Any person or organization connected to the case BEYOND the primary accused.
Extract ALL of these categories that appear in the documents:

  GOVERNMENT BODIES — ministry, department, municipality, office whose funds were
  misused or where the accused worked.
  Examples: "जलश्रोत तथा सिँचाइ विभाग"  notes: "आरोपी कार्यरत रहेको सरकारी निकाय"
            "राष्ट्रिय सूचना प्रविधि केन्द्र"  notes: "खरिद प्रक्रियामा संलग्न सरकारी निकाय"

  COMPANIES/CONTRACTORS — firms, JVs, cooperatives, suppliers, foreign companies.
  Examples: "कल्पवृक्ष-कोहिनूर जे.भी."  notes: "ठेक्का प्राप्त गर्ने संयुक्त उद्यम"
            "UOB Singapore बैंक"  notes: "Singapore स्थित बैंक, रकम हस्तान्तरणमा प्रयोग"

  FAMILY MEMBERS — spouse, children, relatives holding assets.
  Example: "श्रृजना गिरी"  notes: "आरोपितको श्रीमती, सम्पत्ति हस्तान्तरण गरिएको"

  CO-DEFENDANTS/ASSOCIATES — secondary actors, facilitators, middlemen.
  Example: "नानी काजी थापा"  notes: "घुस लेनदेनमा सहयोग"

  INVESTIGATING/PROSECUTING BODIES — DO NOT extract the inquiry commission
  (अख्तियार दुरुपयोग अनुसन्धान आयोग) or special attorney office as standalone
  entities — they are present in every case. DO NOT extract individual prosecutors,
  attorneys, judges, or court staff — they are performing standard professional
  duties, not materially connected to the case events.
  Only extract named CIAA investigation officers if they are specifically named
  and their investigation is directly relevant.
  Example: "रविन्द्र कुमार बुढाप्रिथी"  notes: "अनुसन्धान अधिकृत, CIAA"

  WITNESSES/INVESTIGATORS — named inquiry officers, key witnesses.
  Example: "रविन्द्र कुमार बुढाप्रिथी"  notes: "अनुसन्धान अधिकृत, CIAA"

Notes must never be blank for related entities. Always describe the specific connection.
Only extract entities with CONFIRMED connections — not people who were later acquitted.

PRIORITY ORDER: People and organizations DIRECTLY involved in the case events come first.
Generic legal infrastructure (courts, attorney offices) should be skipped unless a
specific named person from those bodies is materially connected.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PART 3 — ACCUSED NOTES (accused_notes array)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
For each primary accused person named in the documents, extract a SHORT note
describing their job title and role. Format: "job title, employer"
Examples:
  "तत्कालीन प्रबन्ध निर्देशक, नेपाल टेलिकम"
  "तत्कालीन नगरप्रमुख, खैरहनी नगरपालिका"
  "नापी अधिकृत, नापी कार्यालय चाबहिल"

Only include primary accused persons. Keep notes under 80 chars.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
OUTPUT FORMAT
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Output ONLY this JSON object, no other text:
{
  "entities": [
    {
      "entity_name": "Name exactly as in document",
      "relationship_type": "location" or "related",
      "notes": "specific description"
    }
  ],
  "accused_notes": [
    {
      "name": "Accused person name exactly as in document",
      "notes": "job title, employer"
    }
  ]
}
"""


def _truncate_press_release(text, limit=None):
    """Truncate press release, cutting at sentence boundary before limit."""
    if not text:
        return text
    if limit is None:
        limit = PRESS_RELEASE_CHARS
    if len(text) <= limit:
        return text

    chunk = text[:limit]
    for sep in ("।", "\n", ".", "!"):
        idx = chunk.rfind(sep)
        if idx >= limit // 2:
            return chunk[: idx + 1]

    return chunk


def _truncate_court_order(text):
    """Extract the most entity-rich section from a court order."""
    if not text:
        return text

    if len(text) < COURT_ORDER_FULL_THRESHOLD:
        return text

    thahar_marker = "ठहर खण्ड"
    idx = text.find(thahar_marker)
    if idx != -1:
        thahar_text = text[idx:]
        limit = COURT_ORDER_THAHAR_CHARS
        if len(thahar_text) <= limit:
            return f"\n\n[...ठहर खण्ड (verdict section)...]\n\n{thahar_text}"
        chunk = thahar_text[:limit]
        for sep in ("।", "\n", ".", "!"):
            sep_idx = chunk.rfind(sep)
            if sep_idx >= limit // 2:
                chunk = chunk[: sep_idx + 1]
                break
        return f"\n\n[...ठहर खण्ड (verdict section)...]\n\n{chunk}"

    label_head = "\n\n[...court order header section...]\n\n"
    label_tail = "\n\n[...court order verdict section...]\n\n"
    return (
        label_head
        + text[:COURT_ORDER_HEAD_CHARS]
        + label_tail
        + text[-COURT_ORDER_TAIL_CHARS:]
    )


def _enforce_prompt_budget(parts):
    """Ensure combined prompt stays within budget."""
    combined = "\n\n".join(parts)
    if len(combined) <= PROMPT_HARD_MAX:
        return combined

    # Find largest part and truncate
    largest_idx = max(range(len(parts)), key=lambda i: len(parts[i]))
    current_overage = len(combined) - PROMPT_HARD_MAX
    original = parts[largest_idx]
    if len(original) > current_overage + 1000:
        parts[largest_idx] = original[: len(original) - current_overage - 100]

    combined = "\n\n".join(parts)
    return combined[:PROMPT_HARD_MAX]


def _build_content_parts(press_release_text, court_order_text):
    """Build the LLM's user-prompt sections from the two independently-sourced
    texts. Extracted verbatim from the donor's inline `_process_case` (donor
    lines 385-405) into a named, unit-testable function -- the logic itself is
    unchanged: either source alone is sufficient, and the press-release
    truncation limit depends on whether a court order is ALSO present
    (`PRESS_RELEASE_CHARS_NO_COURT` vs `PRESS_RELEASE_CHARS`)."""
    content_parts = []

    if press_release_text:
        if court_order_text is None:
            truncated = _truncate_press_release(
                press_release_text, limit=PRESS_RELEASE_CHARS_NO_COURT
            )
        else:
            truncated = _truncate_press_release(press_release_text)
        content_parts.append("--- PRESS RELEASE ---")
        content_parts.append(truncated)

    if court_order_text:
        truncated = _truncate_court_order(court_order_text)
        content_parts.append("--- COURT ORDER ---")
        content_parts.append(truncated)

    return content_parts


def _parse_extraction_response(response_text):
    """Extract entities and accused_notes from LLM response JSON."""
    entities = parse_extraction_response(response_text, {"entities"}) or []
    accused_notes = parse_extraction_response(response_text, {"accused_notes"}) or []
    return entities, accused_notes


def build_api(args):
    """Construct the client. Basic (local DEV_AUTH) unless a token is given.

    `allow_remote_writes` is threaded through here for uniformity with the
    other five ported enrichers even though this module never calls
    `patch_field`/`replace_list` -- see module docstring (EXTRACTION ONLY).
    Passing it is harmless: it only changes what `CaseworkApi._patch` would
    do, and `_patch` is never reached from this file.
    """
    if args.api_token:
        return CaseworkApi(
            args.api_base_url, token=args.api_token,
            allow_remote_writes=args.allow_remote_writes,
        )
    return CaseworkApi(
        args.api_base_url,
        basic=(os.getenv("CASEWORK_API_USER", "abgen"),
               os.getenv("CASEWORK_API_PASSWORD", "local-dev-only")),
        allow_remote_writes=args.allow_remote_writes,
    )


def main(argv=None):
    """Main entry point. EXTRACTION ONLY -- see module docstring. This never
    calls `api.patch_field` or `api.replace_list`, regardless of `--dry-run`/
    `--apply`."""
    ap = argparse.ArgumentParser(
        description=(
            "Extract related and location entities from CIAA cases via LLM "
            "(DB-free, EXTRACTION ONLY -- no /entities writes; see module docstring)."
        ),
        epilog="Reads cases entirely over the Jawafdehi HTTP API. Writes nothing.",
    )
    add_common_args(ap)
    args = ap.parse_args(argv)

    setup_logging(args.verbose)
    logger, run_id, paths = configure_run_logging("entities", verbose=args.verbose)
    start_time = time.monotonic()

    # Bootstrap Django + LLM (MUST come before importing llm.invoke)
    try:
        bootstrap(args.provider, args.model)
    except Exception as exc:
        print(f"Bootstrap failed: {exc}", file=sys.stderr)
        sys.exit(1)

    from llm.invoke import invoke_text
    from llm.usage import UsageAccumulator, render_usage_table

    api = build_api(args)
    usage = UsageAccumulator()
    report = RunReport()

    all_cases = list(api.iter_cases())
    cases = select_cases(
        all_cases,
        fiscal_year=args.fiscal_year,
        slugs=args.slug,
        court_cases=args.court_case,
    )
    if args.limit:
        cases = cases[: args.limit]

    total = len(cases)
    log_run_header(
        logger, stage="entities", base_url=args.api_base_url, dry_run=args.dry_run,
        provider=args.provider, model=args.model, n_selected=total,
        run_id=run_id, paths=paths,
    )
    if total == 0:
        print("No matching CIAA case(s) to process.", file=sys.stderr)
        print_summary(report.summary(), args.dry_run, "Related-entity extraction")
        log_run_footer(
            logger, stage="entities", stats=report.summary(),
            duration_s=time.monotonic() - start_time,
        )
        return report

    print(f"Found {total} matching case(s).")
    print("  NOTE: this port performs EXTRACTION ONLY -- no /entities writes are")
    print("  made, regardless of --dry-run/--apply. See module docstring for why.")
    if args.force:
        print("  --force: re-extracting even for cases with entities already populated")

    total_entities_extracted = 0
    total_accused_notes_extracted = 0

    for idx, case in enumerate(cases, 1):
        slug = case.get("slug") or "?"
        title = case.get("title") or ""
        print(f"\n[{idx}/{total}] {slug} — {title[:80]}")
        log_event(logger, paths["events"], run_id=run_id, stage="entities", slug=slug,
                  step="start", status="start", detail=f"[{idx}/{total}] {title[:80]}")

        # Donor: `get_target_cases(api, args, skip_field="entities")` (donor
        # line 274) -- of the five ported enrichers, this was the only one
        # missing the already-populated skip, so every run re-spent a
        # premium-tier LLM call on cases whose `entities` were already set.
        if case.get("entities") and not args.force:
            report.record(
                slug, "entities", "already",
                f"entities already {case['entities']}")
            print("  entities already populated — skipping (use --force to re-extract)")
            log_event(logger, paths["events"], run_id=run_id, stage="entities", slug=slug,
                      step="idempotency", status="already",
                      detail=f"entities already {case['entities']}")
            continue

        try:
            detail = api.get_case(slug)
        except Exception as exc:
            detail = case
            print(f"  (using summary instead of detail: {exc})")
            log_event(logger, paths["events"], run_id=run_id, stage="entities", slug=slug,
                      step="fetch", status="fallback", detail=str(exc),
                      level=logging.WARNING)

        unmet = unmet_prerequisites(STAGE, detail)
        if unmet:
            for reason in unmet:
                report.record(slug, "entities", "unmet", reason)
            print(f"  Unmet prerequisite(s): {'; '.join(unmet)}")
            log_event(logger, paths["events"], run_id=run_id, stage="entities", slug=slug,
                      step="prereq", status="unmet", detail="; ".join(unmet),
                      level=logging.WARNING)
            continue

        press_text, press_unmet = source_text(detail, types=PRESS_TYPES)
        court_text, court_unmet = source_text(detail, types=COURT_TYPES)
        press_text = press_text.strip() or None
        court_text = court_text.strip() or None

        content_parts = _build_content_parts(press_text, court_text)
        if not content_parts:
            # Donor-preserved gate (donor line 404): skip only when BOTH
            # press release and court order content are absent.
            reasons = (press_unmet + court_unmet) or [
                "no press release or court order content"]
            for reason in reasons:
                report.record(slug, "entities", "unmet", reason)
            print("  No press release or court order content — skipping")
            log_event(logger, paths["events"], run_id=run_id, stage="entities", slug=slug,
                      step="source", status="unmet", detail="; ".join(reasons),
                      level=logging.WARNING)
            continue

        if press_text:
            print(f"  Press release: {len(press_text)} chars")
            log_event(logger, paths["events"], run_id=run_id, stage="entities", slug=slug,
                      step="source", status="ok", detail=f"press release {len(press_text)} chars")
        if court_text:
            print(f"  Court order: {len(court_text)} chars")
            log_event(logger, paths["events"], run_id=run_id, stage="entities", slug=slug,
                      step="source", status="ok", detail=f"court order {len(court_text)} chars")

        user_prompt = _enforce_prompt_budget(content_parts)
        print(f"  Prompt size: {len(user_prompt)} chars")
        log_event(logger, paths["events"], run_id=run_id, stage="entities", slug=slug,
                  step="prompt", status="ok", detail=f"{len(user_prompt)} chars")

        if not user_prompt.strip():
            report.record(slug, "entities", "skipped", "empty prompt after truncation")
            print("  Empty prompt after truncation — skipping")
            log_event(logger, paths["events"], run_id=run_id, stage="entities", slug=slug,
                      step="prompt", status="skipped",
                      detail="empty prompt after truncation", level=logging.WARNING)
            continue

        try:
            response_text = invoke_text(
                system=SYSTEM_PROMPT,
                content=user_prompt,
                max_tokens=2000,
                tier=tier_for("entities"),
                usage=usage,
            )
        except Exception as exc:
            report.record(slug, "entities", "error", f"LLM extraction failed: {exc}")
            print(f"  LLM extraction failed: {exc}")
            log_event(logger, paths["events"], run_id=run_id, stage="entities", slug=slug,
                      step="extract", status="error", detail=str(exc),
                      level=logging.ERROR)
            if args.verbose:
                import traceback

                traceback.print_exc()
            continue

        entities_data, accused_notes = _parse_extraction_response(response_text)
        valid_items = [
            item for item in entities_data
            if isinstance(item, dict)
            and (item.get("entity_name") or "").strip()
            and (item.get("relationship_type") or "").lower() in ("location", "related")
        ]

        if not valid_items and not accused_notes:
            report.record(
                slug, "entities", "skipped", "LLM returned no entities or accused notes")
            print("  LLM returned no entities or accused notes — skipping")
            log_event(logger, paths["events"], run_id=run_id, stage="entities", slug=slug,
                      step="extract", status="skipped",
                      detail="LLM returned no entities or accused notes",
                      level=logging.WARNING)
            continue

        total_entities_extracted += len(valid_items)
        total_accused_notes_extracted += len(accused_notes)

        print(
            f"  Extracted {len(valid_items)} entities, {len(accused_notes)} accused "
            "note(s) — NOT bound (nes_id resolution unavailable, see module docstring)")
        for item in valid_items[:5]:
            rel_type = item.get("relationship_type", "")
            print(f"    {rel_type:8s}  {(item.get('entity_name') or '')[:60]}")
        log_event(
            logger, paths["events"], run_id=run_id, stage="entities", slug=slug,
            step="extract", status="ok",
            detail=(
                f"{len(valid_items)} entities + {len(accused_notes)} accused_notes "
                "extracted; 0 bound"))

        report.record(
            slug, "entities", "extracted-unbound",
            f"{len(valid_items)} entities + {len(accused_notes)} accused_notes "
            "extracted; 0 bound -- nes_id resolution out of scope for this port")

    stats = report.summary()
    print_summary(stats, args.dry_run, "Related-entity extraction")
    unmet_reasons = report.unmet_reasons()
    if unmet_reasons:
        print("  unmet reasons:")
        for reason, count in unmet_reasons.most_common():
            print(f"    {count} x {reason}")

    # Surfaced unconditionally, never buried in per-case logs: a run that
    # extracted N entities and bound zero must say so plainly here.
    print()
    print(f"  TOTAL entities extracted across all cases: {total_entities_extracted}")
    print(f"  TOTAL accused notes extracted: {total_accused_notes_extracted}")
    print(
        "  TOTAL entities bound to cases: 0 (writes are intentionally disabled -- "
        "nes_id resolution is out of scope for this port; see module docstring)")

    usage_summary = ""
    if usage.calls > 0:
        usage_summary = render_usage_table(
            usage.as_dict()["by_provider"], title="entities usage")
        print()
        print(usage_summary)

    log_run_footer(
        logger, stage="entities", stats=stats,
        duration_s=time.monotonic() - start_time, usage_summary=usage_summary,
    )

    return report


if __name__ == "__main__":
    main()
