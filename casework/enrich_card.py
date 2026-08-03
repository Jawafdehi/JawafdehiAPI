#!/usr/bin/env python
"""Write the listing-card fields `Case.title` + `Case.short_description` (DB-free).
LOCAL WRITES ONLY.

THIS SCRIPT IS THE SOLE WRITER OF `Case.title` ON `main`. Nothing else may patch
that field. The donor had three paths that could: `enrich_description`'s title
side-write, the standalone `enrich_title.py`, and `enrich_card`. All three are now
this one file -- `casework/enrich_description.py` documents dropping its pass as
deviation 1, and `enrich_title.py` folds in here as `--only title` (deviation 1
below). A title write appearing anywhere else is the bug; `casework/common/titles.py`
is the contract and this module is its only importer.

WHAT IS BROKEN ON REAL DATA, and why this is not cosmetic work: 2,666 of 2,918
DRAFT cases carry the importer's template in both fields --

    title:             CIAA Special Court Case 076-CR-0182: बिनोद कुमार भूजेल समेत ५
    short_description: अख्तियार दुरुपयोग अनुसन्धान आयोगले विशेष अदालतमा दायर गरेको
                       मुद्दा 076-CR-0182, प्रतिवादी: बिनोद कुमार भूजेल समेत ५।

-- so `title` reads as 100% covered and is in fact ~9% covered (only 155 DRAFT
titles end in the case number in parens, which is the format contract). Both
fields render on the public case list, so wrong here is wrong on the front page.

Fetches NO source documents. Both fields derive from the `description` already on
the case (falling back to `key_allegations`), which is what makes the stage cheap
and what makes the single-owner split affordable: `order_stages` runs
`description` first and this picks up what it wrote, no charge sheet re-fetched.

Ported from two donors at commit `0321a85`: `casework/enrich_card.py` (425 lines)
and `casework/enrich_title.py` (283 lines). Four deliberate deviations:

DEVIATION 1 -- `enrich_title.py` IS NOT PORTED AS ITS OWN SCRIPT. It becomes
`--only title`. The donor needed a separate script because `enrich_description`
skipped its own title pass once a substantial description existed, leaving a case
with a good description no path to a fixed title. `--only title` IS that path, and
it must keep working standalone on a case whose `description` is already good and
must stay untouched (`test_only_title_leaves_the_description_untouched`). Where the
two donors' prompts differ, this takes `enrich_title`'s WIDER input set: it fed
`entities` as well, and named accused are exactly what a headline needs. It also
takes the shared `TITLE_RULES` from `casework/common/titles.py` over the donor
card's own shorter restatement of the same rules -- one place to change a rule
instead of two that quietly diverge.

DEVIATION 2 -- ONE TIER FOR BOTH FIELDS, `cheap`. The donors disagreed: the card
donor used `tier="cheap"` (line 337), `enrich_title.py` used `tier="premium"`
(line 275) for the same no-fetch inputs, with a docstring arguing a headline needs
"a real Opus". Resolved in favour of cheap, per the brief. The reasoning that
settles it: this stage reads no source document, so the hard part -- being faithful
to a charge sheet -- has already been done by the description pass, and the
remaining job is compression. The two write gates are also unusually strong here
(a title that fails `validate_title` is never written at all), so a weak model
costs a rejected call, not a bad public headline.

DEVIATION 3 -- NO LIST-LEVEL `skip_field`. The card donor selected cases with
`get_target_cases(api, args, skip_field="short_description")`, on the stated
assumption that "nothing populates it today, so virtually every card is
processed". That assumption is now false, and expensively so: 2,666 cases carry a
populated template stub, so a field-presence skip would skip every single case
this script exists to fix. Selection is `select_cases` plus per-case gates, and
the short_description gate is the content-based adequacy judge
(`casework/common/judge.py`), which is what can tell a 120-character grammatical
Nepali stub from a real teaser.

DEVIATION 4 -- AN OVER-LENGTH TITLE FAILS THE CASE, IT IS NOT TRIMMED.
`CasePatchSerializer` caps `title` at `max_length=200` (and so does the model), and
the donor had no check -- it would have sent the title and taken a 422. Truncating
a headline mid-word is worse than not having regenerated it, because a trimmed
title looks deliberate and loses the case number the contract requires at the end.
So over-length is reported and the case moves on. The `short_description` cap of
320 is different in kind: the field is a `TextField` with no server limit, so that
number is the donor's editorial judgement about a one-line card teaser, kept as-is.

Usage:
    uv run python -m casework.enrich_card --dry-run
    uv run python -m casework.enrich_card --slug case-0123
    uv run python -m casework.enrich_card --only title --limit 5 --verbose
    uv run python -m casework.enrich_card --only short_description --apply
"""

import argparse
import logging
import sys
import time
from dataclasses import dataclass

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
from casework.common.judge import judge_description_adequacy
from casework.common.llm import bootstrap, tier_for
from casework.common.parse import parse_object_response
from casework.common.pipeline import STAGES, RunReport, unmet_prerequisites
from casework.common.review import ReviewRow, build_review_file
from casework.common.select import court_number, select_cases
from casework.common.titles import (
    TITLE_RULES,
    title_has_headcount,
    title_is_acceptable,
    validate_title,
)

log = logging.getLogger("casework.enrich_card")

STAGE = STAGES["card"]

TITLE_FIELD = "title"
SHORT_FIELD = "short_description"

# The donor read neither of these from the environment (no `env_int` knob existed
# for them), so both are the donors' own literals. 3000 is the CARD donor's
# snippet budget; `enrich_title.py` used 2000, and the wider one wins per
# deviation 1.
DESCRIPTION_SNIPPET_BUDGET = 3000
CARD_MAX_TOKENS = 1000

# `title` really is capped at 200 by CasePatchSerializer AND cases/models.py.
# `short_description` is a TextField with no server cap -- 320 is the donor's
# editorial limit for a one-line teaser. See deviation 4.
MAX_TITLE_CHARS = 200
MAX_SHORT_DESCRIPTION_CHARS = 320

SYSTEM_PROMPT = (
    """\
You are a Nepali editor writing the public listing-card fields for a case in \
Jawafdehi, a civic accountability archive of Nepal's anti-corruption cases \
investigated by the CIAA (अख्तियार दुरुपयोग अनुसन्धान आयोग) and tried at the \
Special Court (विशेष अदालत).

You are given the case's current title, its बिगो amount, key allegations, named \
entities, and a snippet of the already-written case description. Produce up to two \
fields in formal Nepali (देवनागरी; keep English technical/proper terms — "CR", \
company names — as-is). Ground every word in the provided data: never invent \
names, amounts, sections, dates, or outcomes.

"""
    + TITLE_RULES
    + """

SHORT_DESCRIPTION RULES (when asked for it):
- A SINGLE punchy teaser, 1–2 sentences, ideally under 200 characters — the essence
  (who/what/how much) at a glance.
- Plain prose: no court number, no markdown, no headings, no bullets.
- Neutral and factual, grounded in the provided data.

OUTPUT FORMAT — return ONLY a single JSON object, no markdown fences, no prose.
Include only the requested key(s); set an unrequested key to null:
{"title": "नेपाली शीर्षक (080-CR-0047)", "short_description": "एक-वाक्य सारांश"}
"""
)

USER_PROMPT = """\
{request_note}

Current title: {current_title}
Special-court case number (MUST end any regenerated title): {court_number}
Bigo (बिगो), NPR: {bigo}

KEY ALLEGATIONS:
{key_allegations}

NAMED ENTITIES (accused / related / location):
{entities}

DESCRIPTION (snippet — the factual basis for both fields):
{description}

Return ONLY the JSON object described in the system prompt.
"""


def build_parser():
    ap = argparse.ArgumentParser(
        description="Write case listing-card fields (title + short_description) "
                    "via LLM, DB-free over the Jawafdehi HTTP API.",
        epilog="This is the only script that patches Case.title.",
    )
    add_common_args(ap)
    # The donor's choices were ("title", "short", "both"). `short` is spelled out
    # here so the flag value matches the field name it writes -- `--only short`
    # patching `short_description` is one more thing a reader has to hold in
    # their head while auditing which field a run touched.
    ap.add_argument(
        "--only", choices=(TITLE_FIELD, SHORT_FIELD, "both"), default="both",
        help="Which card field(s) to (re)generate (default: both). "
             "`--only title` is the replacement for the donor's enrich_title.py.")
    return ap


def _snippet(detail):
    """The description text fed to the prompt, or '(none)'."""
    return (detail.get("description") or "").strip()[:DESCRIPTION_SNIPPET_BUDGET] \
        or "(none)"


def _build_prompt(detail, number, need_title, need_short):
    wanted = [f for f, need in ((TITLE_FIELD, need_title), (SHORT_FIELD, need_short))
              if need]
    request_note = (
        "Produce the title AND the short_description."
        if len(wanted) == 2
        else f"Only the {wanted[0]} is needed; set the other key to null."
    )
    return USER_PROMPT.format(
        request_note=request_note,
        current_title=detail.get("title") or "",
        court_number=number or "(unknown)",
        bigo=format_bigo(detail.get("bigo")),
        key_allegations=format_list(detail.get("key_allegations")),
        entities=format_entities(detail.get("entities")),
        description=_snippet(detail),
    )


def _generate(detail, number, need_title, need_short, invoke_text, usage):
    """One cheap-tier call producing whichever fields were asked for.

    Returns the parsed dict, or None. Accepts an object carrying EITHER key --
    a run asking only for a title gets `{"title": ..., "short_description":
    null}`, so demanding both would reject every single-field reply.
    """
    response_text = invoke_text(
        system=SYSTEM_PROMPT,
        content=_build_prompt(detail, number, need_title, need_short),
        max_tokens=CARD_MAX_TOKENS,
        tier=tier_for("card"),
        usage=usage,
    )
    result = parse_object_response(
        response_text,
        predicate=lambda o: TITLE_FIELD in o or SHORT_FIELD in o,
    )
    if result is None:
        log.warning("No card JSON object found in the LLM response")
    return result


def vet_title(candidate, number):
    """(title, rejection_reason). Exactly one of the two is set.

    Every reason a title can be refused, in one place, so the dry-run preview
    and the write path cannot disagree about what is publishable. Over-length is
    a rejection, never a trim -- see deviation 4.
    """
    title = (candidate or "").strip()
    if not title:
        return None, "LLM returned no title"
    issue = validate_title(title, number)
    if issue:
        return None, issue
    if title_has_headcount(title):
        return None, "contains a defendant headcount"
    if len(title) > MAX_TITLE_CHARS:
        return None, (
            f"too long: {len(title)} > {MAX_TITLE_CHARS} chars "
            "(CasePatchSerializer max_length); not truncating a headline")
    return title, None


def vet_short_description(candidate):
    """(short_description, rejection_reason). Exactly one of the two is set.

    An empty value is a rejection, not a write: `CaseListSerializer` ships this
    field with no fallback to title or description (`cases/serializers.py:320`),
    so writing "" renders a blank card on the public list.
    """
    short = (candidate or "").strip()
    if not short:
        return None, "LLM returned an empty short_description"
    if len(short) > MAX_SHORT_DESCRIPTION_CHARS:
        return None, f"too long: {len(short)} > {MAX_SHORT_DESCRIPTION_CHARS} chars"
    return short, None


@dataclass
class _WriteContext:
    """Everything `_vet_and_write` needs that is the same for both fields.

    A dataclass rather than a closure inside `main()`: the two fields go through
    identical vet -> record -> log -> maybe-PATCH machinery, and writing that
    twice is how the title path and the short_description path drift until one
    of them forgets to pass `if_match` or forgets a review row.
    """
    api: object
    slug: str
    dry_run: bool
    etag: object
    sources: list
    report: object
    review: object
    logger: object
    run_id: str
    events_path: str


def _vet_and_write(ctx, field, current, candidate, vet):
    """Vet one generated field value and write it, or record why it was refused.

    Returns the written value, or None when nothing was written -- a rejection,
    a dry run, and a failed PATCH all return None, because in all three cases
    the server still holds `current`.
    """
    def _record(status, note, generated):
        ctx.report.record(ctx.slug, "card", status, f"{field}: {note}")
        ctx.review.add(ReviewRow(
            slug=ctx.slug, status=f"{status} ({field})", before=current,
            generated=generated, sources=ctx.sources, note=note))

    value, rejection = vet(candidate)
    if rejection:
        _record("rejected", rejection, (candidate or "").strip())
        log_event(ctx.logger, ctx.events_path, run_id=ctx.run_id, stage="card",
                  slug=ctx.slug, step=f"vet:{field}", status="rejected",
                  detail=rejection, level=logging.WARNING)
        return None

    if ctx.dry_run:
        _record("would-enrich", value, value)
        log_event(ctx.logger, ctx.events_path, run_id=ctx.run_id, stage="card",
                  slug=ctx.slug, step=f"write:{field}", status="would-enrich",
                  detail=f"{field}={value}")
        return None

    try:
        ctx.api.patch_field(ctx.slug, field, value, if_match=ctx.etag)
    except Exception as exc:
        _record("error", f"PATCH failed: {exc}", value)
        log_event(ctx.logger, ctx.events_path, run_id=ctx.run_id, stage="card",
                  slug=ctx.slug, step=f"write:{field}", status="error",
                  detail=str(exc), level=logging.ERROR)
        return None

    _record("enriched", value, value)
    log_event(ctx.logger, ctx.events_path, run_id=ctx.run_id, stage="card",
              slug=ctx.slug, step=f"write:{field}", status="enriched",
              detail=f"{field}={value}")
    return value


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
    args = build_parser().parse_args(argv)

    setup_logging(args.verbose)
    logger, run_id, paths = configure_run_logging("card", verbose=args.verbose)
    start_time = time.monotonic()

    try:
        bootstrap(args.provider, args.model)
    except Exception as exc:
        print(f"Bootstrap failed: {exc}", file=sys.stderr)
        sys.exit(1)

    from llm.invoke import invoke_text
    from llm.usage import UsageAccumulator, render_usage_table

    from casework.common.llm_cache import build_llm_cache, wrap_invoke_text

    # Local dev response cache (casework/common/llm_cache.py). Cheap tier, but
    # this stage makes TWO calls per case (the adequacy judge, then the
    # generation), so a re-run after a gate tweak still doubles the bill without
    # it. --no-llm-cache forces fresh.
    llm_cache = build_llm_cache(args)
    invoke_text = wrap_invoke_text(invoke_text, llm_cache)

    api = build_api(args)
    usage = UsageAccumulator()
    report = RunReport()
    # The review file names both fields: one run can write either or both, and a
    # reviewer has to see which.
    review = build_review_file(
        args, stage="card", field_name=f"{TITLE_FIELD} + {SHORT_FIELD}", run_id=run_id)

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
        logger, stage="card", base_url=args.api_base_url, dry_run=args.dry_run,
        provider=args.provider, model=args.model, n_selected=total,
        run_id=run_id, paths=paths,
    )
    if total == 0:
        print("No matching CIAA case(s) to process.", file=sys.stderr)
        print_summary(report.summary(), args.dry_run, "Card enrichment")
        print(f"review file: {review.write()}")
        log_run_footer(
            logger, stage="card", stats=report.summary(),
            duration_s=time.monotonic() - start_time,
        )
        return report

    print(f"Found {total} matching case(s). Target: {args.only}")
    if args.force:
        print("  --force: regenerating even fields that already pass their gate")
    if args.dry_run:
        print("  [DRY RUN] No changes will be saved.")

    want_title = args.only in (TITLE_FIELD, "both")
    want_short = args.only in (SHORT_FIELD, "both")

    for idx, case in enumerate(cases, 1):
        slug = case.get("slug") or "?"
        log_event(logger, paths["events"], run_id=run_id, stage="card", slug=slug,
                  step="start", status="start",
                  detail=f"[{idx}/{total}] {(case.get('title') or '')[:80]}")

        try:
            detail, etag = api.get_case_with_etag(slug)
        except Exception as exc:
            # Donor-preserved fallback: a detail-fetch failure does not abort the
            # case. Widened from the donor's bare `except` / `requests.HTTPError`
            # because `CaseworkApi` is urllib-based.
            detail, etag = case, None
            log_event(logger, paths["events"], run_id=run_id, stage="card", slug=slug,
                      step="fetch", status="fallback", detail=str(exc),
                      level=logging.WARNING)

        current_title = detail.get("title") or ""
        current_short = detail.get("short_description") or ""
        number = court_number(detail)

        unmet = unmet_prerequisites(STAGE, detail)
        if unmet:
            for reason in unmet:
                report.record(slug, "card", "unmet", reason)
            review.add(ReviewRow(slug=slug, status="unmet", before=current_title,
                                 note="; ".join(unmet)))
            log_event(logger, paths["events"], run_id=run_id, stage="card", slug=slug,
                      step="prereq", status="unmet", detail="; ".join(unmet),
                      level=logging.WARNING)
            continue

        # ── decide what needs work ──
        # No court number means no title can ever satisfy the format contract
        # (it must END in that number), so regenerating would burn a call on a
        # title guaranteed to be rejected. Donor-preserved.
        need_title = False
        if want_title:
            if not number:
                report.record(slug, "card", "unmet",
                              "no special-court number; a title cannot satisfy "
                              "the format contract")
            else:
                need_title = args.force or not title_is_acceptable(
                    current_title, number)
                if not need_title:
                    report.record(slug, "card", "already",
                                  f"title already valid: {current_title}")

        need_short = False
        short_reason = ""
        if want_short:
            if args.force:
                need_short = True
            else:
                adequate, short_reason = judge_description_adequacy(
                    current_short,
                    kind="case card short description (one-line teaser)",
                    invoke_text=invoke_text,
                    usage=usage,
                    context=f"title={current_title[:80]}",
                )
                need_short = not adequate
                if adequate:
                    report.record(slug, "card", "already",
                                  f"short_description already adequate: {short_reason}")
                log_event(logger, paths["events"], run_id=run_id, stage="card",
                          slug=slug, step="judge",
                          status="adequate" if adequate else "inadequate",
                          detail=short_reason)

        if not need_title and not need_short:
            review.add(ReviewRow(slug=slug, status="already", before=current_title,
                                 note="nothing to do (use --force)"))
            continue

        # Both fields derive from the description, falling back to allegations.
        if not (detail.get("description") or "").strip() and not detail.get(
                "key_allegations"):
            reason = "no description or key_allegations to derive a card from"
            report.record(slug, "card", "unmet", reason)
            review.add(ReviewRow(slug=slug, status="unmet", before=current_title,
                                 note=reason))
            log_event(logger, paths["events"], run_id=run_id, stage="card", slug=slug,
                      step="source", status="unmet", detail=reason,
                      level=logging.WARNING)
            continue

        try:
            result = _generate(detail, number, need_title, need_short,
                               invoke_text, usage)
        except Exception as exc:
            report.record(slug, "card", "error", f"LLM generation failed: {exc}")
            review.add(ReviewRow(slug=slug, status="error", before=current_title,
                                 note=f"LLM generation failed: {exc}"))
            log_event(logger, paths["events"], run_id=run_id, stage="card", slug=slug,
                      step="generate", status="error", detail=str(exc),
                      level=logging.ERROR)
            if args.verbose:
                import traceback

                traceback.print_exc()
            continue

        if result is None:
            report.record(slug, "card", "skipped", "LLM returned no parseable object")
            review.add(ReviewRow(slug=slug, status="skipped", before=current_title,
                                 note="LLM returned no parseable object"))
            log_event(logger, paths["events"], run_id=run_id, stage="card", slug=slug,
                      step="generate", status="skipped",
                      detail="no parseable object", level=logging.WARNING)
            continue

        # The description snippet IS the source for both fields, so it is what
        # the review file shows. There is no material IRI: this stage fetches no
        # document, and inventing one would misattribute the provenance.
        fed = [("case description (no document fetched)", "", _snippet(detail))]

        ctx = _WriteContext(
            api=api, slug=slug, dry_run=args.dry_run, etag=etag, sources=fed,
            report=report, review=review, logger=logger, run_id=run_id,
            events_path=paths["events"],
        )
        if need_title:
            _vet_and_write(ctx, TITLE_FIELD, current_title,
                           result.get(TITLE_FIELD), lambda c: vet_title(c, number))
        if need_short:
            _vet_and_write(ctx, SHORT_FIELD, current_short,
                           result.get(SHORT_FIELD), vet_short_description)

    stats = report.summary()
    print_summary(stats, args.dry_run, "Card enrichment")
    unmet_reasons = report.unmet_reasons()
    if unmet_reasons:
        print("  unmet reasons:")
        for reason, count in unmet_reasons.most_common():
            print(f"    {count} x {reason}")

    usage_summary = ""
    if usage.calls > 0:
        usage_summary = render_usage_table(
            usage.as_dict()["by_provider"], title="card usage")
        print()
        print(usage_summary)

    cache_summary = llm_cache.summary()
    print(cache_summary)
    print(f"review file: {review.write()}")

    log_run_footer(
        logger, stage="card", stats=stats,
        duration_s=time.monotonic() - start_time, usage_summary=usage_summary,
        cache_summary=cache_summary,
    )

    return report


if __name__ == "__main__":
    main()
