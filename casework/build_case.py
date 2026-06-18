#!/usr/bin/env python
"""Unified agentic case-builder (DB-free script using the llm package).

Builds a WHOLE Jawafdehi corruption-case record in ONE agentic pass: it stages a
case's converted source documents + the authoritative NGM court record, runs a
single run-wild LLM session that emits the full payload (description, title,
key_allegations, timeline, bigo, tags), and PATCHes each field over the HTTP API.

This complements the per-field ``enrich_*.py`` scripts (which it does not
replace). It shines with ``--provider claude_agent``: that backend stages the
sources as files the model reads and loops an iterate-until-complete cheap-model
judge — a good fit for "read everything, miss nothing" extraction. It also runs
on proxy/bedrock/claude_cli (real tool loop, no staging).

Entity wiring is intentionally OUT OF SCOPE here: the agent lists candidate
parties for reference, but the ``entities`` field is populated by the dedicated
``enrich_related_entities.py`` (NES resolution + relationship enums + merge).

Prereqs for ``--provider claude_agent``:
  * CLAUDE_AGENT_MCP_CONFIG → a jawafdehi-mcp config so the agent has the
    ``convert_date`` tool (e.g. uvx --from services/jawafdehi-mcp jawafdehi-mcp).
  * CLAUDE_AGENT_JUDGE_PROVIDER → a cheap provider (proxy/bedrock) for a SEMANTIC
    completeness judge; unset = structural-only (valid-JSON-with-all-keys) check.

Usage:
    python casework/build_case.py --slug case-0123 --provider claude_agent --dry-run
    python casework/build_case.py --fiscal-year 080 --limit 5 --provider claude_agent
    python casework/build_case.py --slug case-0123 --provider proxy --model gpt-5.5
"""

import argparse
import logging
import os
import sys

# Ensure the api dir is on sys.path so imports work when run as a file.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from casework.common import (
    CaseworkApi,
    add_common_args,
    bootstrap,
    convert_date_tool,
    get_target_cases,
    is_valid_iso_date,
    print_summary,
    setup_logging,
    source_content,
)

logger = logging.getLogger(__name__)

# Split marker the claude_agent harness uses to stage case.md vs sources.md.
_SOURCES_MARKER = "SOURCE DOCUMENT EXCERPTS"

# Fields the builder produces and PATCHes directly (entities are deferred).
_TEXT_FIELDS = ("description", "title", "key_allegations", "timeline", "bigo", "tags")

BUILDER_SYSTEM = """\
You are a meticulous Nepali legal analyst building a complete case record for
Jawafdehi.org, an open civic archive of Nepal anti-corruption (CIAA / अख्तियार)
cases tried at the Special Court (विशेष अदालत).

You are given a case's SOURCE DOCUMENTS (charge sheet, CIAA press release, court
order/verdict — converted to markdown) and the official NGM court record. From
ONLY those materials, produce the structured fields below. Ground every value in
the sources; never fabricate amounts, dates, names, sections, or outcomes. If the
sources do not support a field, omit it (an empty/absent field is better than a
guessed one).

You have a `convert_date` tool that converts between AD (Gregorian) and BS
(Bikram Sambat) using Nepal's official calendar. LLMs routinely get BS<->AD wrong,
so you MUST use the tool for every timeline date — never convert in your head.
Source dates are BS (use bs_to_ad); NGM hearing dates are AD (use ad_to_bs).

You ALWAYS reply with a single valid JSON object and nothing else.
"""

# The output contract, appended to the end of the staged materials.
BUILDER_INSTRUCTION = """\
============================================================
TASK — BUILD THE CASE RECORD

Using ONLY the case data + source documents + NGM record above, output ONE JSON
object with these keys (include a key only when the sources support it):

{
  "description": "<Markdown, Nepali. Structured editorial summary of the case:
     the scheme/allegation, the accused and their office, amounts, the CIAA
     charge, the Special Court outcome, and any appeal. Use ### sub-headings.>",
  "title": "<short Nepali case title naming the accused + the substance, optionally
     with the court number, e.g. 'घुस रिसवत (080-CR-0047): नामथर'>",
  "key_allegations": ["<Nepali one-line allegation>", "..."],
  "timeline": [
    {"date": "YYYY-MM-DD",        // AD; from convert_date
     "date_bs": "YYYY-MM-DD",     // BS as in the source; from convert_date
     "title": "<short Nepali milestone label>",
     "description": "<1-3 Nepali sentences with specifics>",
     "end_date": "YYYY-MM-DD"}    // ONLY for the incident-period entry
  ],
  "bigo": <integer NPR amount of the alleged corruption (बिगो), digits only, no
     commas/decimals/paisa; null if the sources state none>,
  "tags": ["<lowercase-hyphenated topical tag>", "..."],
  "entities": [   // REFERENCE ONLY — not written by this tool
    {"name": "<party name>", "role": "accused|location|related|witness"}
  ]
}

Timeline milestones to look for (only those the sources support, chronological):
incident period (single entry with date+end_date), complaint (उजुरी), CIAA
investigation, press release, charge sheet filed (अभियोगपत्र दायर), interim order,
Special Court verdict (फैसला), Supreme Court appeal + verdict. Do NOT emit one
entry per hearing; use the NGM hearings only to anchor milestone dates.

Reply with the JSON object ONLY — no markdown fences, no prose.
"""


def _build_content(case, source_text, ngm_md):
    """Assemble the single content string handed to the model.

    The ``SOURCE DOCUMENT EXCERPTS`` marker lets the claude_agent harness stage
    the case stub (case.md) and the sources+NGM+task (sources.md) as files.
    """
    title = case.get("title") or ""
    court_cases = ", ".join(str(c) for c in (case.get("court_cases") or [])) or "(none)"
    evidence = case.get("evidence") or []
    src_lines = []
    for ev in evidence:
        s = (ev.get("source") or {}) if isinstance(ev, dict) else {}
        if s.get("title") or s.get("source_type"):
            src_lines.append(f"  - {s.get('title', '')} ({s.get('source_type', '')})")
    stub = (
        "CASE STUB (existing data — you are BUILDING the fields, this is context only):\n"
        f"title: {title}\n"
        f"court_cases: {court_cases}\n"
        f"existing key_allegations: {case.get('key_allegations') or '(none)'}\n"
        "attached sources:\n" + ("\n".join(src_lines) or "  (none)")
    )
    ngm_block = f"\n\n{ngm_md}" if ngm_md else ""
    return (
        f"{stub}\n\n{_SOURCES_MARKER} (converted to markdown):\n"
        f"{source_text or '(no source text could be converted)'}"
        f"{ngm_block}\n\n{BUILDER_INSTRUCTION}"
    )


def _clean_timeline(raw):
    """Keep timeline entries that have a valid AD date + a title; sort by date."""
    if not isinstance(raw, list):
        return []
    clean = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        date_val = str(item.get("date") or "").strip()
        title_val = str(item.get("title") or "").strip()
        if not date_val or not title_val or not is_valid_iso_date(date_val):
            continue
        entry = {"date": date_val, "title": title_val}
        for k in ("date_bs", "description", "end_date", "end_date_bs"):
            v = str(item.get(k) or "").strip()
            if v:
                entry[k] = v
        if entry.get("end_date") and not is_valid_iso_date(entry["end_date"]):
            entry.pop("end_date", None)
            entry.pop("end_date_bs", None)
        clean.append(entry)
    clean.sort(key=lambda e: e["date"])
    return clean


def _normalize_payload(payload):
    """Coerce the raw model JSON into the per-field values we PATCH."""
    out = {}
    if not isinstance(payload, dict):
        return out
    desc = str(payload.get("description") or "").strip()
    if desc:
        out["description"] = desc
    title = str(payload.get("title") or "").strip()
    if title:
        out["title"] = title
    allg = [
        str(a).strip() for a in (payload.get("key_allegations") or []) if str(a).strip()
    ]
    if allg:
        out["key_allegations"] = allg
    timeline = _clean_timeline(payload.get("timeline"))
    if timeline:
        out["timeline"] = timeline
    bigo = payload.get("bigo")
    if isinstance(bigo, bool):
        bigo = None
    if isinstance(bigo, (int, float)) and int(bigo) > 0:
        out["bigo"] = int(bigo)
    tags = [str(t).strip() for t in (payload.get("tags") or []) if str(t).strip()]
    if tags:
        out["tags"] = tags
    return out


def _process_case(
    case,
    idx,
    total,
    args,
    api,
    usage,
    invoke_with_tools,
    salvage_json,
    case_markdown,
    stats,
):
    """Build one case: stage materials, run the agent, PATCH the produced fields."""
    stats["cases_processed"] += 1
    slug = case.get("slug")
    title = case.get("title", "")
    print(f"\n[{idx}/{total}] {slug} — {title[:80]}")

    if not case.get("evidence"):
        try:
            case = api.get_case(slug)
        except Exception as exc:  # noqa: BLE001
            print(f"  detail fetch failed: {exc}")

    source_text, n_chars = source_content(case)
    try:
        ngm_md = case_markdown(case)
    except Exception as exc:  # noqa: BLE001 - NGM is best-effort
        ngm_md = ""
        logger.warning("NGM render failed: %s", exc)
    if not source_text and not ngm_md:
        stats["cases_no_content"] += 1
        print("  No source content or NGM record — skipping")
        return
    print(
        f"  Source content: {n_chars} chars | NGM record: {'yes' if ngm_md else 'no'}"
    )

    content = _build_content(case, source_text, ngm_md)
    try:
        text = invoke_with_tools(
            system=BUILDER_SYSTEM,
            content=content,
            tools=[convert_date_tool()],
            max_tokens=8000,
            tier="premium",
            usage=usage,
            max_iterations=40,
        )
        payload = salvage_json(text)
    except Exception as exc:  # noqa: BLE001
        stats["cases_llm_error"] += 1
        print(f"  LLM build failed: {exc}")
        if args.verbose:
            import traceback

            traceback.print_exc()
        return

    fields = _normalize_payload(payload)
    entities = payload.get("entities") if isinstance(payload, dict) else None
    if not fields:
        stats["cases_skipped"] += 1
        print("  Model produced no usable fields — skipping")
        return

    # Report what was built.
    summary = ", ".join(
        f"{k}({len(v) if isinstance(v, list) else 'set'})" for k, v in fields.items()
    )
    print(f"  Built: {summary}")
    if entities:
        names = ", ".join(
            f"{e.get('name')}[{e.get('role')}]" for e in entities if isinstance(e, dict)
        )
        print(f"  Entity candidates (run enrich_related_entities.py to wire): {names}")

    # PATCH each field (skip already-populated unless --force).
    for field in _TEXT_FIELDS:
        if field not in fields:
            continue
        if not args.force and case.get(field):
            stats["fields_already_populated"] += 1
            continue
        if args.dry_run:
            stats["fields_would_patch"] += 1
            print(f"  [DRY RUN] would PATCH /{field}")
            continue
        try:
            api.patch_field(slug, field, fields[field])
            stats["fields_patched"] += 1
            print(f"  [UPDATED] /{field}")
        except Exception as exc:  # noqa: BLE001
            stats["cases_llm_error"] += 1
            print(f"  Failed to PATCH /{field}: {exc}")
    stats["cases_built"] += 1


def main():
    ap = argparse.ArgumentParser(
        description="Build a whole CIAA case record in one agentic pass (DB-free).",
        epilog="Reads + writes entirely over HTTP. Entity wiring is left to "
        "enrich_related_entities.py.",
    )
    add_common_args(ap)
    args = ap.parse_args()

    setup_logging(args.verbose)
    try:
        bootstrap(args.provider, args.model)
    except Exception as exc:  # noqa: BLE001
        print(f"Bootstrap failed: {exc}", file=sys.stderr)
        sys.exit(1)

    from llm.invoke import invoke_with_tools, salvage_json
    from llm.usage import UsageAccumulator, render_usage_table
    from sourcing.ngm_render import case_markdown

    try:
        api = CaseworkApi(base_url=args.api_base_url, token=args.api_token)
    except RuntimeError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        sys.exit(1)

    usage = UsageAccumulator()
    # A case "needs building" when it has no description yet.
    cases = list(get_target_cases(api, args, skip_field="description"))
    total = len(cases)
    if total == 0:
        print("No CIAA draft cases to build.", file=sys.stderr)
        sys.exit(0)

    print(f"Found {total} CIAA case(s) to build (provider={args.provider}).")
    if args.dry_run:
        print("  [DRY RUN] No changes will be saved.")

    stats = {
        "cases_processed": 0,
        "cases_built": 0,
        "cases_skipped": 0,
        "cases_no_content": 0,
        "cases_llm_error": 0,
        "fields_patched": 0,
        "fields_would_patch": 0,
        "fields_already_populated": 0,
    }
    for idx, case in enumerate(cases, 1):
        try:
            _process_case(
                case,
                idx,
                total,
                args,
                api,
                usage,
                invoke_with_tools,
                salvage_json,
                case_markdown,
                stats,
            )
        except Exception as exc:  # noqa: BLE001
            stats["cases_llm_error"] += 1
            print(f"Unhandled error: {exc}", file=sys.stderr)
            if args.verbose:
                import traceback

                traceback.print_exc()

    print_summary(stats, args.dry_run, "Case build")
    if usage.calls > 0:
        print()
        print(render_usage_table(usage.as_dict()["by_provider"], title="Build usage"))


if __name__ == "__main__":
    main()
