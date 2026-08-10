#!/usr/bin/env python
"""Write the public case narrative `Case.description` via LLM (DB-free). LOCAL WRITES ONLY.

Ported from the deleted `casework/enrich_description.py` (recovered at donor commit
`0321a85`, 619 lines). Reads a case's charge sheet, CIAA press release and Special
Court verdict entirely over the Jawafdehi HTTP API and asks the premium LLM tier for
the अभियोगदावी / बयान / फैसला structure of https://github.com/Jawafdehi/JawafdehiAPI/issues/199.
Never touches the database directly -- writes go through `CaseworkApi.patch_fields`,
which this project's binding constraint restricts to loopback (`127.0.0.1:48010`).

It is the field production is emptiest on: 188 of 3,003 cases carry a description.

TWO FIELDS, ONE CALL. This script writes `description` and `missing_details`,
and nothing else. The second rides in the SAME generate call because that call
has already paid to summarise the verdict -- `summarize_verdict` bills one
premium request per 150,000-char chunk, and batch verdicts run to a 141,000-char
median. A standalone stage would re-read the same फैसला from scratch, roughly
doubling the batch's premium spend.

Derivation stays independent: `missing_details` comes from which materials are
BOUND plus which documents the sources cite, never from the finished narrative.
`casework/common/missing_details.py` holds that logic, LLM- and API-free.

The gates DIFFER, so `build` returns None on cases this stage still describes:
`description` needs press release OR verdict, `missing_details` needs the verdict
specifically. Press-only cases are reported as `no verdict bound`, not an error.

DEVIATION 1 -- THE DONOR'S TITLE PASS IS DROPPED. The donor regenerated
`Case.title` in the same call, gated by `--skip-title`. `title` now has exactly
one owner, `casework/enrich_card.py`, so `--skip-title`, `validate_title`,
`title_has_headcount` and the `"title"` response key are all gone. Costs one
extra cheap call per case, since `enrich_card` reads the description this script
wrote. `test_never_writes_title` pins it.

DEVIATION 2 -- `invoke_text`, NOT the donor's `invoke_with_tools` +
`convert_date` tool. The donor passed the tool but never told the model it
existed, and this stage emits prose, not structured dates: every date reaching it
is already converted by `enrich_timeline` or copied verbatim from a source, where
leaving the BS date as written is correct. The paired safety change is the
QUALITY RULE forbidding the model from converting dates itself -- without it,
dropping the tool invites the silent BS<->AD arithmetic error it prevented.

DEVIATION 3 -- THE DONOR'S NGM SECTION IS NOT PORTED. It fetched
`GET /ngm/court_case/{special:NNN-CR-NNNN}`, a doubly dead path: the
colon-prefixed reference matches 0 of 109 real `court_cases` entries (they are
full IRIs), and the endpoint was removed in the 2026-07-01 cut of `config/urls.py`.
Prompt-identical either way -- the donor's `{ngm_section}` renders empty whenever
the fetch returns None, which is always. `casework/enrich_timeline.py` has the
measurements.

Usage:
    uv run python -m casework.enrich_description --dry-run
    uv run python -m casework.enrich_description --slug case-0123
    uv run python -m casework.enrich_description --limit 3 --verbose
    uv run python -m casework.enrich_description --apply
"""

import argparse
import json
import logging
import os
import sys
import time

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
from casework.common.missing_details import accept_items
from casework.common.missing_details import build as build_missing_details
from casework.common.missing_details import has_verdict, held_summary
from casework.common.parse import parse_object_response
from casework.common.pipeline import (
    COURT_TYPES,
    PRESS_TYPES,
    STAGES,
    SUBSTANTIAL_DESCRIPTION_CHARS,
    RunReport,
    unmet_prerequisites,
)
from casework.common.review import ReviewRow, build_review_file
from casework.common.select import court_number, select_for_run

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
DONOR_MAX_TOKENS = 8000
# `maxOutputTokens` for this model, as the CLI reports it in its result JSON.
MODEL_MAX_OUTPUT_TOKENS = 64000


def _max_tokens_from_env() -> int:
    """Description output cap, from `CASEWORK_DESCRIPTION_MAX_TOKENS`.

    The donor's 8000 is the default, but a case whose verdict summarises to ~16k
    chars needs more and the CLI hard-errors rather than truncating (078-CR-0038,
    078-CR-0073). Validated rather than trusted because a bad value is expensive:
    one extra zero and every request fails after ~10 minutes of model time.
    """
    raw = (os.getenv("CASEWORK_DESCRIPTION_MAX_TOKENS") or "").strip()
    if not raw:
        return DONOR_MAX_TOKENS
    try:
        value = int(raw)
    except ValueError:
        raise SystemExit(
            f"CASEWORK_DESCRIPTION_MAX_TOKENS must be a whole number, got {raw!r}"
        ) from None
    if not 1 <= value <= MODEL_MAX_OUTPUT_TOKENS:
        raise SystemExit(
            "CASEWORK_DESCRIPTION_MAX_TOKENS must be between 1 and "
            f"{MODEL_MAX_OUTPUT_TOKENS:,}, got {value:,}"
        )
    return value


DESCRIPTION_MAX_TOKENS = _max_tokens_from_env()

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

MISSING DOCUMENTS — a SECOND, separate output, independent of the description:

The source documents REFER to other documents — witness depositions, defendant
statements, contracts, audit reports, bid papers, bank records, court orders. Some
of those we hold; most we do not. Your job is the DIFFERENCE.

You are told below, under DOCUMENTS WE ALREADY HOLD, exactly what is in our
evidence. List the documents the sources REFERENCE OR RELY ON that are NOT in
that list, up to 4, most significant first.

Rules:
- Each item must be a document the sources actually mention, cite, or quote.
  If you cannot point to where it is referenced, leave it out.
- NEVER list something that appears in DOCUMENTS WE ALREADY HOLD.
- Be SPECIFIC. Name the document, with its date, party, phase, or number when the
  sources give one. Specificity is the whole value of this output.
- Short Nepali noun phrases (देवनागरी), not sentences. No "… छैन।" — the page
  already frames these as missing. Keep each under ~15 words.
- Do NOT list: अन्य आवश्यक स्रोतहरू, थप आधार र प्रमाण, अन्य प्रमाण, or any
  catch-all filler. An item a reader cannot go and look for is worthless.
- Do NOT list the अभियोगपत्र/आरोपपत्र, and do NOT comment on whether an appeal
  (पुनरावेदन) was filed. Both are already handled.
- Do not criticise the court's reasoning or the CIAA's investigation. This output
  is about our archive's completeness, not the case's merits.
- Return [] when the sources reference nothing beyond what we hold. An empty list
  is a perfectly good answer; padding it with vague items is not.

Good (all from published cases):
- ३ चरणका ठेक्का सम्झौताका प्रतिलिपि
- मूल सम्झौता (2011) र पुरक सम्झौता
- लेखापरीक्षण प्रतिवेदन र महालेखापरीक्षकको टिप्पणी
- सुनिल पौडेलको UOB Singapore बैंक खातामा जम्मा भएको लेनदेन विवरण र मिति
- मिति २०८१।१२।१८ गते विशेष अदालतबाट भएको आदेश
- प्रतिवादीहरूले अदालतमा गरेको बयानको ब्याहोरा
- साक्षीहरूको वकपत्र

Bad:
- अन्य आवश्यक स्रोतहरू।  →  filler; names nothing
- थप आधार र प्रमाण पुष्टि गर्ने प्रमाणिक स्रोत  →  filler
- अभियोगपत्र  →  already handled
- प्रेस विज्ञप्ति  →  we hold it; check the list before you write
- अदालतले पर्याप्त प्रमाण मूल्याङ्कन गरेको छैन।  →  a criticism, and a sentence

OUTPUT FORMAT — return ONLY a single JSON object, no markdown fences, no prose:
{"description": "### क) …\\n…", "missing_documents": []}
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

NAMED ENTITIES (accused / related / location). A `फैसला:` label on an entity is
that person's own outcome — it is the authoritative per-defendant answer for
section ग, so prefer it over inferring the split from the source prose. Any other
trailing text is an INTERNAL caseworker note: read it for context, never quote or
paraphrase it, and never treat it as a published fact.
{entities}

DOCUMENTS WE ALREADY HOLD (our complete evidence for this case — anything the
sources reference that is NOT here is what `missing_documents` must report; never
list one of these):
{held_documents}

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
    """One premium-tier call. Returns `(description, documents)`.

    `documents` is raw model output, unvalidated on purpose --
    `missing_details.accept_items` owns every acceptance rule.
    """
    prompt = EXTRACTION_USER_PROMPT.format(
        case_title=detail.get("title") or "",
        # UPPERCASED. `select.court_number()` reads the number off the canonical
        # IRI, which is lowercase (`.../courtcase/special/081-cr-0091`), and the
        # prompt tells the model to prefer specifics from the context over vague
        # phrasing -- so the lowercase form lands verbatim in public prose. Fixed
        # for the card on evidence (`081-cr-0060` shipped in 2 of 5 titles in the
        # 2026-08-04 evaluation, against 50/50 uppercase in PUBLISHED titles);
        # this is the same defect on the same input, one stage earlier.
        court_number=(court_number or "").upper() or "(unknown)",
        bigo=format_bigo(detail.get("bigo")),
        court_cases=", ".join(detail.get("court_cases") or []) or "(none)",
        key_allegations=format_list(detail.get("key_allegations")),
        timeline=json.dumps(detail.get("timeline") or [], ensure_ascii=False),
        entities=format_entities(detail.get("entities")),
        held_documents=held_summary(detail),
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


def _parse_description_response(response_text: str):
    """Pull `description` and `missing_documents` out of the JSON object reply.

    `description` may be None; `documents` is always a list. `description` is the
    REQUIRED key for the object scan -- requiring `missing_documents` too would
    reject every correct empty-list reply. A volunteered `title` key is ignored,
    which is what holds the single-owner rule against a chatty response.
    """
    obj = parse_object_response(response_text, "description")
    if obj is None:
        log.warning("No JSON object with a description found in the LLM response")
        return None, []
    description = (obj.get("description") or "").strip()
    return (description or None), _coerce_documents(obj.get("missing_documents"))


def _coerce_documents(raw) -> list:
    """Normalise the `missing_documents` value into a list of strings.

    Tolerant on SHAPE, strict on content -- content rules live in
    `missing_details.reject_item`, where they can be tested. A bare string, a
    quoted `"[]"`, or a newline-joined blob is a formatting slip, not a wrong
    answer, so rejecting it would throw away a correct finding over punctuation.
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        log.warning("missing_documents was %s, not a list -- ignored", type(raw).__name__)
        return []
    out = []
    for entry in raw:
        if not isinstance(entry, str):
            continue
        # Split a newline-joined blob, and strip any enumerator the model added
        # despite being asked for plain phrases.
        for line in entry.splitlines():
            line = line.strip().lstrip("-–•").strip()
            for mark in ("क)", "ख)", "ग)", "घ)", "ङ)", "च)",
                         "१.", "२.", "३.", "४.", "५.",
                         "1.", "2.", "3.", "4.", "5."):
                if line.startswith(mark):
                    line = line[len(mark):].strip()
                    break
            # Checked PER ENTRY, not just on a bare string. A model that answers
            # `["कुनै छैन"]` means "nothing"; treated as an item it publishes the
            # word "none" as a missing document, and no content rule catches it.
            if _is_nothing(line):
                continue
            if line:
                out.append(line)
    return out


# "no documents" spelled as text rather than as an empty list.
_NOTHING = ("", "null", "none", "n/a", "na", "-", "[]", "{}", "nil")
_NOTHING_NE = ("कुनै छैन", "कुनै पनि छैन", "छैन", "कुनै कागजात छैन")


def _is_nothing(line: str) -> bool:
    text = (line or "").strip().strip("।.")
    return text.lower() in _NOTHING or text in _NOTHING_NE


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
    except Exception as exc:  # noqa: BLE001 - bootstrap is the entrypoint; it reports and exits 1
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
    # `select_for_run` is the one selection path every enricher shares (#410): it
    # applies the --batch-csv allowlist, then the other selectors, then --limit --
    # and it slices --limit in BATCH order, which a local `cases[:limit]` cannot
    # do because the API's iteration order is not the CSV's.
    cases = select_for_run(all_cases, args)

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
        fetch_ok = True
        try:
            detail, etag = api.get_case_with_etag(slug)
        except Exception as exc:  # noqa: BLE001 - donor-preserved fallback to the list payload
            # Donor-preserved fallback: a detail-fetch failure does not abort the
            # case. The LIST-shaped payload still yields a well-formed "unmet"
            # reason below (unresolved material), never a crash. Widened from the
            # donor's `requests.HTTPError` because `CaseworkApi` is urllib-based.
            fetch_ok = False
            detail = case
            log_event(logger, paths["events"], run_id=run_id, stage="description",
                      slug=slug, step="fetch", status="fallback", detail=str(exc),
                      level=logging.WARNING)

        # A SUCCESSFUL FETCH THAT CARRIES NO ETag IS AN UNCONDITIONAL WRITE.
        # `if_match` is sent only when truthy and the server checks it only when
        # present, so a 200 missing the header (a proxy stripping it, a
        # non-`retrieve` path) writes without a precondition and logs nothing about
        # it. Scoped to `fetch_ok` so the donor fallback above is untouched: that
        # path is meant to reach the unmet-material gate and report from there.
        if fetch_ok and not etag:
            reason = ("case detail returned no ETag; refusing to write "
                      "unconditionally (would clobber a concurrent edit)")
            report.record(slug, "description", "error", reason)
            review.add(ReviewRow(slug=slug, status="error",
                                 before=(detail.get("description") or "").strip(),
                                 note=reason))
            log_event(logger, paths["events"], run_id=run_id, stage="description",
                      slug=slug, step="fetch", status="error", detail=reason,
                      level=logging.ERROR)
            continue

        before = (detail.get("description") or "").strip()

        # PER FIELD, because this stage writes two. A description-only check
        # skipped every case that already had a description before `missing_details`
        # was even computed -- and `provides` names both fields, so an orchestrator
        # reading it would call exactly those cases complete. The description itself
        # is still never overwritten without --force; see the write below.
        description_done = _has_substantial_description(detail)
        missing_done = bool((detail.get("missing_details") or "").strip())
        if description_done and (missing_done or not has_verdict(detail)) \
                and not args.force:
            reason = f"description already {len(before):,} chars"
            if not missing_done:
                reason += "; missing_details needs a verdict"
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

        # A PARTIAL FETCH FAILURE MUST BE VISIBLE. `text_unmet` was previously
        # consumed only when NOTHING fetched, so a case whose charge sheet
        # succeeded and whose court order 500'd generated a full public narrative
        # from the prosecution claim alone -- silently omitting that the defendant
        # was acquitted. Nothing in the review file said a verdict source existed
        # and was lost, so the human reviewer could not catch it either. This is
        # the one stage where the lost source can BE the outcome.
        source_note = ""
        if text_unmet:
            source_note = "SOURCE MISSING — " + "; ".join(text_unmet)
            for reason in text_unmet:
                report.record(slug, "description", "partial", reason)
            log_event(logger, paths["events"], run_id=run_id, stage="description",
                      slug=slug, step="source", status="partial",
                      detail="; ".join(text_unmet), level=logging.WARNING)

        log_event(logger, paths["events"], run_id=run_id, stage="description", slug=slug,
                  step="source", status="ok",
                  detail=f"{len(chunks)} source(s): "
                         + ", ".join(f"{t}({len(x):,})" for t, _, x in chunks))

        try:
            source_block, fed = _assemble_source_text(chunks, invoke_text, usage)
        except Exception as exc:  # noqa: BLE001 - source assembly is per-case; the run continues
            report.record(slug, "description", "error", f"source assembly failed: {exc}")
            review.add(ReviewRow(slug=slug, status="error", before=before,
                                 note=f"source assembly failed: {exc}"))
            log_event(logger, paths["events"], run_id=run_id, stage="description",
                      slug=slug, step="assemble", status="error", detail=str(exc),
                      level=logging.ERROR)
            continue

        try:
            description, documents = _generate_description(
                detail=detail,
                court_number=court_number(detail),
                source_text=source_block,
                invoke_text=invoke_text,
                usage=usage,
            )
        except Exception as exc:  # noqa: BLE001 - an LLM failure is recorded per-case and the run continues
            report.record(slug, "description", "error", f"LLM generation failed: {exc}")
            review.add(ReviewRow(slug=slug, status="error", before=before, sources=fed,
                                 note="; ".join(filter(None, (
                                     f"LLM generation failed: {exc}", source_note)))))
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
                                 note="; ".join(filter(None, (
                                     "LLM returned no description", source_note)))))
            log_event(logger, paths["events"], run_id=run_id, stage="description",
                      slug=slug, step="generate", status="skipped",
                      detail="LLM returned no description", level=logging.WARNING)
            if documents:
                # This stage abandons the case here, so the findings die with it.
                # Unlogged they are indistinguishable from a model that found
                # nothing -- the exact confusion the rejection logging exists for.
                log_event(logger, paths["events"], run_id=run_id, stage="description",
                          slug=slug, step="documents", status="discarded",
                          detail=f"{len(documents)} item(s) lost with the blank "
                                 "description: " + " | ".join(documents),
                          level=logging.WARNING)
            continue

        before_missing = (detail.get("missing_details") or "").strip()

        # A PARTIAL FETCH IS THE TRAP HERE. `has_verdict` is BINDING-based, so a
        # case whose court order failed to fetch still reports True and
        # `held_summary` still tells the model we hold the verdict -- so it is asked
        # to diff the sources against an inventory it could not read, and the result
        # would be published. The description path has `source_note` for this; this
        # field falls back to the deterministic floor, which is binding-based and
        # therefore still true.
        #
        # Read off `chunks`, NOT off `text_unmet`'s prose. Parsing `f"{mtype}: …"`
        # out of a human-readable reason meant rewording that message silently
        # disabled the guard, and a case with two court orders where one failed
        # tripped it even though a verdict HAD been read.
        verdict_read = any(mtype in COURT_TYPES for mtype, _, _ in chunks)
        verdict_lost = has_verdict(detail) and not verdict_read

        blocked = None
        if not has_verdict(detail):
            blocked = "no verdict bound"
        elif verdict_lost:
            blocked = "verdict source was not fetched; deterministic floor only"
        elif before_missing:
            blocked = f"missing_details already {len(before_missing):,} chars"

        # Every rejection is logged with the rule that fired: a silently dropped
        # item looks identical to a model that found nothing, and those need
        # opposite follow-up (prompt problem vs sourcing problem). Rejections
        # never block the write -- the floor alone is a publishable value.
        kept, rejected = accept_items(documents, detail)
        for item, reason in rejected:
            log_event(logger, paths["events"], run_id=run_id, stage="description",
                      slug=slug, step="documents", status="rejected",
                      detail=f"{reason}: {item[:120]}", level=logging.WARNING)
        if kept and blocked:
            # "accepted" followed by "skipped: <reason>" reads as a contradiction,
            # and this log exists to tell a prompt problem from a sourcing problem.
            log_event(logger, paths["events"], run_id=run_id, stage="description",
                      slug=slug, step="documents", status="discarded",
                      detail=f"{len(kept)} item(s) found but {blocked}: "
                             + " | ".join(kept), level=logging.WARNING)
        elif kept:
            log_event(logger, paths["events"], run_id=run_id, stage="description",
                      slug=slug, step="documents", status="accepted",
                      detail=f"{len(kept)} of {len(documents)}: " + " | ".join(kept))
        elif documents:
            log_event(logger, paths["events"], run_id=run_id, stage="description",
                      slug=slug, step="documents", status="none-kept",
                      detail=f"all {len(documents)} proposed item(s) rejected",
                      level=logging.WARNING)

        missing = build_missing_details(detail, [] if verdict_lost else kept)

        # NEVER TOUCH A NON-EMPTY VALUE -- not even with --force. Two things share
        # this field and neither can be safely merged into:
        #  - the importer's truncation guard
        #    (`CIAADraftCaseService._flag_truncated_roster`) appends
        #    `ACCUSED LIST INCOMPLETE`, and losing it would let a case publish with
        #    a knowingly truncated accused list;
        #  - the 61 hand-written published values.
        # Appending was tried and withdrawn. A repeat --force run reads run 1's own
        # output back as `before_missing`, and the floor items alone are not a
        # usable signature for "this stage wrote it" -- they were copied verbatim
        # FROM hand-written cases. So any append duplicates the floor and restarts
        # the enumeration (`क) … ख) … क) … ख) …`), unbounded across runs, and the
        # concatenation never passes back through `build` so MAX_CHARS never sees
        # it. Refusing costs a manual clear; appending corrupts the page.
        if missing and before_missing:
            log_event(logger, paths["events"], run_id=run_id, stage="description",
                      slug=slug, step="missing_details", status="already",
                      detail=f"missing_details already {len(before_missing):,} chars; "
                             "not appending (clear the field by hand to regenerate)"
                             + (" -- --force does NOT override this" if args.force else ""),
                      level=logging.WARNING if args.force else logging.INFO)
            missing = None
        elif not missing:
            # Distinct reasons needing different follow-up: a press-only case wants
            # sourcing, a case with both floor items satisfied wants nothing.
            log_event(logger, paths["events"], run_id=run_id, stage="description",
                      slug=slug, step="missing_details", status="skipped",
                      detail=blocked or "no gap to report")

        detail_msg = f"description={len(description):,} chars"
        if missing:
            detail_msg += f", missing_details={len(missing):,} chars"
        log_event(logger, paths["events"], run_id=run_id, stage="description", slug=slug,
                  step="generate", status="ok", detail=detail_msg)

        # `generated` stays the DESCRIPTION ALONE. The review file prints
        # `len(generated)` in its summary table and section heading under a header
        # naming `description`, and that before/after size comparison is what flags
        # a truncated or runaway description -- folding a second field into it
        # over-reported by the length of that field.
        #
        # missing_details rides in `note` instead, which the file prints per case.
        # Discarded findings go there too: without them, "found four documents and
        # threw them away" looks identical to "found nothing" to the person
        # approving the run, which is the confusion the reject logging exists to
        # prevent.
        md_note = ""
        if missing:
            md_note = "missing_details → " + " · ".join(missing.splitlines())
        elif blocked and kept:
            md_note = (f"missing_details NOT written ({blocked}); discarded: "
                       + " · ".join(kept))
        elif blocked:
            md_note = f"missing_details NOT written ({blocked})"
        note = "; ".join(filter(None, (source_note, md_note)))

        if args.dry_run:
            report.record(slug, "description", "would-enrich", detail_msg)
            review.add(ReviewRow(slug=slug, status="would-enrich", before=before,
                                 generated=description, sources=fed, note=note))
            log_event(logger, paths["events"], run_id=run_id, stage="description",
                      slug=slug, step="write", status="would-enrich", detail=detail_msg)
            continue

        # THE BACKSTOP FOR THE FALLBACK ROUTE. The fetch-time guard above is
        # scoped to a successful fetch, which leaves the donor fallback
        # (`detail = case`, `etag = None`) able to reach a write. Today the
        # unmet-material gate stops it, because a LIST payload carries
        # `material: null` -- but that is an accident of the list serializer, not
        # a precondition, and it is the same "safe by accident" reasoning
        # `enrich_card` refuses to rely on. One check, at the only write.
        if not etag:
            reason = ("no ETag for this case; refusing an unconditional write "
                      "(would clobber a concurrent edit)")
            report.record(slug, "description", "error", reason)
            review.add(ReviewRow(slug=slug, status="error", before=before,
                                 generated=description, sources=fed,
                                 note="; ".join(filter(None, (reason, note)))))
            log_event(logger, paths["events"], run_id=run_id, stage="description",
                      slug=slug, step="write", status="error", detail=reason,
                      level=logging.ERROR)
            continue

        try:
            # ONE conditional request for both fields. `patch_fields`, not two
            # `patch_field` calls: the second call's ETag would already be stale
            # from the first write, so a loop cannot stay conditional.
            #
            # The description is omitted when the case already had a substantial
            # one and --force was not passed. That is what lets the per-field gate
            # above admit a described case for its EMPTY missing_details without
            # silently rewriting prose a human may have approved.
            pairs = []
            if not description_done or args.force:
                pairs.append(("description", description))
            if missing:
                pairs.append(("missing_details", missing))
            if not pairs:
                log_event(logger, paths["events"], run_id=run_id, stage="description",
                          slug=slug, step="write", status="skipped",
                          detail="nothing left to write")
                report.record(slug, "description", "already", "nothing left to write")
                continue
            api.patch_fields(slug, pairs, if_match=etag)
            report.record(slug, "description", "enriched", detail_msg)
            review.add(ReviewRow(slug=slug, status="enriched", before=before,
                                 generated=description, sources=fed, note=note))
            log_event(logger, paths["events"], run_id=run_id, stage="description",
                      slug=slug, step="write", status="enriched", detail=detail_msg)
        except Exception as exc:  # noqa: BLE001 - a PATCH failure is recorded per-case and the run continues
            report.record(slug, "description", "error", f"PATCH failed: {exc}")
            review.add(ReviewRow(slug=slug, status="error", before=before,
                                 generated=description, sources=fed,
                                 note="; ".join(filter(None, (
                                     f"PATCH failed: {exc}", note)))))
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
