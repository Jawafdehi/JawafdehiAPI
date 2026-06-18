#!/usr/bin/env python
"""Enrich the two description fields attached to a case's sources, in ONE
source-fetching pass over the Jawafdehi HTTP API (DB-free):

  1. DocumentSource.description — a REUSABLE, case-agnostic abstract of the
     document itself (what it is, who issued it, when, identifiers, parties, a
     short factual précis). Written via PATCH /api/sources/{source_id}/. Because a
     source can be cited by many cases, it is written at most once per source per
     run and only when blank/placeholder; the prompt hard-guards it against any
     case-specific framing.

  2. Evidence-entry description — a CASE-SPECIFIC note on the source's probative
     role for THIS case's allegations. Written back into Case.evidence by replacing
     the /evidence array.

The expensive step — converting the source document to text — is shared: one fetch
+ one LLM call per evidence entry yields BOTH descriptions. Whether a field needs
regenerating is decided by the cheap adequacy judge; --force regenerates regardless.

Usage:
    python casework/enrich_evidence.py --dry-run
    python casework/enrich_evidence.py --slug case-0123
    python casework/enrich_evidence.py --priority --target source
    python casework/enrich_evidence.py --slug case-0123 --force
"""

import argparse
import json
import logging
import os
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

SOURCE_TEXT_BUDGET = 40000
CONVERTIBLE_ROLES = ("RAW", "ALTERNATE", "SOURCE_PAGE")

SYSTEM_PROMPT = """\
You are a Nepali legal editor for Jawafdehi, a civic accountability archive of \
Nepal's anti-corruption cases. You are given ONE source document (its type, title \
and full text) and the case that cites it (title, key allegations, named \
entities). Produce TWO descriptions in formal Nepali (देवनागरी; keep English \
technical and proper terms — "CR", company names — as-is).

1. source_description — a REUSABLE, CASE-AGNOSTIC abstract of the DOCUMENT ITSELF:
   what kind of document it is (अभियोगपत्र / फैसला / press release / audit report /
   news article), its issuer or outlet, the key date stated in it, any identifiers
   (CR / नि.नं. / मुद्दा नं.), the parties named in it, and a 1–3 line factual précis
   of its contents.
   CRITICAL: this text is shared by EVERY case that cites this document. It MUST NOT
   mention "this case" (यस मुद्दा), the citing case's allegations, or frame the
   document as evidence/proof. Describe ONLY the document. 2–5 sentences.

2. evidence_description — a CASE-SPECIFIC note on the document's PROBATIVE ROLE for
   THIS case: which allegation(s) it bears on and whether it is a primary record,
   corroboration, or background context. Frame the role/weight relative to the
   allegation; do NOT merely re-summarise the document's facts. 1–3 sentences.

Ground every statement in the provided document text and case data; never invent
names, amounts, sections, dates, or outcomes. If the document text is insufficient
for a field, write a faithful minimal description from what is available rather than
fabricating.

OUTPUT FORMAT — return ONLY a single JSON object, no markdown fences, no prose:
{"source_description": "…", "evidence_description": "…"}
"""

USER_PROMPT = """\
{targets_note}

SOURCE DOCUMENT
Type: {source_type}
Title: {source_title}

CITING CASE
Title: {case_title}
Court case: {court_number}
Key allegations:
{allegations}
Named entities:
{entities}

DOCUMENT TEXT:
{source_text}

Return ONLY the JSON object described in the system prompt.
"""


def main():
    ap = argparse.ArgumentParser(
        description="Enrich DocumentSource + evidence-entry descriptions via LLM, "
        "DB-free over HTTP.",
    )
    add_common_args(ap)
    ap.add_argument(
        "--target",
        choices=("source", "evidence", "both"),
        default="both",
        help="Which description(s) to regenerate (default: both).",
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

    # Evidence enrichment maps to no single case field; the per-entry judge decides
    # what to regenerate. Pass an empty skip_field so every DRAFT CIAA case is
    # scanned (case.get("") is None → never skipped at the list stage).
    cases = list(get_target_cases(api, args, skip_field=""))
    total = len(cases)
    if total == 0:
        print("No CIAA draft cases to process.", file=sys.stderr)
        sys.exit(0)

    print(f"Found {total} CIAA draft case(s) to process. Target: {args.target}")
    if args.dry_run:
        print("  [DRY RUN] No changes will be saved.")

    # Source-level descriptions are reusable: once a source_id is handled in this
    # run, later cases citing it must not re-judge or re-write it.
    sources_done: set[str] = set()
    stats = {
        "cases_processed": 0,
        "sources_enriched": 0,
        "evidence_enriched": 0,
        "entries_already_ok": 0,
        "entries_no_content": 0,
        "llm_errors": 0,
    }

    for idx, case in enumerate(cases, 1):
        try:
            _process_case(
                case=case,
                idx=idx,
                total=total,
                target=args.target,
                force=args.force,
                dry_run=args.dry_run,
                api=api,
                usage=usage,
                invoke_text=invoke_text,
                sources_done=sources_done,
                stats=stats,
            )
        except Exception as exc:  # noqa: BLE001 — one case must not abort the batch
            stats["llm_errors"] += 1
            print(f"Unhandled error processing case: {exc}", file=sys.stderr)
            if args.verbose:
                import traceback

                traceback.print_exc()

    print_summary(stats, args.dry_run, "Evidence/source description enrichment")
    if usage.calls > 0:
        print()
        print(
            render_usage_table(usage.as_dict()["by_provider"], title="evidence usage")
        )


def _process_case(
    case,
    idx,
    total,
    target,
    force,
    dry_run,
    api,
    usage,
    invoke_text,
    sources_done,
    stats,
):
    stats["cases_processed"] += 1
    case_id = case.get("case_id", "?")
    print(f"\n[{idx}/{total}] {case_id} — {case.get('title', '')[:80]}")

    try:
        detail = api.get_case(case.get("slug") or case_id)
    except Exception:  # noqa: BLE001
        detail = case
        print("  (using summary instead of detail)")

    slug = detail.get("slug") or case.get("slug")
    court_number = _special_court_number(detail)
    case_title = detail.get("title", "")
    allegations = detail.get("key_allegations")
    entities = detail.get("entities")

    evidence = detail.get("evidence") or []
    if not evidence:
        print("  No evidence — skipping")
        return

    writable = _writable_evidence(evidence)
    evidence_changed = False

    for pos, entry in enumerate(evidence):
        source_id = _resolve_source_id(entry)
        if not source_id:
            continue
        src = entry.get("source") or {}
        source_type = src.get("source_type") or "MISC"
        source_title = src.get("title") or ""
        urls = src.get("urls") or []
        evidence_desc = entry.get("description") or ""

        do_source = target in ("source", "both") and source_id not in sources_done
        do_evidence = target in ("evidence", "both")

        need_source = False
        if do_source:
            current_src_desc = ""
            try:
                current_src_desc = (api.get_source(source_id) or {}).get(
                    "description", ""
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("  source GET failed for %s: %s", source_id, exc)
            if force:
                need_source = True
            else:
                adequate, reason = judge_description_adequacy(
                    current_src_desc,
                    kind="document source description (what the document is)",
                    invoke_text=invoke_text,
                    usage=usage,
                    context=f"source_type={source_type}; title={source_title[:80]}",
                )
                need_source = not adequate
                if adequate:
                    sources_done.add(source_id)

        need_evidence = False
        if do_evidence:
            if force:
                need_evidence = True
            else:
                adequate, _ = judge_description_adequacy(
                    evidence_desc,
                    kind="evidence relevance note (a source's probative role for an allegation)",
                    invoke_text=invoke_text,
                    usage=usage,
                    context=f"case={case_title[:80]}",
                )
                need_evidence = not adequate

        if not need_source and not need_evidence:
            stats["entries_already_ok"] += 1
            continue

        source_text = _source_text(urls)
        if not source_text:
            stats["entries_no_content"] += 1
            print(f"  {source_id}: no convertible source content — skip")
            if do_source:
                sources_done.add(source_id)  # don't retry an empty source elsewhere
            continue

        try:
            result = _generate(
                source_type=source_type,
                source_title=source_title,
                case_title=case_title,
                court_number=court_number,
                allegations=allegations,
                entities=entities,
                source_text=source_text,
                need_source=need_source,
                need_evidence=need_evidence,
                invoke_text=invoke_text,
                usage=usage,
            )
        except Exception as exc:  # noqa: BLE001
            stats["llm_errors"] += 1
            print(f"  {source_id}: LLM failed: {exc}")
            continue
        if not result:
            stats["llm_errors"] += 1
            print(f"  {source_id}: no parseable result — skip")
            continue

        # ── source description (reusable) ──
        new_src = (result.get("source_description") or "").strip()
        if need_source and new_src:
            sources_done.add(source_id)
            print(f"  {source_id} SOURCE: {new_src[:140]}")
            if dry_run:
                print("    [DRY RUN] would PATCH /api/sources/")
            else:
                try:
                    api.patch_source(source_id, "description", new_src)
                    stats["sources_enriched"] += 1
                    print("    [SOURCE UPDATED]")
                except Exception as exc:  # noqa: BLE001
                    stats["llm_errors"] += 1
                    print(f"    source PATCH failed: {exc}")

        # ── evidence description (case-specific) ──
        new_ev = (result.get("evidence_description") or "").strip()
        if need_evidence and new_ev:
            print(f"  {source_id} EVIDENCE: {new_ev[:140]}")
            writable[pos]["description"] = new_ev
            evidence_changed = True

    if evidence_changed:
        if dry_run:
            print("  [DRY RUN] would PATCH /evidence")
        else:
            try:
                api.patch_field(slug, "evidence", writable)
                stats["evidence_enriched"] += 1
                print(f"  [EVIDENCE UPDATED] {case_id}")
            except Exception as exc:  # noqa: BLE001
                stats["llm_errors"] += 1
                print(f"  evidence PATCH failed: {exc}")


# ── source text acquisition (body only; not the existing evidence description) ──


def _source_text(urls: list) -> Optional[str]:
    """Convert a source's url list to document text: prefer a MARKDOWN-role link,
    else convert a RAW/ALTERNATE/SOURCE_PAGE link. Unlike
    common.content_from_evidence_entry this never falls back to the evidence
    description (that would be circular — we are writing descriptions ABOUT the
    document). Returns None when no sufficient text can be obtained."""
    if not urls:
        return None
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
                return text[:SOURCE_TEXT_BUDGET]
        except Exception:  # noqa: BLE001
            pass

    convertible = [
        u["link"]
        for u in urls
        if isinstance(u, dict) and u.get("link") and u.get("role") in CONVERTIBLE_ROLES
    ]
    if convertible:
        try:
            from sourcing import converter as source_converter

            result = source_converter.convert_source({"url": convertible})
            if result.get("status") in ("converted", "attached"):
                text = (result.get("markdown") or "").strip()
                if len(text) > 200:
                    return text[:SOURCE_TEXT_BUDGET]
        except Exception:  # noqa: BLE001
            pass
    return None


# ── LLM generation ────────────────────────────────────────────────────────────


def _generate(
    source_type,
    source_title,
    case_title,
    court_number,
    allegations,
    entities,
    source_text,
    need_source,
    need_evidence,
    invoke_text,
    usage,
) -> Optional[dict]:
    wanted = []
    if need_source:
        wanted.append("source_description")
    if need_evidence:
        wanted.append("evidence_description")
    targets_note = (
        "Produce BOTH descriptions."
        if len(wanted) == 2
        else f"Only the {wanted[0]} is needed (still return the JSON object with "
        "both keys; the other may be an empty string)."
    )
    prompt = USER_PROMPT.format(
        targets_note=targets_note,
        source_type=source_type,
        source_title=source_title or "(untitled)",
        case_title=case_title or "(unknown)",
        court_number=court_number or "(unknown)",
        allegations=_format_list(allegations),
        entities=_format_entities(entities),
        source_text=source_text,
    )
    response_text = invoke_text(
        system=SYSTEM_PROMPT,
        content=prompt,
        max_tokens=1500,
        tier="premium",
        usage=usage,
    )
    return _parse_response(response_text)


def _parse_response(response_text: str) -> Optional[dict]:
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
        if isinstance(obj, dict) and (
            "source_description" in obj or "evidence_description" in obj
        ):
            return obj
    logger.warning("No description JSON object found in LLM response")
    return None


# ── evidence helpers ──────────────────────────────────────────────────────────


def _resolve_source_id(entry: dict) -> Optional[str]:
    """Extract the string source_id from an evidence entry (the API may embed it
    as a dict, mirroring CaseDetailSerializer.resolve_source_id)."""
    sid = entry.get("source_id")
    if isinstance(sid, dict):
        return sid.get("source_id") or sid.get("link")
    return sid if isinstance(sid, str) and sid else None


def _writable_evidence(evidence: list) -> list[dict]:
    """Reduce read-shape evidence (each entry carries a nested `source`) to the
    writable [{source_id, description}] shape CasePatchSerializer expects,
    preserving order and existing descriptions."""
    out = []
    for entry in evidence:
        out.append(
            {
                "source_id": _resolve_source_id(entry) or "",
                "description": entry.get("description") or "",
            }
        )
    return out


def _special_court_number(case: dict) -> Optional[str]:
    for ref in case.get("court_cases") or []:
        if isinstance(ref, str) and ref.startswith("special:"):
            return ref.split(":", 1)[1]
    for ref in case.get("court_cases") or []:
        if isinstance(ref, str) and ":" in ref:
            return ref.split(":", 1)[1]
    return None


def _format_list(items) -> str:
    if not items:
        return "(none provided)"
    return "\n".join(f"- {x}" for x in items)


def _format_entities(entities) -> str:
    if not entities:
        return "(none provided)"
    lines = []
    for e in entities:
        if not isinstance(e, dict):
            continue
        name = e.get("display_name") or ""
        etype = e.get("type") or ""
        line = f"- [{etype}] {name}"
        if e.get("notes"):
            line += f" — {e['notes']}"
        lines.append(line)
    return "\n".join(lines) or "(none provided)"


if __name__ == "__main__":
    main()
