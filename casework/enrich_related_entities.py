#!/usr/bin/env python
"""Enrich CIAA Special Court cases with related and location entities (DB-free script using the llm package).

Standalone script to extract related entities (persons, organizations, locations) from CIAA
press releases and court orders using LLM extraction, entirely over the Jawafdehi HTTP API.
Never touches the database.

Phase A.3 of the CIAA Case Enrichment pipeline. Populates ``Case.entities`` (relationship
list) with related and location entities extracted from CIAA press releases and court orders.

Usage:
    python casework/enrich_related_entities.py --dry-run
    python casework/enrich_related_entities.py --slug case-0123
    python casework/enrich_related_entities.py --limit 10 --verbose
    python casework/enrich_related_entities.py --fiscal-year 080 --dry-run
    python casework/enrich_related_entities.py --force
"""

import argparse
import logging
import os
import sys

import requests

# Ensure the api dir is in sys.path so imports work when run as a file
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from casework.common import (
    CaseworkApi,
    add_common_args,
    bootstrap,
    content_from_evidence_entry,
    get_target_cases,
    parse_extraction_response,
    print_summary,
    setup_logging,
)

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Slicing constants (match the original)
# ─────────────────────────────────────────────────────────────────────────────
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


def _parse_extraction_response(response_text):
    """Extract entities and accused_notes from LLM response JSON."""
    entities = parse_extraction_response(response_text, {"entities"}) or []
    accused_notes = parse_extraction_response(response_text, {"accused_notes"}) or []
    return entities, accused_notes


def main():
    """Main entry point."""
    ap = argparse.ArgumentParser(
        description="Extract related and location entities from CIAA cases via LLM (DB-free).",
        epilog="Reads cases and writes results entirely over HTTP via JAWAFDEHI_API_TOKEN.",
    )
    add_common_args(ap)

    args = ap.parse_args()

    # Set up logging
    setup_logging(args.verbose)

    # Bootstrap Django + LLM
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

    # Collect target cases (skip those with entities already populated, unless --force)
    cases = []
    for case in get_target_cases(api, args, skip_field="entities"):
        cases.append(case)

    if not cases:
        print("No CIAA draft cases to process.", file=sys.stderr)
        sys.exit(0)

    total = len(cases)
    print(f"Found {total} CIAA draft case(s) to process.")
    if args.force:
        print("  --force: re-enriching even for cases with entities")
    if args.dry_run:
        print("  [DRY RUN] No changes will be saved.")

    stats = {
        "cases_processed": 0,
        "cases_enriched": 0,
        "cases_skipped": 0,
        "cases_no_content": 0,
        "cases_llm_error": 0,
        "entities_created": 0,
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
            print(f"Unhandled error processing case: {exc}", file=sys.stderr)
            if args.verbose:
                import traceback

                traceback.print_exc()

    # Print summary
    print_summary(stats, args.dry_run, "Related-entity enrichment")

    # Print usage table
    if usage.calls > 0:
        print()
        print(
            render_usage_table(usage.as_dict()["by_provider"], title="entities usage")
        )


def _get_content_for_case(api, case):
    """Fetch case detail and return (press_release_text, court_order_text)."""
    slug = case.get("slug") or case.get("case_id")
    try:
        detail = api.get_case(slug)
    except requests.HTTPError:
        detail = case

    # Get press release content
    press_release_text = None
    evidence = detail.get("evidence") or []
    for entry in evidence:
        if not isinstance(entry, dict):
            continue
        source = entry.get("source")
        if not isinstance(source, dict):
            continue
        if source.get("source_type") != "CIAA_PRESS_RELEASE":
            continue
        text = content_from_evidence_entry(entry)
        if text:
            press_release_text = text
            break

    # For court order, we approximate: look for any COURT_ORDER source type
    # (the original uses DocumentSource model, but over API we just use evidence)
    court_order_text = None
    for entry in evidence:
        if not isinstance(entry, dict):
            continue
        source = entry.get("source")
        if not isinstance(source, dict):
            continue
        if source.get("source_type") == "COURT_ORDER":
            text = content_from_evidence_entry(entry)
            if text:
                court_order_text = text
                break

    return press_release_text, court_order_text


def _process_case(case, idx, total, dry_run, api, usage, invoke_text, stats):
    """Process a single case: fetch detail, extract entities, PATCH or preview."""
    stats["cases_processed"] += 1
    case_id = case.get("case_id", "?")
    title = case.get("title", "")
    slug = case.get("slug") or case_id
    print(f"\n[{idx}/{total}] {case_id} — {title[:80]}")

    # Get press release and court order content
    press_release_text, court_order_text = _get_content_for_case(api, case)

    # Build prompt
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
        print(f"  Press release: {len(press_release_text)} chars used={len(truncated)}")

    if court_order_text:
        truncated = _truncate_court_order(court_order_text)
        content_parts.append("--- COURT ORDER ---")
        content_parts.append(truncated)
        print(f"  Court order: {len(court_order_text)} chars used={len(truncated)}")

    if not content_parts:
        stats["cases_no_content"] += 1
        print("  No press release or court order content — skipping")
        return

    user_prompt = _enforce_prompt_budget(content_parts)
    print(f"  Prompt size: {len(user_prompt)} chars")

    if user_prompt.strip() == "":
        stats["cases_skipped"] += 1
        print("  Empty prompt after truncation — skipping")
        return

    # Call LLM
    try:
        response_text = invoke_text(
            system=SYSTEM_PROMPT,
            content=user_prompt,
            max_tokens=2000,
            tier="premium",
            usage=usage,
        )
    except Exception as exc:
        stats["cases_llm_error"] += 1
        print(f"  LLM extraction failed: {exc}")
        if logger.level == logging.DEBUG:
            import traceback

            traceback.print_exc()
        return

    # Parse response
    entities_data, accused_notes = _parse_extraction_response(response_text)

    if not entities_data and not accused_notes:
        stats["cases_skipped"] += 1
        print("  LLM returned no entities or accused notes — skipping")
        return

    # PATCH /entities REPLACES the case's relationship list server-side, so we
    # MUST start from the case's EXISTING entities (accused links + any prior
    # enrichment) and only ADD the newly-extracted location/related ones —
    # otherwise the replace would wipe the accused relationships. Fetch the full
    # case detail for the authoritative entity list (the iter-path summary may
    # not include it).
    try:
        existing_entities = api.get_case(slug).get("entities") or []
    except Exception:  # noqa: BLE001
        existing_entities = case.get("entities") or []
    entities_to_patch = []
    seen = set()
    for existing in existing_entities:
        if not isinstance(existing, dict):
            continue
        eid = existing.get("entity") or existing.get("entity_id")
        rel = existing.get("relationship_type") or existing.get("relationship")
        if not eid or not rel:
            continue
        key = (eid, rel)
        if key in seen:
            continue
        seen.add(key)
        entities_to_patch.append(
            {
                "entity": eid,
                "relationship_type": rel,
                "notes": existing.get("notes") or "",
            }
        )
    created_count = 0

    for item in entities_data:
        name = (item.get("entity_name") or "").strip()
        rel_type = item.get("relationship_type", "").lower()
        notes = (item.get("notes") or "").strip()

        if not name or rel_type not in ("location", "related"):
            continue

        if dry_run:
            print(
                f"  [DRY RUN] {rel_type:8s}  {name}" + (f"  — {notes}" if notes else "")
            )
            created_count += 1
            continue

        # Create entity via API
        try:
            entity_id = api.create_entity(display_name=name, nes_id="")
        except Exception as exc:
            print(f"    Failed to create entity '{name}': {exc}")
            continue

        # Map relationship_type string to enum
        rel_type_enum = "location" if rel_type == "location" else "related"

        key = (entity_id, rel_type_enum)
        if key in seen:
            continue
        seen.add(key)
        entities_to_patch.append(
            {
                "entity": entity_id,
                "relationship_type": rel_type_enum,
                "notes": notes,
            }
        )
        created_count += 1

    if created_count == 0:
        stats["cases_skipped"] += 1
        print("  No entities to create — skipping")
        return

    if dry_run:
        print(f"  [DRY RUN] Would PATCH {created_count} entities but --dry-run is set")
        stats["cases_enriched"] += 1
        return

    # PATCH the entities list (replaces server-side, so send all)
    try:
        api.patch_field(slug, "entities", entities_to_patch)
        stats["cases_enriched"] += 1
        stats["entities_created"] += created_count
        print(f"  [UPDATED] {case_id} with {created_count} entities")
    except Exception as exc:
        stats["cases_llm_error"] += 1
        print(f"  Failed to PATCH entities: {exc}")


if __name__ == "__main__":
    main()
