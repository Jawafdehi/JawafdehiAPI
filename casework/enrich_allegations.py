#!/usr/bin/env python
"""Extract CIAA Special Court case key allegations via LLM (DB-free). LOCAL WRITES ONLY.

Ported from the deleted `casework/enrich_allegations.py` (recovered at donor commit
`0321a85`). Reads a case's CIAA press-release source text entirely over the
Jawafdehi HTTP API and asks the premium LLM tier to extract 2-3 self-contained
Nepali allegation sentences. Never touches the database directly -- writes go
through `CaseworkApi.patch_field`, which this project's binding constraint
restricts to loopback (`127.0.0.1:48010`) only.

Writes exactly ONE field, `key_allegations`. `missing_details` is written by
`casework/enrich_description.py`, which has the verdict in hand; this stage only
ever reads the press release.

Usage:
    uv run python -m casework.enrich_allegations --dry-run
    uv run python -m casework.enrich_allegations --slug case-0123
    uv run python -m casework.enrich_allegations --limit 10 --verbose
    uv run python -m casework.enrich_allegations --apply
"""

import argparse
import logging
import re
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
from jawafdehi_shared.entities.ids import parse_courtcase_iri

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
11. End with the charge marker "भन्ने आरोप छ।" — a participle clause closed by that phrase, so the sentence reads as the CIAA's claim and not as a finding of fact

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
- End allegations with source-attribution phrases such as "उल्लेख छ", "भनिएको छ", "जनाइएको छ", or "देखिन्छ" — these attribute the sentence to the document rather than to the charge
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
गैरकानूनी रूपमा सम्पत्ति आर्जन गरेको भन्ने आरोप छ।"

Example 2 (Procurement fraud):
"प्रतिवादीहरूको मिलेमतोमा काठमाडौं महानगरपालिकाको NCBW-KMC को ठेक्कामा
Pending Litigation नहुने विषयलाई Pending Litigation रहेको भनी गलत मूल्याङ्कन
प्रतिवेदन खडा गरी सार्वजनिक सम्पत्ति बदनियतपूर्वक हानि नोक्सानी पुर्याएको भन्ने आरोप छ।"

Example 3 (Bribery and money laundering):
"मोहनबहादुर बस्नेतले नगर प्रमुख पदको दुरुपयोग गरी पद्मा कम्पनीहरू र राजु
प्रसाद कँडेललाई कर छुट र जग्गा उपलब्धता लगायत अनुचित लाभ पुर्याई सो बापत
करिब रु. ९.२२ करोड घुस/रिसवत लिएको भन्ने आरोप छ।"

Example 4 (Embezzlement):
"प्रतिवादीहरूको मिलेमतोमा हुलाक बचत बैङ्कमा बचतकर्ताहरूको निक्षेप रकम
बैङ्क दाखिला नगरी अपचलन गरी हिनामिना गरेको भन्ने आरोप छ।"
"""

USER_PROMPT_TEMPLATE = """Extract 2-3 key allegation statements from this CIAA press release.

Case title: {case_title}
Bigo amount: {bigo}

Instructions:
- Each allegation must be exactly one complete, self-contained sentence in Nepali
- End every allegation with "भन्ने आरोप छ।" — write the act as a participle clause and close with that phrase, as in "…गरेको भन्ने आरोप छ।"
- Do not use source-attribution wording such as "उल्लेख छ", "भनिएको छ", "जनाइएको छ", or "देखिन्छ"
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


# `tone.hedge_key_allegations` in work/slug-fix/enricher-fix-rules.json: a bare
# declarative reads as an established fact, and six of the ten cases the rule was
# proven on had ended in acquittal. The prompt asks for the marker; this enforces it.
HEDGE = " भन्ने आरोप छ।"
_ALREADY_HEDGED = ("भन्ने आरोप", "अभियोग दाबी")
# The participle ending, and only that: `ेको` (गरेको) plus the independent-vowel
# `एको` (लुकाएको, पुर्‍याएको). A bare `को` also matches the genitive (`सरकारको।`),
# where the marker is not Nepali. The terminator is optional and may be a danda,
# an ASCII full stop, or absent, and a trailing perfect copula (`गरेको छ।`) is
# absorbed -- HEDGE supplies its own छ.
_PARTICIPLE_END = re.compile(r"([ेए]को)(?:\s*छ)?\s*[।.]?\s*$")


def _hedge(text: str) -> str:
    """Close a participle allegation with the CIAA charge marker.

    Idempotent. A non-participle ending is left alone -- force-suffixing one
    produces ungrammatical Nepali. A perfect (`…गरेको छ।`) asserts guilt as
    plainly as the bare participle, so it is hedged too; the cost is that a
    neutral perfect (`…कायम भएको छ।`) is marked as a claim as well, which stays
    true because the sentence is the CIAA's in the first place.
    """
    t = text.rstrip()
    if any(m in t for m in _ALREADY_HEDGED) or not _PARTICIPLE_END.search(t):
        return t
    return _PARTICIPLE_END.sub(r"\1" + HEDGE, t)


# `tone.append_acquittal_line` in work/slug-fix/enricher-fix-rules.json:
# key_allegations renders standalone on some surfaces, so on a case the court
# cleared it reads as an unqualified guilt narrative on its own.
ACQUITTAL_LINE = (
    "माथि उल्लिखित कुराहरू अख्तियार दुरुपयोग अनुसन्धान आयोगको अभियोग दाबी हुन्; "
    "विशेष अदालतले उक्त दाबी पुग्न नसकी {defendants} आरोपित कसुरबाट सफाइ दिने "
    "ठहर गरेको छ।"
)
# Phrases, not the bare morpheme: `सफाइ` is a substring of `सरसफाइ`
# (sanitation), a routine CIAA contract subject, so a morpheme guard suppressed
# the line on exactly the acquitted cases the rule was written for.
_ACQUITTAL_MARKERS = ("सफाइ दिने", "सफाइ दिएको", "सफाइ पाएको",
                      "सफाई दिने", "सफाई दिएको", "सफाई पाएको")
_SPECIAL_COURT = "special"


def _accused_binds(detail: dict) -> list:
    """The case's accused binds -- `outcome` is meaningful only on those."""
    return [
        e for e in (detail.get("entities") or [])
        if isinstance(e, dict) and (e.get("type") or "").strip() == "accused"
    ]


def _other_courts(detail: dict) -> list:
    """Courts the case reached that are not the Special Court, from `court_cases`."""
    others = []
    for ref in detail.get("court_cases") or []:
        try:
            court = parse_courtcase_iri(ref).court
        except (ValueError, TypeError):
            continue
        if court != _SPECIAL_COURT:
            others.append(court)
    return others


def _append_acquittal_line(detail: dict, allegations: list) -> tuple:
    """Close the allegations with the acquittal when the court cleared every bound accused.

    Returns the (possibly unchanged) list and a reason for the run ledger. The
    verdict comes from the accused binds' `outcome` and nothing else -- not the
    title, not the prose. Anything short of a unanimous `acquitted` is left
    alone: a partial conviction, an abatement or a still-`charged` defendant all
    make a blanket acquittal line false. Binds are curator-created and are NOT
    guaranteed to cover every defendant, which is why `main()` logs the count.
    """
    accused = _accused_binds(detail)
    if not accused:
        return allegations, "no-accused-bind"
    if {(e.get("outcome") or "").strip() for e in accused} != {"acquitted"}:
        return allegations, "not-unanimous"
    others = _other_courts(detail)
    if others:
        # `RelationshipOutcome`'s terminal values are set from *a* primary court
        # order, which on an appealed case can be the appeal court's. The
        # enricher cannot tell which order set the outcome, so it refuses rather
        # than risk stating the opposite of what the Special Court ruled.
        return allegations, "other-court:" + ",".join(sorted(set(others)))
    if any(m in a for a in allegations for m in _ACQUITTAL_MARKERS):
        return allegations, "already-stated"
    defendants = "प्रतिवादीलाई" if len(accused) == 1 else "प्रतिवादीहरूलाई"
    return [*allegations, ACQUITTAL_LINE.format(defendants=defendants)], "appended"


def _parse_allegations_response(response_text: str) -> Optional[list]:
    """Parse the LLM response into clean allegations (at most 3)."""
    entries = parse_extraction_response(response_text, {"allegations"})
    if not entries:
        return None
    clean = [
        _hedge(str(a).strip())
        for a in entries
        if isinstance(a, str) and a.strip()
    ]
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
    except Exception as exc:  # noqa: BLE001 - bootstrap failure is reported and exits(1)
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

        allegations, acquittal = _append_acquittal_line(detail, allegations)

        log_event(logger, paths["events"], run_id=run_id, stage="allegations", slug=slug,
                  step="extract", status="ok",
                  detail=(f"key_allegations={allegations} "
                          f"accused_binds={len(_accused_binds(detail))} "
                          f"acquittal_line={acquittal}"))

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
