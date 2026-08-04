#!/usr/bin/env python
"""Extract CIAA Special Court case key allegations via LLM (DB-free). LOCAL WRITES ONLY.

Ported from the deleted `casework/enrich_allegations.py` (recovered at donor commit
`0321a85`). Reads a case's CIAA press-release source text entirely over the
Jawafdehi HTTP API and asks the premium LLM tier to extract 2-3 self-contained
Nepali allegation sentences. Never touches the database directly -- writes go
through `CaseworkApi.patch_field`, which this project's binding constraint
restricts to loopback (`127.0.0.1:48010`) only.

CONCERN -- phantom `missing_details` field (flagged for the dispatcher, not
silently "fixed"): `STAGES["allegations"]` in `casework/common/pipeline.py`
declares `provides=("key_allegations", "missing_details")`, and the task-14
brief accordingly asked for a `normalise_missing_details(value) -> str | None`
helper. Neither exists anywhere in the donor: `git log --all -p -- casework/
enrich_allegations.py` never mentions `missing_details` in the DB-free script's
history, and the 367-line donor at 0321a85 (read in full) writes exactly ONE
field via `api.patch_field(case_slug, "key_allegations", allegations)`. The
`provides` tuple traces back to `task-11-brief.md` (written before this
donor was ever recovered for a Task-12-14 dispatch), not to any donor
behavior. `missing_details` IS a real `Case` field (see `cases/models.py`,
`cases/services/ciaa_draft_case_service.py`, and the OLD, still-live DB-based
`cases/management/commands/enrich_ciaa_allegations.py`), but this DB-free
enricher never wrote it, before or after deletion. Per this task's explicit
"donor is the source of truth" instruction, this port writes ONLY
`key_allegations` and does NOT invent a `normalise_missing_details` helper.
`STAGES["allegations"].provides` is left uncorrected (out of this task's
Create-file scope -- it is shared Task-11 infrastructure) but should be
revisited: a future idempotency check that requires ALL `provides` fields to
be populated before considering this stage "done" would never see it as
complete.

The donor's own prompt-context construction is also narrower than
`enrich_missing_bigo.py`'s `_source_metadata`: the donor's
`USER_PROMPT_TEMPLATE` only ever includes the case title, the bigo display
string, and the full press-release markdown body -- it never built a
DocumentSource-title/description prompt block the way the bigo donor did, so
there is no analogous metadata-surfacing helper to port here.

Usage:
    uv run python -m casework.enrich_allegations --dry-run
    uv run python -m casework.enrich_allegations --slug case-0123
    uv run python -m casework.enrich_allegations --limit 10 --verbose
    uv run python -m casework.enrich_allegations --apply
"""

import argparse
import logging
import sys
import time
from typing import Optional

from casework.common.api import CaseworkApi
from casework.common.cli import (
    add_common_args,
    basic_auth_from_env,
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
from casework.common.pipeline import PRESS_TYPES, STAGES, RunReport, unmet_prerequisites
from casework.common.select import select_for_run

log = logging.getLogger("casework.enrich_allegations")

STAGE = STAGES["allegations"]

# The donor read this feed limit from `CASEWORK_PRESS_RELEASE_CHARS` via an
# `env_int()` helper that lived in the deleted `casework/common.py` and was
# never re-created in the Task 5-11 common package (see
# `enrich_missing_bigo.py`'s identical note -- no env-configurable knob exists
# anywhere else in `casework.common`). Fixed at the donor's own default.
PRESS_RELEASE_CHARS = 60000

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


def _clamp(text: str, limit: int, label: str = "source") -> str:
    """Truncate `text` to `limit` chars (<=0 = no limit) and PRINT total vs sent,
    matching `enrich_missing_bigo.py`'s `_clamp` convention (an operator can see
    how much of each source actually reached the model)."""
    text = text or ""
    total = len(text)
    sent = text if (limit <= 0 or total <= limit) else text[:limit]
    note = "" if len(sent) == total else f"  (capped at {limit:,})"
    print(f"    {label}: {total:,} total chars, sent {len(sent):,}{note}")
    return sent


def _extract_allegations(
    press_release_text: str,
    case_title: str,
    bigo: str,
    invoke_text,
    usage,
) -> Optional[list]:
    """Call the LLM (without tools) to extract allegations from press release."""
    prompt = USER_PROMPT_TEMPLATE.format(
        case_title=case_title,
        bigo=bigo,
        press_release=_clamp(press_release_text, PRESS_RELEASE_CHARS, "press release"),
    )

    response_text = invoke_text(
        system=SYSTEM_PROMPT,
        content=prompt,
        max_tokens=2000,
        tier=tier_for("allegations"),
        usage=usage,
    )

    return _parse_allegations_response(response_text)


def _parse_allegations_response(response_text: str) -> Optional[list]:
    """Parse the LLM response into clean allegations (at most 3)."""
    entries = parse_extraction_response(response_text, {"allegations"})
    if not entries:
        return None
    clean = [str(a).strip() for a in entries if isinstance(a, str) and a.strip()]
    return clean[:3] if clean else None


def build_api(args):
    """Construct the client. Basic (local DEV_AUTH) unless a token is given."""
    if args.api_token:
        return CaseworkApi(
            args.api_base_url, token=args.api_token,
            allow_remote_writes=args.allow_remote_writes,
        )
    return CaseworkApi(
        args.api_base_url,
        basic=basic_auth_from_env(),
        allow_remote_writes=args.allow_remote_writes,
    )


def main(argv=None):
    """Main entry point."""
    ap = argparse.ArgumentParser(
        description="Extract CIAA Special Court case key allegations via LLM (DB-free).",
        epilog="Reads/writes cases entirely over the Jawafdehi HTTP API.",
    )
    add_common_args(ap)
    args = ap.parse_args(argv)

    setup_logging(args.verbose)
    logger, run_id, paths = configure_run_logging("allegations", verbose=args.verbose)
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
    cases = select_for_run(all_cases, args)

    total = len(cases)
    log_run_header(
        logger, stage="allegations", base_url=args.api_base_url, dry_run=args.dry_run,
        provider=args.provider, model=args.model, n_selected=total,
        run_id=run_id, paths=paths,
    )
    if total == 0:
        print("No matching CIAA case(s) to process.", file=sys.stderr)
        print_summary(report.summary(), args.dry_run, "Allegation extraction")
        log_run_footer(
            logger, stage="allegations", stats=report.summary(),
            duration_s=time.monotonic() - start_time,
        )
        return report

    print(f"Found {total} matching case(s).")
    if args.force:
        print("  --force: re-generating even for populated cases")
    if args.dry_run:
        print("  [DRY RUN] No changes will be saved.")

    for idx, case in enumerate(cases, 1):
        slug = case.get("slug") or "?"
        # Donor-preserved: the prompt's `case_title` comes from the LIST-shaped
        # `case` dict captured here, NOT from `detail` fetched below -- the
        # donor's `_process_case` reads `title = case.get("title", "")` before
        # the detail fetch and passes that same `title` to `_extract_allegations`,
        # never `detail.get("title")`.
        title = case.get("title") or ""
        log_event(logger, paths["events"], run_id=run_id, stage="allegations", slug=slug,
                  step="start", status="start", detail=f"[{idx}/{total}] {title[:80]}")

        if case.get("key_allegations") and not args.force:
            report.record(
                slug, "allegations", "already",
                f"key_allegations already {case['key_allegations']}")
            log_event(logger, paths["events"], run_id=run_id, stage="allegations", slug=slug,
                      step="idempotency", status="already",
                      detail=f"key_allegations already {case['key_allegations']}")
            continue

        # Donor-preserved fallback: a detail-fetch failure does not abort the
        # case -- the donor caught `requests.HTTPError` and fell back to the
        # LIST-shaped `case` dict (`detail = case`). `CaseworkApi` raises
        # urllib errors, not `requests.HTTPError`, so this is widened to
        # `Exception` for the new HTTP client rather than silently never
        # firing. A LIST-shaped fallback still yields a well-formed "unmet"
        # reason below (unresolved material), never a crash or fabricated
        # content.
        try:
            detail = api.get_case(slug)
        except Exception as exc:  # noqa: BLE001 - detail-fetch failure falls back to the LIST-shaped case
            detail = case
            log_event(logger, paths["events"], run_id=run_id, stage="allegations", slug=slug,
                      step="fetch", status="fallback", detail=str(exc),
                      level=logging.WARNING)

        unmet = unmet_prerequisites(STAGE, detail)
        if unmet:
            for reason in unmet:
                report.record(slug, "allegations", "unmet", reason)
            log_event(logger, paths["events"], run_id=run_id, stage="allegations", slug=slug,
                      step="prereq", status="unmet", detail="; ".join(unmet),
                      level=logging.WARNING)
            continue

        text, text_unmet = source_text(detail, types=PRESS_TYPES)
        if not text.strip():
            reasons = text_unmet or ["no press-release source text"]
            for reason in reasons:
                report.record(slug, "allegations", "unmet", reason)
            log_event(logger, paths["events"], run_id=run_id, stage="allegations", slug=slug,
                      step="source", status="unmet", detail="; ".join(reasons),
                      level=logging.WARNING)
            continue

        log_event(logger, paths["events"], run_id=run_id, stage="allegations", slug=slug,
                  step="source", status="ok", detail=f"{len(text)} chars")

        # Donor-verbatim bigo display formatting.
        bigo = detail.get("bigo")
        bigo_display = f"रु {bigo:,}" if bigo else "उल्लेख छैन"

        try:
            allegations = _extract_allegations(
                press_release_text=text,
                case_title=title,
                bigo=bigo_display,
                invoke_text=invoke_text,
                usage=usage,
            )
        except Exception as exc:  # noqa: BLE001 - per-case LLM failure is recorded, run continues
            report.record(slug, "allegations", "error", f"LLM extraction failed: {exc}")
            log_event(logger, paths["events"], run_id=run_id, stage="allegations", slug=slug,
                      step="extract", status="error", detail=str(exc),
                      level=logging.ERROR)
            if args.verbose:
                import traceback

                traceback.print_exc()
            continue

        if not allegations:
            report.record(slug, "allegations", "skipped", "LLM returned no allegations")
            log_event(logger, paths["events"], run_id=run_id, stage="allegations", slug=slug,
                      step="extract", status="skipped",
                      detail="LLM returned no allegations", level=logging.WARNING)
            continue

        log_event(logger, paths["events"], run_id=run_id, stage="allegations", slug=slug,
                  step="extract", status="ok", detail=f"key_allegations={allegations}")

        if args.dry_run:
            report.record(
                slug, "allegations", "would-enrich", f"key_allegations={allegations}")
            log_event(logger, paths["events"], run_id=run_id, stage="allegations", slug=slug,
                      step="write", status="would-enrich",
                      detail=f"key_allegations={allegations}")
            continue

        try:
            api.patch_field(slug, "key_allegations", allegations)
            report.record(
                slug, "allegations", "enriched", f"key_allegations={allegations}")
            log_event(logger, paths["events"], run_id=run_id, stage="allegations", slug=slug,
                      step="write", status="enriched",
                      detail=f"key_allegations={allegations}")
        except Exception as exc:  # noqa: BLE001 - per-case PATCH failure is recorded, run continues
            report.record(slug, "allegations", "error", f"PATCH failed: {exc}")
            log_event(logger, paths["events"], run_id=run_id, stage="allegations", slug=slug,
                      step="write", status="error", detail=str(exc),
                      level=logging.ERROR)

    stats = report.summary()
    print_summary(stats, args.dry_run, "Allegation extraction")
    unmet_reasons = report.unmet_reasons()
    if unmet_reasons:
        print("  unmet reasons:")
        for reason, count in unmet_reasons.most_common():
            print(f"    {count} x {reason}")

    usage_summary = ""
    if usage.calls > 0:
        usage_summary = render_usage_table(
            usage.as_dict()["by_provider"], title="allegations usage")
        print()
        print(usage_summary)

    log_run_footer(
        logger, stage="allegations", stats=stats,
        duration_s=time.monotonic() - start_time, usage_summary=usage_summary,
    )

    return report


if __name__ == "__main__":
    main()
