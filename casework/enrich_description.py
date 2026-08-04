#!/usr/bin/env python
"""Write the public case narrative `Case.description` via LLM (DB-free). LOCAL WRITES ONLY.

Ported from the deleted `casework/enrich_description.py` (recovered at donor commit
`0321a85`, 619 lines). Reads a case's charge sheet, CIAA press release and Special
Court verdict entirely over the Jawafdehi HTTP API and asks the premium LLM tier for
the अभियोगदावी / बयान / फैसला structure of https://github.com/Jawafdehi/JawafdehiAPI/issues/199.
Never touches the database directly -- writes go through `CaseworkApi.patch_field`,
which this project's binding constraint restricts to loopback (`127.0.0.1:48010`).

It is the field production is emptiest on: 188 of 3,003 cases carry a description.

ONE FIELD. This script writes `description` and NOTHING else -- see the three
deliberate deviations from the donor below.

DEVIATION 1 -- THE DONOR'S TITLE PASS IS DROPPED. The donor regenerated
`Case.title` in the same LLM call, gated by `--skip-title`, because it had the
source documents in hand and the title came free. `title` now has exactly one
owner, `casework/enrich_card.py`. So `--skip-title`, the `TITLE_RULES` import from
`casework/common/titles.py`, `validate_title`, `title_has_headcount` and the
`"title"` key in the response contract are all gone, and `STAGES["description"]
.provides` is `("description",)` alone. What that buys: one prompt holding the
title rules instead of two, one answer to "why does this case have this title",
and a title that can be regenerated without re-fetching charge sheets. What it
costs: one extra cheap LLM call per case, since `enrich_card` reads the
`description` this script just wrote. `test_never_writes_title` pins it.

DEVIATION 2 -- `invoke_text`, NOT the donor's `invoke_with_tools` +
`convert_date` tool. Two reasons, in order of weight:
  1. The donor passed the tool but never told the model it existed. Compare
     `enrich_timeline.py`'s `EXTRACTION_SYSTEM_PROMPT`, which spends a 12-line
     "DATE CONVERSION TOOL (MANDATORY)" block on it, against the donor's
     description prompt, which mentions dates only under QUALITY RULES and never
     names the tool. An unadvertised tool in a prose-generation call is a
     tool-loop the model has no reason to enter.
  2. This stage emits prose, not structured dates. Every date reaching it is
     already converted -- the FACTUAL TIMELINE block carries `date` (AD) and
     `date_bs` written by `enrich_timeline`, which DID use the tool -- or is
     copied verbatim from a source document, where the correct behaviour is to
     leave the BS date exactly as written.
A single-turn call is also the cheaper shape: `invoke_with_tools` bills every
turn of the loop, and this is the most expensive call in the casework pipeline.
The safety pairing for removing the tool is one added QUALITY RULE forbidding the
model from converting dates itself; without it, dropping the tool would invite
exactly the silent BS<->AD arithmetic error the tool existed to prevent.

DEVIATION 3 -- THE DONOR'S NGM SECTION IS NOT PORTED. The donor fetched
`GET /ngm/court_case/{special:NNN-CR-NNNN}` and formatted it as a prompt block.
That path is doubly dead and `casework/enrich_timeline.py` documents the
measurements: the colon-prefixed `special:` reference it scans for matches 0 of
109 real `court_cases` entries (they are full IRIs), and the endpoint itself was
removed in the 2026-07-01 hard cut of `config/urls.py`. `enrich_timeline` kept its
copy dead-but-intact for a port-vs-donor A/B that this task has no part in;
porting an unreachable HTTP call a second time would add a code path no run can
enter. Prompt-identical on current data either way: the donor's `{ngm_section}`
renders to the empty string whenever the fetch returns None, which is always.

Usage:
    uv run python -m casework.enrich_description --dry-run
    uv run python -m casework.enrich_description --slug case-0123
    uv run python -m casework.enrich_description --limit 3 --verbose
    uv run python -m casework.enrich_description --apply
"""

import argparse
import json
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
from casework.common.format import format_bigo, format_entities, format_list
from casework.common.llm import bootstrap, tier_for
from casework.common.materials import source_chunks
from casework.common.parse import parse_object_response
from casework.common.pipeline import (
    COURT_TYPES,
    PRESS_TYPES,
    STAGES,
    RunReport,
    unmet_prerequisites,
)
from casework.common.review import ReviewRow, build_review_file
from casework.common.select import court_number, select_cases

# `summarize_verdict` and its four donor-pinned constants are imported from the
# timeline enricher rather than re-copied. The donor kept them in the shared
# `casework/common.py`; on `main` they landed in `enrich_timeline.py` only
# because that was the sole enricher in their porting task's scope, and
# `tests/casework/test_enrich_timeline.py` now asserts each constant against
# the donor source. A second copy here would be four more numbers free to
# drift from that pin. Their proper home is `casework/common/`; moving them is
# a separate change, not this port's business.
from casework.enrich_timeline import (
    VERDICT_SUMMARY_TARGET,
    VERDICT_SUMMARY_TRIGGER,
    summarize_verdict,
)

log = logging.getLogger("casework.enrich_description")

STAGE = STAGES["description"]

# Source types ordered by usefulness for the description, richest first --
# the donor's `DESCRIPTION_SOURCE_TYPES` mapped onto the current material
# vocabulary (`casework/common/pipeline.py`). The donor's AG_ABHIYOG_PATRA is
# today's `charge_sheet` and it stays first: it is the prosecution claim
# verbatim, which is what section क is. The verdict comes last because it is
# also the one that gets summarised rather than passed through whole.
DESCRIPTION_SOURCE_ORDER = ("charge_sheet", "ciaa_press_release", "press_release",
                            "court_order")

# The donor read this from `CASEWORK_SOURCE_TEXT_BUDGET` via an `env_int()`
# helper that lived in the deleted `casework/common.py` and was never
# re-created in the common package -- same treatment as every sibling
# enricher (see `enrich_allegations.py`'s identical note). Fixed at the
# donor's own default.
SOURCE_TEXT_BUDGET = 60000
DESCRIPTION_MAX_TOKENS = 8000

# Donor-verbatim idempotency threshold (`_has_substantial_description`): a
# description shorter than this is a stub, not a description. Content-based on
# purpose -- an emptiness test would treat a one-line template stub as done.
SUBSTANTIAL_DESCRIPTION_CHARS = 600

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
for each defendant (दोषी / सफाई), with the बिगो/sentence and the court's key
reasoning. Do NOT include procedural or registry orders — the appeal म्याद
(e.g. "३५ दिनभित्र पुनरावेदन गर्न"), धरौटी सदर/फिर्ता, लगत कायम, and similar routine
"अन्य आदेश" are court-procedure, not the substantive फैसला; leave them out. A
विशेष अदालत ruling does NOT set precedent, so do not call its reasoning a नजिर here.

### घ) पुनरावेदनको सार
Only if the sources or a supreme-court reference show an appeal was ACTUALLY
filed: the grounds and legal basis of the appeal and who filed it. The routine
appeal म्याद granted in the verdict (e.g. "३५ दिनभित्र पुनरावेदन गर्न जाने") is NOT
an appeal — do not emit this section for it; OMIT the section entirely unless an
appeal was really lodged.

### ङ) सर्वोच्च अदालतको फैसलाको सार
Only if a Supreme Court judgment is in the sources: date, bench, and final
outcome.

### च) नजिरको सार
Include ONLY when a सर्वोच्च अदालत (Supreme Court) judgment in the sources
establishes a legal principle — ideally one published in the Nepal Kanoon Patrika
(नेपाल कानून पत्रिका). A विशेष अदालत (Special Court) decision is NEVER a precedent;
if no qualifying Supreme Court principle is in the sources, OMIT this section
entirely. State only the key principle.

QUALITY RULES:
- Ground every sentence in the provided sources/case data. Do NOT fabricate
  names, amounts, section numbers, dates, benches, or outcomes. If the verdict is
  not in the sources, write section ग only to the extent the timeline/NGM data
  supports (e.g. "मिति … मा फैसला भएको") and omit unknown specifics.
- Prefer specifics from the documents (exact बिगो, दफा, र.नं./नि.नं., dates,
  named officials) over vague phrasing.
- Use the बिगो figure provided in the case data as the headline amount.
- DATES: write every date exactly as its source states it — a BS date stays in
  BS, as written. Do NOT convert between BS (Bikram Sambat) and AD yourself.
  Such a conversion done in your head is wrong by days or months often enough
  that the converted date is a fabricated fact. The FACTUAL TIMELINE below
  already carries both the AD date and the BS date where you need the pair.
- This is an official public record drawn from government/court documents; do not
  soften, editorialise, or add commentary. Neutral, factual tone only.

OUTPUT FORMAT — return ONLY a single JSON object, no markdown fences, no prose:
{"description": "### क) …\\n…"}
"""

EXTRACTION_USER_PROMPT = """\
Write the Jawafdehi case description for the following CIAA Special Court case.

Case title: {case_title}
Special-court case number: {court_number}
Bigo (बिगो), NPR: {bigo}
Court case references: {court_cases}

KEY ALLEGATIONS (already curated for this case):
{key_allegations}

FACTUAL TIMELINE (already curated; dates are reliable — use for section ग etc.):
{timeline}

NAMED ENTITIES (accused / related / location):
{entities}

SOURCE DOCUMENTS (press release, charge sheet, verdict — the factual basis for
the description; quote specifics from here):

{source_text}

Return ONLY the JSON object described in the system prompt.
"""


def _clamp(text: str, limit: int, label: str = "source") -> str:
    """Truncate `text` to `limit` chars (<=0 = no limit) and PRINT total vs sent,
    matching the convention every sibling enricher already follows (an operator
    can see how much of each source actually reached the model). Kept local, as
    in `enrich_allegations.py` / `enrich_timeline.py` / `enrich_missing_bigo.py`
    -- consolidating the four copies into `casework/common/` is a separate
    change that would touch three enrichers this port has no reason to edit."""
    text = text or ""
    total = len(text)
    sent = text if (limit <= 0 or total <= limit) else text[:limit]
    note = "" if len(sent) == total else f"  (capped at {limit:,})"
    print(f"    {label}: {total:,} total chars, sent {len(sent):,}{note}")
    return sent


def _ordered_sources(chunks):
    """Sort `source_chunks` triples into `DESCRIPTION_SOURCE_ORDER`.

    A material type outside that list keeps its original relative position at
    the end rather than being dropped -- an unexpected type is still evidence.
    """
    def key(item):
        mtype = item[0]
        return (DESCRIPTION_SOURCE_ORDER.index(mtype)
                if mtype in DESCRIPTION_SOURCE_ORDER
                else len(DESCRIPTION_SOURCE_ORDER))

    return sorted(chunks, key=key)


def _allocate_budget(sizes, budget):
    """Chars each source may spend, max-min fair. Returns a list parallel to `sizes`.

    WHY NOT GREEDY. The previous version walked the sources in PRIORITY order and
    gave each one whatever was left, so one huge document consumed the whole
    budget and every later source was dropped outright. Measured on
    `081-CR-0138`: a 529,947-char charge sheet took all 60,000 and the case's
    4,185-char press release -- 7% of the budget, and the most concise account of
    the case that exists -- was dropped entirely. The model saw 11% of one
    document and nothing of the other.

    Allocating smallest-first fixes that without a magic reserve: each source
    claims an equal share of what remains, and a source smaller than its share
    returns the surplus to the pool for the larger ones. Small documents are
    therefore never starved, and the big ones still absorb everything left over
    -- with two sources and a 60,000 budget the press release takes its 4,185 and
    the charge sheet gets the remaining 55,815.
    """
    allowance = [0] * len(sizes)
    remaining = budget
    # Ascending, so the smallest claims first and its surplus flows upward.
    for n, i in enumerate(sorted(range(len(sizes)), key=lambda i: sizes[i])):
        left = len(sizes) - n
        take = min(sizes[i], remaining // left)
        allowance[i] = take
        remaining -= take
    return allowance


def _assemble_source_text(chunks, invoke_text, usage):
    """Build the source-document block within SOURCE_TEXT_BUDGET.

    Returns `(prompt_block, fed_sources)` where `fed_sources` is the
    `[(label, material_iri, text)]` actually sent to the model -- the review
    file prints those, so it must reflect the post-summarisation,
    post-truncation reality rather than what was fetched.

    A long verdict is SUMMARISED rather than head-truncated: the ठहर sits at
    the end of a फैसला, so a head clamp is exactly the truncation that drops
    the outcome section ग exists to report.
    """
    prepared = []
    for mtype, iri, text in _ordered_sources(chunks):
        if mtype in COURT_TYPES and len(text) > VERDICT_SUMMARY_TRIGGER:
            summary = summarize_verdict(text, invoke_text, usage)
            if summary:
                log.info("Verdict summarised: %d -> %d chars", len(text), len(summary))
                prepared.append((f"{mtype} (फैसला सारांश)", iri, summary))
                continue
            # Summary failed -- fall back to the donor's truncated head.
            prepared.append((mtype, iri, text[:VERDICT_SUMMARY_TARGET]))
            continue
        prepared.append((mtype, iri, text))

    parts, fed = [], []
    allowance = _allocate_budget([len(text) for _, _, text in prepared],
                                SOURCE_TEXT_BUDGET)
    for i, (label, iri, text) in enumerate(prepared):
        if allowance[i] <= 0:
            log.warning("Source budget spent; dropped a %s source", label)
            continue
        chunk = _clamp(text, allowance[i], label)
        parts.append(f"[{label}]\n{chunk}")
        fed.append((label, iri, chunk))
    return "\n\n---\n\n".join(parts), fed


def _generate_description(detail, court_number, source_text, invoke_text, usage):
    """One premium-tier call. Returns the description string, or None."""
    prompt = EXTRACTION_USER_PROMPT.format(
        case_title=detail.get("title") or "",
        court_number=court_number or "(unknown)",
        bigo=format_bigo(detail.get("bigo")),
        court_cases=", ".join(detail.get("court_cases") or []) or "(none)",
        key_allegations=format_list(detail.get("key_allegations")),
        timeline=json.dumps(detail.get("timeline") or [], ensure_ascii=False),
        entities=format_entities(detail.get("entities")),
        source_text=source_text,
    )
    response_text = invoke_text(
        system=EXTRACTION_SYSTEM_PROMPT,
        content=prompt,
        max_tokens=DESCRIPTION_MAX_TOKENS,
        tier=tier_for("description"),
        usage=usage,
    )
    return _parse_description_response(response_text)


def _parse_description_response(response_text: str) -> Optional[str]:
    """Pull `description` out of the JSON object reply.

    A `title` key in the reply is IGNORED, not written -- see deviation 1. The
    model can still emit one (the OUTPUT FORMAT block does not ask for it, but
    models volunteer keys); silently dropping it here is what makes the
    single-owner rule hold even against a chatty response.
    """
    obj = parse_object_response(response_text, "description")
    if obj is None:
        log.warning("No JSON object with a description found in the LLM response")
        return None
    description = (obj.get("description") or "").strip()
    return description or None


def _has_substantial_description(case: dict) -> bool:
    """Donor-verbatim: a description at/over the threshold counts as done."""
    return len((case.get("description") or "").strip()) >= SUBSTANTIAL_DESCRIPTION_CHARS


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
        description="Write CIAA Special Court case descriptions via LLM (DB-free).",
        epilog="Reads/writes cases entirely over the Jawafdehi HTTP API.",
    )
    add_common_args(ap)
    args = ap.parse_args(argv)

    setup_logging(args.verbose)
    logger, run_id, paths = configure_run_logging("description", verbose=args.verbose)
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
    review = build_review_file(
        args, stage="description", field_name="description", run_id=run_id)

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
        logger, stage="description", base_url=args.api_base_url, dry_run=args.dry_run,
        provider=args.provider, model=args.model, n_selected=total,
        run_id=run_id, paths=paths,
    )
    if total == 0:
        print("No matching CIAA case(s) to process.", file=sys.stderr)
        print_summary(report.summary(), args.dry_run, "Description generation")
        print(f"review file: {review.write()}")
        log_run_footer(
            logger, stage="description", stats=report.summary(),
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
        title = case.get("title") or ""
        log_event(logger, paths["events"], run_id=run_id, stage="description", slug=slug,
                  step="start", status="start", detail=f"[{idx}/{total}] {title[:80]}")

        # `get_case_with_etag` in place of `get_case`: the ETag is echoed back as
        # `If-Match` on the PATCH below. A description is the most expensive
        # output in the pipeline, so losing it to a concurrent caseworker edit
        # matters more here than anywhere else -- a 412 means re-read and retry,
        # where an unconditional write would silently clobber the other writer.
        etag = None
        try:
            detail, etag = api.get_case_with_etag(slug)
        except Exception as exc:
            # Donor-preserved fallback: a detail-fetch failure does not abort the
            # case. The LIST-shaped payload still yields a well-formed "unmet"
            # reason below (unresolved material), never a crash. Widened from the
            # donor's `requests.HTTPError` because `CaseworkApi` is urllib-based.
            detail = case
            log_event(logger, paths["events"], run_id=run_id, stage="description",
                      slug=slug, step="fetch", status="fallback", detail=str(exc),
                      level=logging.WARNING)

        before = (detail.get("description") or "").strip()

        if _has_substantial_description(detail) and not args.force:
            reason = f"description already {len(before):,} chars"
            report.record(slug, "description", "already", reason)
            review.add(ReviewRow(slug=slug, status="already", before=before, note=reason))
            log_event(logger, paths["events"], run_id=run_id, stage="description",
                      slug=slug, step="idempotency", status="already", detail=reason)
            continue

        unmet = unmet_prerequisites(STAGE, detail)
        if unmet:
            for reason in unmet:
                report.record(slug, "description", "unmet", reason)
            review.add(ReviewRow(slug=slug, status="unmet", before=before,
                                 note="; ".join(unmet)))
            log_event(logger, paths["events"], run_id=run_id, stage="description",
                      slug=slug, step="prereq", status="unmet", detail="; ".join(unmet),
                      level=logging.WARNING)
            continue

        chunks, text_unmet = source_chunks(detail, types=PRESS_TYPES + COURT_TYPES)
        if not chunks:
            reasons = text_unmet or ["no press-release or court-order source text"]
            for reason in reasons:
                report.record(slug, "description", "unmet", reason)
            review.add(ReviewRow(slug=slug, status="unmet", before=before,
                                 note="; ".join(reasons)))
            log_event(logger, paths["events"], run_id=run_id, stage="description",
                      slug=slug, step="source", status="unmet",
                      detail="; ".join(reasons), level=logging.WARNING)
            continue

        log_event(logger, paths["events"], run_id=run_id, stage="description", slug=slug,
                  step="source", status="ok",
                  detail=f"{len(chunks)} source(s): "
                         + ", ".join(f"{t}({len(x):,})" for t, _, x in chunks))

        try:
            source_block, fed = _assemble_source_text(chunks, invoke_text, usage)
        except Exception as exc:
            report.record(slug, "description", "error", f"source assembly failed: {exc}")
            review.add(ReviewRow(slug=slug, status="error", before=before,
                                 note=f"source assembly failed: {exc}"))
            log_event(logger, paths["events"], run_id=run_id, stage="description",
                      slug=slug, step="assemble", status="error", detail=str(exc),
                      level=logging.ERROR)
            continue

        try:
            description = _generate_description(
                detail=detail,
                court_number=court_number(detail),
                source_text=source_block,
                invoke_text=invoke_text,
                usage=usage,
            )
        except Exception as exc:
            report.record(slug, "description", "error", f"LLM generation failed: {exc}")
            review.add(ReviewRow(slug=slug, status="error", before=before, sources=fed,
                                 note=f"LLM generation failed: {exc}"))
            log_event(logger, paths["events"], run_id=run_id, stage="description",
                      slug=slug, step="generate", status="error", detail=str(exc),
                      level=logging.ERROR)
            if args.verbose:
                import traceback

                traceback.print_exc()
            continue

        if not description:
            report.record(slug, "description", "skipped", "LLM returned no description")
            review.add(ReviewRow(slug=slug, status="skipped", before=before, sources=fed,
                                 note="LLM returned no description"))
            log_event(logger, paths["events"], run_id=run_id, stage="description",
                      slug=slug, step="generate", status="skipped",
                      detail="LLM returned no description", level=logging.WARNING)
            continue

        detail_msg = f"description={len(description):,} chars"
        log_event(logger, paths["events"], run_id=run_id, stage="description", slug=slug,
                  step="generate", status="ok", detail=detail_msg)

        if args.dry_run:
            report.record(slug, "description", "would-enrich", detail_msg)
            review.add(ReviewRow(slug=slug, status="would-enrich", before=before,
                                 generated=description, sources=fed))
            log_event(logger, paths["events"], run_id=run_id, stage="description",
                      slug=slug, step="write", status="would-enrich", detail=detail_msg)
            continue

        try:
            api.patch_field(slug, "description", description, if_match=etag)
            report.record(slug, "description", "enriched", detail_msg)
            review.add(ReviewRow(slug=slug, status="enriched", before=before,
                                 generated=description, sources=fed))
            log_event(logger, paths["events"], run_id=run_id, stage="description",
                      slug=slug, step="write", status="enriched", detail=detail_msg)
        except Exception as exc:
            report.record(slug, "description", "error", f"PATCH failed: {exc}")
            review.add(ReviewRow(slug=slug, status="error", before=before,
                                 generated=description, sources=fed,
                                 note=f"PATCH failed: {exc}"))
            log_event(logger, paths["events"], run_id=run_id, stage="description",
                      slug=slug, step="write", status="error", detail=str(exc),
                      level=logging.ERROR)

    stats = report.summary()
    print_summary(stats, args.dry_run, "Description generation")
    unmet_reasons = report.unmet_reasons()
    if unmet_reasons:
        print("  unmet reasons:")
        for reason, count in unmet_reasons.most_common():
            print(f"    {count} x {reason}")

    usage_summary = ""
    if usage.calls > 0:
        usage_summary = render_usage_table(
            usage.as_dict()["by_provider"], title="description usage")
        print()
        print(usage_summary)

    print(f"review file: {review.write()}")

    log_run_footer(
        logger, stage="description", stats=stats,
        duration_s=time.monotonic() - start_time, usage_summary=usage_summary,
    )

    return report


if __name__ == "__main__":
    main()
