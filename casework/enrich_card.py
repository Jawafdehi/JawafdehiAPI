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
the case, which is what makes the stage cheap and what makes the single-owner
split affordable: `order_stages` runs `description` first and this picks up what
it wrote, no charge sheet re-fetched. `key_allegations` and the entity list go
into the prompt as supporting context, NOT as a fallback source -- the donor
treated them as one, but `STAGES["card"].requires_fields` declares
`("description",)`, so `unmet_prerequisites` reports any case with a blank
description as unmet before generation is ever reached. A description is a hard
prerequisite here, not a preference.

Ported from two donors at commit `0321a85`: `casework/enrich_card.py` (425 lines)
and `casework/enrich_title.py` (283 lines). Six deliberate deviations:

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
this script exists to fix. Selection is `select_for_run` plus per-case gates, and
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

DEVIATION 5 -- `CARD_MAX_TOKENS` IS 4000, NOT THE DONOR'S 1000. Measured on the
2026-08-04 local smoke run: one of two cases died with
`API Error: Claude's response exceeded the 1000 output token maximum`, a hard
provider failure rather than a truncated answer, so the case produced nothing at
all. The cap has to cover the two fields' own limits (200 + 320 chars) in
DEVANAGARI, which costs far more tokens per character than Latin text, plus
whatever framing the provider wraps around the reply. The donor's 1000 was set
against Latin-heavy expectations; it does not survive real Nepali output. 4000 is
still half of `enrich_description`'s budget and cannot mask an over-length title,
because `vet_title` rejects on characters, not tokens.

DEVIATION 6 -- BOTH FIELDS GO IN ONE PATCH, NOT ONE PATCH EACH. The donor wrote
each field with its own request. That cannot work against a server enforcing
`If-Match`: the first PATCH changes the case's ETag, so the second one -- still
carrying the ETag read at the top of the case -- fails with 412 every time. The
2026-08-04 smoke run wrote `title` and lost `short_description` to
"HTTP Error 412: Precondition Failed" on every case, so the script could never
finish a card. No dry run can catch it; a dry run issues no PATCH and reports
`would-enrich` for both fields. `_write_fields` now sends one multi-op document
(`CaseworkApi.patch_fields`), which also makes the write atomic: a half-written
card -- new headline, stale teaser -- is no longer reachable. Per-field
INDEPENDENCE is unchanged and lives where it always belonged, in vetting: a title
that fails `vet_title` is simply left out of the op list, and the teaser still
lands.

Usage:
    uv run python -m casework.enrich_card --dry-run
    uv run python -m casework.enrich_card --slug case-0123
    uv run python -m casework.enrich_card --only title --limit 5 --verbose
    uv run python -m casework.enrich_card --only short_description --apply
"""

import argparse
import logging
import re
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
from casework.common.pipeline import (
    STAGES,
    SUBSTANTIAL_DESCRIPTION_CHARS,
    RunReport,
    unmet_prerequisites,
)
from casework.common.review import ReviewRow, build_review_file
from casework.common.select import court_number, select_for_run
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
# 4000, not the donor's 1000 -- see DEVIATION 5. A Devanagari title +
# short_description does not fit in 1000 output tokens, and the provider turns an
# over-budget reply into a hard error, so the case yields nothing rather than a
# trimmed answer.
CARD_MAX_TOKENS = 4000

# `title` really is capped at 200 by CasePatchSerializer AND cases/models.py.
# `short_description` is a TextField with no server cap -- 320 is the donor's
# editorial limit for a one-line teaser. See deviation 4.
MAX_TITLE_CHARS = 200
MAX_SHORT_DESCRIPTION_CHARS = 320

#: The literal placeholders in the OUTPUT FORMAT example below. Named constants
#: so the prompt and the vetting gate cannot drift apart.
#:
#: WHY THIS IS A WRITE GATE AND NOT A CURIOSITY. A cheap-tier model that echoes
#: the example instead of answering it produces an object that passes every other
#: check. `vet_title` catches the title half only by accident -- the example's
#: case number never matches the case's own, so `validate_title` rejects it --
#: but `short_description` has no format contract to violate, so nothing stopped
#: "एक-वाक्य सारांश" ("one-sentence summary") from being PATCHed onto a public
#: case card. Deviation 2 argues the cheap tier is safe here because "the two
#: write gates are also unusually strong"; that was true of `title` and false of
#: `short_description`.
_TITLE_PLACEHOLDER = "नेपाली शीर्षक (080-CR-0047)"
_SHORT_PLACEHOLDER = "एक-वाक्य सारांश"

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
- STATE THE OUTCOME when the description reports one. If the case has been \
decided — दोषी, सफाई, आंशिक सफाई, जरिवाना, कैद — say so, briefly, after the \
allegation. A teaser that gives only the आरोप for a case that was already decided \
is wrong by omission: readers take it as a live accusation. When the description \
reports no verdict, say nothing about an outcome — do not guess, and do not write \
"अनुसन्धान जारी" unless the description says so.
- IF THE DEFENDANTS GOT DIFFERENT OUTCOMES, SAY SO. When the description reports \
one defendant दोषी and another सफाई, a teaser naming only one of them is false \
about the other — and a person a court CLEARED must never read as convicted. Name \
the split plainly: "…एक प्रतिवादीलाई दोषी ठहर, अर्कोलाई सफाई" or \
"…प्रधानाध्यापकलाई कैद, लेखा कर्मचारीलाई सफाई". Prefer naming who was cleared over \
listing the sentence in full — the sentence is on the case page, the correction of \
a false impression is not.

OUTPUT FORMAT — return ONLY a single JSON object, no markdown fences, no prose.
Include only the requested key(s); set an unrequested key to null:
"""
    + f'{{"title": "{_TITLE_PLACEHOLDER}", '
      f'"short_description": "{_SHORT_PLACEHOLDER}"}}\n'
)

USER_PROMPT = """\
{request_note}

Current title: {current_title}
Special-court case number (MUST end any regenerated title): {court_number}
Bigo (बिगो), NPR: {bigo}

KEY ALLEGATIONS:
{key_allegations}

NAMED ENTITIES (accused / related / location). A `फैसला:` label on an entity is
that person's own outcome — use it to get a split verdict right. Any other trailing
text is an INTERNAL caseworker note: read it for context, never quote or paraphrase
it, and never treat it as a published fact.
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


#: Words that state a RESULT -- what the court decided, not what anyone asked
#: for. `फैसला` ("judgment") is deliberately ABSENT even though a verdict section
#: is titled with it: it is also the word an appeal section uses to refer BACK to
#: the judgment, so including it drags the locator past the verdict it exists to
#: find. Measured over the corpus, `पुनरावेदन` appears after the last outcome
#: word on 16 of the 83 descriptions long enough to need this path.
_OUTCOME_WORD_RE = re.compile(r"(सफाई|सफाइ|दोषी|ठहर|जरिवाना|कैद)")

#: How much of the description's OPENING the snippet keeps. The rest of the
#: budget goes to the verdict.
SNIPPET_HEAD_CHARS = 1800

#: Ceiling on the whole snippet once a verdict has been spliced in -- what stops
#: a 13,617-character judgment summary from filling the prompt.
SNIPPET_MAX_CHARS = 6000

#: How far past the last outcome word the window runs, so the sentence stating
#: the outcome is never cut off mid-clause.
_OUTCOME_TRAILING_CHARS = 200


def _verdict_window(text):
    """`(start, end)` of the verdict passage, or None when no outcome is stated.

    THE WINDOW ENDS AT THE LAST OUTCOME WORD AND GROWS BACKWARD. Every
    start-somewhere-and-read-forward design loses verdicts, because no single
    point reliably marks where the outcome discussion begins:

      - Read forward from the FIRST outcome word past the head, and a
        mid-narrative hit hijacks the slice. One real case matches `दोषी` at
        6,466 while the actual ठहर sits at 26,224.
      - Read forward from the LAST outcome word, and a multi-defendant verdict
        loses every defendant but the last one named.
      - Read forward from the `ग)` section heading, and a long verdict
        discussion is clipped before it finishes. Measured: preferring the
        heading scores WORSE than ignoring it (51/54 vs 53/54 on carrying the
        acquittal), which is why this function has no heading branch at all.

    Ending the window at the last outcome word and filling the budget backward
    dominates all three, because the outcome discussion is contiguous and ends
    where the last outcome word is. Measured over the 83 descriptions longer than
    the snippet budget, on whether the passage the model actually receives
    contains the court's granted acquittal:

        plain head clamp        2/54       (and 7/76 on the last outcome stated)
        read forward from last  42/54      (62/76)
        this function           53/54      (76/76)

    The one remaining miss is a false one -- `सफाइ दिने अवसर` ("an opportunity to
    be heard") is a due-process phrase, not a verdict.
    """
    matches = [m for m in _OUTCOME_WORD_RE.finditer(text)
               if m.start() >= SNIPPET_HEAD_CHARS]
    if not matches:
        return None
    end = min(len(text), matches[-1].end() + _OUTCOME_TRAILING_CHARS)
    start = max(SNIPPET_HEAD_CHARS, end - (SNIPPET_MAX_CHARS - SNIPPET_HEAD_CHARS))
    # Align to a line boundary so the passage does not open mid-sentence, moving
    # FORWARD. Backing up to the PREVIOUS line start is the obvious reading and
    # is wrong: it grows the window by up to a whole line, which put 50 of the 83
    # real snippets over `SNIPPET_MAX_CHARS` (the worst 6,998). Moving forward
    # can only shrink it, so the ceiling holds. A text with no newline at all
    # simply keeps the computed start -- `find` returning -1 must not be read as
    # a boundary, which is the trap on the other side of this.
    if start > SNIPPET_HEAD_CHARS:
        line_start = text.find("\n", start)
        if 0 <= line_start < end:
            start = line_start + 1
    return start, end


def _snippet(detail):
    """The description text fed to the prompt, or '(none)'.

    A HEAD CLAMP ALONE LOSES THE OUTCOME. A description states the allegation
    first and the verdict last, so the outcome is structurally late. Measured
    across the published cases whose description exceeds this budget and states
    an outcome at all: **34 of 52 state it only beyond 3,000 characters**. So a
    plain head clamp hides the verdict from the teaser on about two thirds of
    decided cases, and no prompt rule can recover it -- the model cannot state
    what it was never shown. The 2026-08-04 evaluation caught exactly that on
    `081-CR-0060`, an acquitted case (सफाई at offset 5,100) whose generated
    teaser read as a live accusation.

    So when an outcome exists outside the head, the snippet becomes
    head + elision + the verdict passage.

    A MULTI-DEFENDANT VERDICT IS THE CASE THAT MUST NOT BE CLIPPED. Such a case
    states one outcome per defendant, so any slice that ends early keeps the
    first (usually a conviction) and drops the rest. Reproduced twice on
    `078-CR-0103`, where राकेशमान श्रेष्ठ's सफाई sat past where the slice ended --
    the model was shown the conviction, never the acquittal, and wrote a teaser
    reading as though both defendants were convicted. `_verdict_window` exists to
    make that structurally hard; see its docstring for the measurement.

    Why a ceiling instead of no limit: three in four verdict passages are under
    3,337 characters, but the longest is 13,617, and one outlier judgment must
    not dominate the prompt. Input tokens are the cheap half of this stage; the
    output cap (`CARD_MAX_TOKENS`) is what actually bounds cost.
    """
    text = (detail.get("description") or "").strip()
    if not text:
        return "(none)"
    if len(text) <= DESCRIPTION_SNIPPET_BUDGET:
        return text

    window = _verdict_window(text)
    # Nothing to rescue when the text states no outcome past the head -- an
    # outcome inside the head is already kept by the plain clamp.
    if window is None:
        return text[:DESCRIPTION_SNIPPET_BUDGET]

    start, end = window
    head = text[:SNIPPET_HEAD_CHARS].rstrip()
    return f"{head}\n\n[…]\n\n{text[start:end].strip()}"


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
        # UPPERCASED. The model copies this string into the title verbatim, and
        # `court_number()` reads it off the canonical IRI, which is lowercase
        # (`.../courtcase/special/081-cr-0060`). Feeding the lowercase form
        # produced public headlines ending in `(081-cr-0060)` on 2 of 5 cases in
        # the 2026-08-04 evaluation, against 50 of 50 PUBLISHED titles that use
        # `(081-CR-0060)`. `validate_title` compares case-insensitively, so
        # nothing rejected it -- the only fix is to hand the model the right form.
        court_number=(number or "").upper() or "(unknown)",
        bigo=format_bigo(detail.get("bigo")),
        key_allegations=format_list(detail.get("key_allegations")),
        entities=format_entities(detail.get("entities")),
        description=_snippet(detail),
    )


def _carries_a_field(obj, wanted=(TITLE_FIELD, SHORT_FIELD)):
    """True when the object holds a NON-BLANK STRING on a field this run WANTS.

    Key presence is not enough, and this is the whole point. The system prompt
    tells the model to "set an unrequested key to null", so
    `{"title": null, "short_description": null}` is a shape the prompt itself
    invites -- and a presence test (`TITLE_FIELD in o`) accepts it, which stops
    `parse_object_response`'s scan on an object carrying nothing. The run then
    reports two rejections ("LLM returned an empty ...") for a response whose
    REAL object may have been the next `{` along, behind a preamble.

    `wanted` IS NOT DECORATION. Checking both fields unconditionally reopens the
    same hole one field over: on an `--only title` run,
    `{"title": null, "short_description": "..."}` satisfies "carries a field",
    the scan stops there, and the title is then rejected at vetting ("LLM
    returned no title") while the object that actually had one -- behind a
    preamble the model added -- is never looked at. The predicate has to ask about
    the fields the run can write, not about the union of both.
    """
    return any(
        isinstance(obj.get(field), str) and obj.get(field).strip()
        for field in wanted
    )


def _generate(detail, number, need_title, need_short, invoke_text, usage):
    """One cheap-tier call producing whichever fields were asked for.

    Returns the parsed dict, or None. Accepts an object carrying any field this
    run ASKED FOR -- a run wanting only a title gets `{"title": ...,
    "short_description": null}`, so demanding both would reject every single-field
    reply, and accepting either would let a null title through on an
    `--only title` run. See `_carries_a_field`.
    """
    response_text = invoke_text(
        system=SYSTEM_PROMPT,
        content=_build_prompt(detail, number, need_title, need_short),
        max_tokens=CARD_MAX_TOKENS,
        tier=tier_for("card"),
        usage=usage,
    )
    wanted = tuple(f for f, need in ((TITLE_FIELD, need_title),
                                    (SHORT_FIELD, need_short)) if need)
    # TWO PASSES, NARROW THEN BROAD. The narrow pass skips an object that carries
    # only the field this run cannot write -- otherwise an `--only title` run
    # accepts `{"title": null, "short_description": "..."}`, stops scanning, and
    # reports "LLM returned no title" while the object that HAD one, behind a
    # preamble, is never looked at.
    #
    # The broad pass exists so narrowing does not cost error precision. When the
    # model's real answer carries the wanted field but leaves it BLANK, the narrow
    # pass rejects it and, alone, would report "no card JSON object found" -- true
    # of the predicate, misleading to an operator whose model actually returned an
    # empty teaser. Falling back lets vetting name the real problem. Both passes
    # are pure string work; neither costs an LLM call.
    result = parse_object_response(
        response_text,
        predicate=lambda obj: _carries_a_field(obj, wanted),
    )
    if result is None:
        result = parse_object_response(response_text, predicate=_carries_a_field)
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

    The placeholder check is the only content gate this field has -- unlike
    `title`, it has no format contract for `validate_title` to enforce. See
    `_SHORT_PLACEHOLDER`.
    """
    short = (candidate or "").strip()
    if not short:
        return None, "LLM returned an empty short_description"
    if _SHORT_PLACEHOLDER in short:
        return None, ("echoed the OUTPUT FORMAT placeholder "
                      f"({_SHORT_PLACEHOLDER!r}) instead of answering")
    if len(short) > MAX_SHORT_DESCRIPTION_CHARS:
        return None, f"too long: {len(short)} > {MAX_SHORT_DESCRIPTION_CHARS} chars"
    return short, None


@dataclass
class _WriteContext:
    """Everything `_vet_and_write` needs that is the same for both fields.

    A dataclass rather than a closure inside `main()`: both fields go through
    identical vet -> record -> log machinery, and writing that twice is how the
    title path and the short_description path drift until one of them forgets a
    review row.

    `etag` is read once per case and never rewritten -- the single multi-op PATCH
    in `_write_fields` is the only write, so there is nothing to refresh.
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


def _record(ctx, field, current, status, note, generated):
    """One report row + one review row for one field's outcome.

    Shared by the vet and the write halves so a rejection and a successful write
    cannot drift into describing themselves differently.
    """
    ctx.report.record(ctx.slug, "card", status, f"{field}: {note}")
    ctx.review.add(ReviewRow(
        slug=ctx.slug, status=f"{status} ({field})", before=current,
        generated=generated, sources=ctx.sources, note=note))


def _vet_field(ctx, field, current, candidate, vet):
    """Vet one generated value. Returns it, or None when it was refused.

    Vetting is deliberately separate from writing: a bad headline must not cost
    the case its teaser, and that independence lives HERE, before any request is
    made -- not in the number of PATCHes issued.
    """
    value, rejection = vet(candidate)
    if rejection:
        _record(ctx, field, current, "rejected", rejection,
                (candidate or "").strip())
        log_event(ctx.logger, ctx.events_path, run_id=ctx.run_id, stage="card",
                  slug=ctx.slug, step=f"vet:{field}", status="rejected",
                  detail=rejection, level=logging.WARNING)
        return None
    return value


def _write_fields(ctx, accepted):
    """Write every field that passed vetting in ONE conditional PATCH.

    `accepted` is `[(field, current, value)]`.

    WHY ONE REQUEST AND NOT ONE PER FIELD. A PATCH changes the case's ETag, so a
    second request carrying the ETag read at the top of the case fails `If-Match`
    with a 412 -- always. This script shipped as two `patch_field` calls and could
    therefore never write both `title` and `short_description` in one pass: the
    2026-08-04 smoke run wrote the title and took
    "HTTP Error 412: Precondition Failed" on the teaser for every case. A dry run
    cannot see it, because it issues no PATCH at all and reports `would-enrich`
    for both.

    Sending both ops in one request removes that failure rather than handling it,
    and buys atomicity: the server applies the whole array against a single
    snapshot, so a half-written card -- new headline, stale teaser -- stops being
    a reachable state.
    """
    if not accepted:
        return

    if ctx.dry_run:
        for field, current, value in accepted:
            _record(ctx, field, current, "would-enrich", value, value)
            log_event(ctx.logger, ctx.events_path, run_id=ctx.run_id,
                      stage="card", slug=ctx.slug, step=f"write:{field}",
                      status="would-enrich", detail=f"{field}={value}")
        return

    try:
        ctx.api.patch_fields(
            ctx.slug, [(f, v) for f, _, v in accepted], if_match=ctx.etag)
    except Exception as exc:
        # The write is atomic, so the failure is too: every field is reported
        # failed, because the server holds `current` for all of them.
        for field, current, value in accepted:
            _record(ctx, field, current, "error", f"PATCH failed: {exc}", value)
            log_event(ctx.logger, ctx.events_path, run_id=ctx.run_id,
                      stage="card", slug=ctx.slug, step=f"write:{field}",
                      status="error", detail=str(exc), level=logging.ERROR)
        return

    for field, current, value in accepted:
        _record(ctx, field, current, "enriched", value, value)
        log_event(ctx.logger, ctx.events_path, run_id=ctx.run_id, stage="card",
                  slug=ctx.slug, step=f"write:{field}", status="enriched",
                  detail=f"{field}={value}")


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

    api = build_api(args)
    usage = UsageAccumulator()
    report = RunReport()
    # The review file names both fields: one run can write either or both, and a
    # reviewer has to see which.
    review = build_review_file(
        args, stage="card", field_name=f"{TITLE_FIELD} + {SHORT_FIELD}", run_id=run_id)

    all_cases = list(api.iter_cases())
    # `select_for_run` is the one selection path every enricher shares (#410): it
    # applies the --batch-csv allowlist, then the other selectors, then --limit --
    # and it slices --limit in BATCH order, which a local `cases[:limit]` cannot
    # do because the API's iteration order is not the CSV's.
    cases = select_for_run(all_cases, args)

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
            # NOT the donor's fall-back-to-the-list-payload. This stage must skip.
            #
            # The donor (and `enrich_description`) continue on `detail = case`,
            # and for description that is safe by accident: its
            # `requires_materials` gate trips on the list payload's unresolved
            # `associatedMedia`, so the case is reported unmet and never
            # generated. `STAGES["card"]` declares NO `requires_materials` -- it
            # reads no document -- so nothing would stop the run here.
            #
            # What would happen instead is the bad case: `CaseSerializer` is the
            # list serializer's child (`cases/serializers.py:312`), so the list
            # payload carries `title`, `short_description` AND `description`.
            # Card would generate a headline from a page-cached description and
            # then PATCH with `if_match=None`, because the failed fetch is
            # exactly what left `etag` unset -- an unconditional write that
            # silently clobbers whatever a caseworker changed in between. A
            # skipped case costs one re-run; a clobbered edit is unrecoverable.
            report.record(slug, "card", "error", f"detail fetch failed: {exc}")
            review.add(ReviewRow(slug=slug, status="error",
                                 before=case.get("title") or "",
                                 note=f"detail fetch failed: {exc}"))
            log_event(logger, paths["events"], run_id=run_id, stage="card", slug=slug,
                      step="fetch", status="error", detail=str(exc),
                      level=logging.ERROR)
            continue

        # A 200 THAT CARRIES NO ETag IS THE SAME HOLE AS A FAILED FETCH. The
        # reasoning above guards the exception route, but `if_match` is only sent
        # when it is truthy (`common/api.py`) and the server only checks it when
        # present -- so a response that simply lacks the header (a proxy stripping
        # it, a non-`retrieve` path) also produces an unconditional write, with no
        # log line saying so. `cases/api_views.py` does set it today, which makes
        # this latent rather than live; it is one check to close, and the argument
        # above applies unchanged: a skipped case costs one re-run, a clobbered
        # caseworker edit is unrecoverable.
        if not etag:
            reason = ("case detail returned no ETag; refusing to write "
                      "unconditionally (would clobber a concurrent edit)")
            report.record(slug, "card", "error", reason)
            review.add(ReviewRow(slug=slug, status="error",
                                 before=detail.get("title") or "", note=reason))
            log_event(logger, paths["events"], run_id=run_id, stage="card", slug=slug,
                      step="fetch", status="error", detail=reason,
                      level=logging.ERROR)
            continue

        current_title = detail.get("title") or ""
        current_short = detail.get("short_description") or ""
        # Whole-case rows (unmet / already / error / skipped) concern both fields
        # or neither, so the "Before" column has to reflect what the RUN is about.
        # Reporting `current_title` unconditionally showed a reviewer the headline
        # in every row of an `--only short_description` run -- the one field that
        # run could not touch.
        current_for_review = current_title if want_title else current_short
        number = court_number(detail)

        unmet = unmet_prerequisites(STAGE, detail)
        # THE EMPTINESS TEST IS NOT ENOUGH, so the substance check belongs here --
        # before the adequacy judge below, which is itself an LLM call and would
        # otherwise be billed for a case that cannot be carded.
        #
        # `unmet_prerequisites` rejects `description` only when it is `None`, `""`,
        # `[]` or `{}`. A whitespace-only description passes it and reaches the
        # prompt as `DESCRIPTION: (none)`; a one-line template stub reaches it as
        # the entire factual basis for a headline TITLE_RULES asks to name the
        # principal accused with a quantifiable hook. Either way the model is asked
        # for facts it was never given. An earlier comment here claimed a second
        # check would be unreachable code -- that was wrong, and being wrong is the
        # reason no guard existed.
        substance = len((detail.get("description") or "").strip())
        if not unmet and substance < SUBSTANTIAL_DESCRIPTION_CHARS:
            unmet = [f"description is only {substance} chars, under the "
                     f"{SUBSTANTIAL_DESCRIPTION_CHARS}-char threshold the "
                     "description stage itself uses for a real description"]
        if unmet:
            for reason in unmet:
                report.record(slug, "card", "unmet", reason)
            review.add(ReviewRow(slug=slug, status="unmet", before=current_for_review,
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
            review.add(ReviewRow(slug=slug, status="already", before=current_for_review,
                                 note="nothing to do (use --force)"))
            continue

        try:
            result = _generate(detail, number, need_title, need_short,
                               invoke_text, usage)
        except Exception as exc:
            report.record(slug, "card", "error", f"LLM generation failed: {exc}")
            review.add(ReviewRow(slug=slug, status="error", before=current_for_review,
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
            review.add(ReviewRow(slug=slug, status="skipped", before=current_for_review,
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
        # Vet first, write once. Each field can still be refused on its own --
        # what it cannot do any more is cost the other field a 412.
        accepted = []
        if need_title:
            value = _vet_field(ctx, TITLE_FIELD, current_title,
                               result.get(TITLE_FIELD),
                               lambda c: vet_title(c, number))
            if value is not None:
                accepted.append((TITLE_FIELD, current_title, value))
        if need_short:
            value = _vet_field(ctx, SHORT_FIELD, current_short,
                               result.get(SHORT_FIELD), vet_short_description)
            if value is not None:
                accepted.append((SHORT_FIELD, current_short, value))
        _write_fields(ctx, accepted)

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

    print(f"review file: {review.write()}")

    log_run_footer(
        logger, stage="card", stats=stats,
        duration_s=time.monotonic() - start_time, usage_summary=usage_summary,
    )

    return report


if __name__ == "__main__":
    main()
