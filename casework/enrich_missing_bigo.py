#!/usr/bin/env python
"""Extract missing BIGO (बिगो, disputed amount) for CIAA cases via LLM. LOCAL WRITES ONLY.

Ported from the deleted `casework/enrich_missing_bigo.py` (recovered at donor
commit `0321a85`). Reads a case's CIAA press-release source text entirely over
the Jawafdehi HTTP API and asks the premium LLM tier to extract the official
disputed-amount figure (बिगो) the CIAA/prosecution claims. Never touches the
database directly -- writes go through `CaseworkApi.patch_field`, which this
project's binding constraint restricts to loopback (`127.0.0.1:48010`) only.

The single highest-risk piece of logic here is `coerce_bigo_int`: CIAA press
releases write the disputed amount with a paisa suffix separated by a danda
'।', a slash '/', or a dot '.' (e.g. २३,७५,४६,३२४।५७). Blindly stripping every
non-digit character folds the paisa digits into the rupee figure and inflates
the amount 10-100x -- this exact bug reached production on case 080-CR-0158.
See `coerce_bigo_int`'s body/comment for the fix; it is ported verbatim from
the donor and must not be "cleaned up".

Usage:
    uv run python -m casework.enrich_missing_bigo --dry-run
    uv run python -m casework.enrich_missing_bigo --slug case-0123
    uv run python -m casework.enrich_missing_bigo --limit 10 --verbose
    uv run python -m casework.enrich_missing_bigo --apply
"""

import argparse
import json
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
    format_counts,
    log_event,
    log_run_footer,
    log_run_header,
    print_summary,
    setup_logging,
)
from casework.common.llm import bootstrap, tier_for
from casework.common.materials import materials_of_type, source_text
from casework.common.parse import balanced_object
from casework.common.pipeline import PRESS_TYPES, STAGES, RunReport, unmet_prerequisites
from casework.common.select import select_for_run

log = logging.getLogger("casework.enrich_missing_bigo")

STAGE = STAGES["bigo"]

# Nepali keywords that signal BIGO context. Ported verbatim from the donor --
# `parse_bigo_response` refuses to trust an LLM-reported amount unless its
# `evidence_quote` contains one of these, which is what stops the model
# confidently mislabelling a जम्मा/कूल subtotal (or a bribe/fine amount) as
# the bigo.
BIGO_CONTEXT_KEYWORDS = (
    "बिगो",
    # ब<->व legacy-font variants. These press releases are legacy-font PDFs, not
    # scans, so the same word renders both ways -- often inside one document.
    # Across the FY078/079 238-case batch: विगो is 150 of the 796 bigo tokens, and
    # मागदावी appears in 156 of the 238 documents while मागदाबी appears in 129.
    # Listing only the ब spelling made this gate discard correct, high-confidence
    # extractions: 079-CR-0080 returned bigo=7000 quoting "विगो रु.7,000।- ... कायम
    # गरी" and was recorded as "could not extract". Measured over the batch,
    # विगो-only documents had a 0% success rate against 100% for बिगो-only ones.
    "विगो",
    "मागदाबी",
    "मागदावी",
    "हानि",
    "हानी",
    "नोक्सानी",
    "क्षति",
    "damage claim",
    "loss amount",
    "corruption loss",
)

_NEPALI_TO_ASCII_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")

# Feed-size limits. The donor read these from `CASEWORK_BIGO_SOURCE_CHARS` /
# `CASEWORK_BIGO_FEED_CHARS` via an `env_int()` helper that lived in the
# deleted `casework/common.py` and was never re-created in the Task 5-11
# common package (no env-configurable knob exists anywhere else in
# `casework.common`) -- so these are fixed constants here, at the donor's own
# default values, rather than invented new env var names.
SOURCE_CONTEXT_CHARS = 20000
FEED_CHARS = 100000

EXTRACTION_SYSTEM_PROMPT = """\
You extract BIGO (बिगो), the damage claim amount / मागदाबी, from CIAA press release \
content.

Return STRICT JSON only with this schema:
{
  "bigo": <integer or null>,
  "confidence": "high" | "medium" | "low",
  "evidence_quote": "<short quote from text that supports the amount>",
  "press_release_type": "sting_operation" | "appeal_review" | "charge_filing" | "other"
}

CRITICAL RULES (apply in order):

Rule 1 — Output format
BIGO must be an NPR integer only. No commas, no currency symbols (रू/Rs/NPR), no \
paisa suffix, no floats.
CIAA marks paisa with a danda '।' OR a slash '/' (e.g. १,४६,८१,२२५।९० or \
१,४६,८१,२२५/९०). If the extracted amount has a paisa portion, strip everything \
after the '।' or '/' before returning — return the rupee part only.

Rule 2 — Numeral normalization
Before any matching, normalize Devanagari digits to Arabic (०→0, १→1, ... ९→9). CIAA \
PDFs mix both in the same number.
Then strip commas. Then strip the paisa suffix (everything from the first '।' or \
'/'). Then parse as integer. Never let paisa digits merge into the rupee figure — \
e.g. २३,७५,४६,३२४।५७ is 237546324, NOT 2375463245.

Rule 3 — Type-routing, but only when no बिगो is declared
First check whether the document declares a बिगो anywhere -- "बिगो रु.<AMOUNT> ... \
कायम गरी", or an amount in a "बिगो रु." table column. If it does, EXTRACT IT and \
ignore the routing below entirely. CIAA formally establishes a bribe AS the बिगो under \
दफा ३(१) in 65 of the 238 FY078/079 releases, so routing on document type alone would \
return null on a figure the document states outright.
Only when NO बिगो is declared does the type matter:
- Sting Operation (रंगेहात, sting, caught red-handed) → return null (high confidence). \
  The amounts are physical cash caught during arrest -- bribe/unexplained cash -- not a \
  formally established bigo.
- Appeal/Review (पुनरावेदन, अपील, appeal, review) → return null (high confidence). \
  These record CIAA appealing a court verdict; bigo was defined at charge-sheet stage \
  and is not re-stated here.
- Charge Filing (अभियोग दायर, charge filed, मुद्दा दर्ता), or anything else → proceed to \
  the extraction rules below.
Rule 8's ignore list already stops bare seized cash from being read as बिगो; this rule \
must not become a second, blunter gate that fires on a stated figure.

Rule 4 — Null with low confidence
If no reliable bigo signal exists after all checks, return null with low confidence. \
Do not guess.

Rule 5 — No ranges, floats, or formatted strings
Never return ranges (१-५ लाख), floats (1.5 करोड), or formatted strings. Integer only.

Rule 6 — Priority signal hierarchy (apply in order, stop at first match)
1. Title contains "बिगो रू.[AMOUNT] कायम गरी" → extract AMOUNT → high confidence
2. PDF table row under "बिगो रू." column header → extract amount → high confidence
3. PDF body sentence "कूल आय भन्दा कूल व्यय ... [AMOUNT] ले बढी" (excess of \
   expenditure over income) → extract AMOUNT → medium confidence, verify it \
   matches signal 2
4. No match → null

Note: "बिगो बमोजिम जरिवाना" is a reference to an already-stated bigo, not a \
declaration of it — use it only to confirm, not as a primary source.

Rule 7 — Multiple amounts: label is mandatory
When multiple monetary amounts appear in the text, extract only the one explicitly \
labeled as bigo using the hierarchy in Rule 6.
If multiple amounts are present and none carries a clear bigo label, return null — \
do not pick the largest, the first, or the one that "seems right."

Rule 8 — Ignore list (NEVER extract these as bigo)
Amount type | Nepali marker
------------|---------------
Bribe received/demanded | घुस/रिसवत रकम रू.
Unexplained cash seized | स्रोत नखुलेको रकम रू.
Lawful income subtotal | जम्मा/कूल आय रू.
Expenditure subtotal | जम्मा/कूल व्यय रू.
Fine/penalty | जरिवाना रू.
Contract/budget amount | ठेक्का रकम, बजेट रकम
Asset seizure value | जफत गर्ने सम्पत्ति रू.
Co-accused row with — | no amount; asset forfeiture only

IMPORTANT: Income and expenditure subtotals are always larger than bigo in illegal \
property cases. If you accidentally extract either, the number will be bigger than \
the bigo stated in the table. Use this as a sanity check.

Rule 9 — Vague/verbal amounts → null
If the amount is expressed in vague prose (करोडौं, अरबौं, लाखौं) with no \
accompanying numeric, return null.
Word-amount parsing of Nepali number words is error-prone and CIAA's structured \
documents always pair prose amounts with numerics when a formal bigo exists.
"""

EXTRACTION_USER_PROMPT = """\
Extract the BIGO (damage claim amount) from the following CIAA press release.

Case ID: {case_id}
Case title: {case_title}

Source metadata (title, description, filenames, URLs):
{source_context}

Press release markdown:
{markdown}
"""


def _clamp(text: str, limit: int, label: str = "source") -> str:
    """Truncate `text` to `limit` chars (<=0 = no limit) and PRINT total vs sent,
    so an operator can see how much of each source actually reached the model."""
    text = text or ""
    total = len(text)
    sent = text if (limit <= 0 or total <= limit) else text[:limit]
    note = "" if len(sent) == total else f"  (capped at {limit:,})"
    print(f"    {label}: {total:,} total chars, sent {len(sent):,}{note}")
    return sent


def _source_metadata(case: dict, types) -> str:
    """Best-effort source metadata for the prompt: case title + bound material
    display names/types/URLs. Replaces the donor's
    `_build_source_context_from_entry`, which read `DocumentSource.title`/
    `.description` -- fields that don't exist on the current
    `{material_iri, material: {material_type, urls}}` evidence shape (see
    `casework.common.materials`).

    `material.display_name` is this schema's analog to the donor's
    `source.title` (the same convention `review/jds_client.py` uses:
    `"title": mat.get("display_name") or ""`) -- and, per the CIAA drafting
    convention, is frequently where the बिगो amount itself is first stated
    (e.g. "... उपर बिगो रु.९०,३९,६२०।३९ कायम"). Surface it first so it carries
    the same weight it did in the donor's prompt."""
    lines = [f"case title: {case.get('title') or ''}"]
    for material in materials_of_type(case, types):
        urls = [
            u.get("link") for u in (material.get("urls") or [])
            if isinstance(u, dict) and u.get("link")
        ]
        lines.append(
            f"display_name: {material.get('display_name') or ''}; "
            f"material_type: {material.get('material_type') or '?'}; "
            f"urls: {'; '.join(urls)}"
        )
    return "\n".join(lines)


def coerce_bigo_int(value) -> Optional[int]:
    """Coerce a value to a positive integer or None."""
    if value is None:
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float):
        return int(value) if value > 0 else None
    if not isinstance(value, str):
        return None

    normalized = value.translate(_NEPALI_TO_ASCII_DIGITS)
    # CIAA writes paisa after a danda '।', slash '/', or dot '.' (e.g. ...३२४।५७,
    # ...२२५/९०, ...324.57). OCR frequently misreads the danda '।' as a vertical
    # pipe '|', so treat that as a separator too. Stripping all non-digits would
    # fold the paisa digits into the rupee figure and inflate it 10-100x (e.g.
    # 237546324।57 -> 2375463245). Anchor at the first digit before cutting paisa
    # so a leading currency prefix ('रु.') isn't mistaken for a paisa separator.
    # Rupee grouping uses commas only, so cutting at the first '।'/'|'/'/'/'.'
    # after the digits is safe.
    first_digit = re.search(r"\d", normalized)
    if not first_digit:
        return None
    rupees = re.split(r"[।|/.]", normalized[first_digit.start() :], maxsplit=1)[0]
    digits_only = re.sub(r"[^\d]", "", rupees)
    if not digits_only:
        return None
    bigo = int(digits_only)
    return bigo if bigo > 0 else None


def rupee_amounts_in(text: str) -> set:
    """Every amount stated in `text`, reduced to its rupee part.

    Each match is normalised by `coerce_bigo_int` rather than re-implementing the
    paisa rule here. That keeps ONE definition of "where does the rupee part
    end", so the two sides of the grounding comparison cannot drift: an earlier
    copy here omitted the '.' separator that `coerce_bigo_int` honours, so
    'रु.324.57' reduced to 324 on the model's answer but yielded {57, 324} here.

    Two tokenisation rules earn their keep, both measured against the 238
    FY078/079 sources:

    - **The paisa tail is capped at two digits and must not be followed by more
      digits or a comma.** Paisa is 0-99, and without the cap the OCR'd-danda
      alternative '|' swallows markdown table pipes: `| 1 | 35,200 |` parsed as
      "1 rupees 35 paisa" and dropped 35200 from the set entirely. A बिगो stated
      in a table column is Rule 6's *high-confidence* signal #2, so that made the
      gate reject correct extractions and silently skip the case. 2 of the 238
      sources contain the `<digits> | <digits>` shape.
    - **Comma-separated groups may be split by whitespace.** Markdown extraction
      turns 'रु.1,38,99,998।87' into 'रु. 1, 38, 99, 998।87' in 8 of the 238
      sources; without this the figure is unreachable and a correct answer looks
      ungrounded.

    Deliberately NOT done: adding the un-truncated reading of a paisa-bearing
    token. For 'रु.1,46,81,225।90' that reading is 1468122590 -- precisely the
    paisa-fold this project exists to keep out of production (080-CR-0158). A
    gate that accepted it would bless the exact error class it was built to
    catch. Over-merging across unrelated numbers ('दफा 8, 9' -> 89) is tolerated
    instead: a spurious extra member only makes the gate more permissive, while a
    missing member destroys a correct figure.

    The digit run is matched greedily, so a short number can never "appear"
    merely by being a prefix of a longer one: '1389999887' yields 1389999887,
    never 138999998.
    """
    # Digit groups joined by commas (tolerating the spaces markdown extraction
    # leaves behind), then at most a two-digit paisa tail. Separators match
    # `coerce_bigo_int`'s: danda, OCR'd pipe, slash, dot.
    runs = re.finditer(
        r"\d+(?:\s*,\s*\d+)*(?:\s*[।|/.]\s*\d{1,2}(?![\d,]))?",
        text or "",
    )
    return {
        amount for amount in (coerce_bigo_int(m.group(0)) for m in runs)
        if amount is not None
    }


def amount_is_grounded(text: str, bigo) -> bool:
    """True when `bigo` is actually stated in the source.

    The last line of defence, and the only one that catches a model returning a
    clean-but-wrong integer. Neither of the other two guards can:
    `coerce_bigo_int` sees a well-formed int and passes it through, and
    `is_explicit_bigo_context` only inspects the quote, which is usually copied
    correctly even when the number is not.

    Two real failures this stops, both from the FY078/079 dry run:

    - 078-CR-0116 returned 138999998 where the charge sheet declares
      रु.१,३८,९९,९९८।८७ (13,899,998) -- a 10x inflation whose digits appear
      nowhere in the document.
    - 079-CR-0067 returned 268000, the arithmetic sum of three per-defendant
      विगो figures (35,200 + 55,200 + 177,600). A real total, but one the
      document never states.

    It does NOT resolve which बिगो to pick when a document declares several --
    every candidate is grounded by definition. That is a data-modelling
    question, not an extraction one (see the multi-accused note in
    `work/2026-08-03-Dry-run-bigo-enricher/report.md`).
    """
    if bigo is None:
        return True
    return bigo in rupee_amounts_in(text)


def is_explicit_bigo_context(evidence_quote) -> bool:
    """Check if evidence quote contains explicit BIGO context keywords."""
    if not isinstance(evidence_quote, str):
        return False
    normalized_quote = evidence_quote.strip().lower()
    if not normalized_quote:
        return False
    return any(keyword in normalized_quote for keyword in BIGO_CONTEXT_KEYWORDS)


def parse_bigo_response(response_text: str) -> Optional[int]:
    """Parse the LLM response into a BIGO integer or None."""
    text = response_text.strip()

    # Try to extract JSON (plain or fenced)
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            bigo_val = obj.get("bigo")
            confidence = str(obj.get("confidence", "")).strip().lower()
            evidence_quote = obj.get("evidence_quote")

            # Skip if low confidence
            if confidence == "low":
                return None

            # Skip if no explicit BIGO context
            if not is_explicit_bigo_context(evidence_quote):
                return None

            return coerce_bigo_int(bigo_val)
    except json.JSONDecodeError:
        pass

    # Try fenced JSON
    fenced = re.search(r"```(?:json)?\s*(\{[^}]*\})\s*```", text, re.DOTALL)
    if fenced:
        try:
            obj = json.loads(fenced.group(1))
            if isinstance(obj, dict):
                bigo_val = obj.get("bigo")
                confidence = str(obj.get("confidence", "")).strip().lower()
                evidence_quote = obj.get("evidence_quote")

                if confidence == "low":
                    return None
                if not is_explicit_bigo_context(evidence_quote):
                    return None

                return coerce_bigo_int(bigo_val)
        except json.JSONDecodeError:
            pass

    # Scan for balanced JSON objects (string-aware). A brace regex broke on
    # braces inside quoted fields like evidence_quote and on deeper nesting.
    for start in range(len(text)):
        if text[start] != "{":
            continue
        block = balanced_object(text, start)
        if not block:
            continue
        try:
            obj = json.loads(block)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "bigo" in obj:
            bigo_val = obj.get("bigo")
            confidence = str(obj.get("confidence", "")).strip().lower()
            evidence_quote = obj.get("evidence_quote")

            if confidence == "low":
                return None
            if not is_explicit_bigo_context(evidence_quote):
                return None

            return coerce_bigo_int(bigo_val)

    return None


def _extract_bigo(source_text_, case, invoke_text, usage) -> tuple[Optional[int], str]:
    """Call the LLM to extract BIGO from the press release.

    Returns `(bigo, shown)`, where `shown` is the source text this prompt
    actually carried -- the metadata block plus the clamped body, both post-
    truncation. The grounding gate checks that exact string rather than
    recomposing it, for two reasons: a second construction drifts the moment the
    prompt inputs change, and an unclamped rebuild would ground an amount
    against text the clamp had already cut out of the model's view.
    """
    source_context = _clamp(
        _source_metadata(case, PRESS_TYPES), SOURCE_CONTEXT_CHARS, "bigo context"
    )
    markdown = _clamp(source_text_, FEED_CHARS, "bigo source")
    prompt = EXTRACTION_USER_PROMPT.format(
        case_id=case.get("slug", "?"),
        case_title=case.get("title", ""),
        source_context=source_context,
        markdown=markdown,
    )

    response_text = invoke_text(
        system=EXTRACTION_SYSTEM_PROMPT,
        content=prompt,
        max_tokens=2000,
        tier=tier_for("bigo"),
        usage=usage,
    )

    return parse_bigo_response(response_text), f"{source_context}\n{markdown}"


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


def _run_event(logger, paths, run_id, step, status, detail="", level=logging.INFO):
    """Emit a RUN-scoped event (no slug) to the same events.jsonl as case events.

    `ledger.build_ledger` requires both a slug and a stage and skips any event
    missing either, so these rows describe the run without ever appearing as a
    phantom case in the ledger.

    This exists because everything before the per-case loop used to fail silently:
    a bootstrap error printed to stderr and exited, leaving the run's `.log` AND
    `.events.jsonl` at zero bytes with no record of the target, the mode, or the
    reason. `log_run_header`/`log_run_footer` only reach the `.log`, never the
    events stream the ledger actually reads.
    """
    log_event(logger, paths["events"], run_id=run_id, stage="bigo", slug="",
              step=step, status=status, detail=detail, level=level)


def main(argv=None):
    """Main entry point."""
    ap = argparse.ArgumentParser(
        description="Extract CIAA Special Court case BIGO via LLM (DB-free).",
        epilog="Reads/writes cases entirely over the Jawafdehi HTTP API.",
    )
    add_common_args(ap)
    args = ap.parse_args(argv)

    setup_logging(args.verbose)
    logger, run_id, paths = configure_run_logging("bigo", verbose=args.verbose)
    start_time = time.monotonic()

    # Put the run's identity on disk BEFORE anything that can fail. Selection is
    # what decides `n_selected`, so the header cannot move up here -- but target,
    # mode, provider and model are all known now, and they are exactly what you
    # need to interpret a crash during bootstrap or case listing.
    _run_event(
        logger, paths, run_id, "run", "start",
        f"target={args.api_base_url} mode={'DRY-RUN' if args.dry_run else 'APPLY'} "
        f"provider={args.provider} model={args.model or '(provider default)'} "
        f"allow_remote_writes={args.allow_remote_writes}",
    )

    def _die(step, exc):
        """Record a pre-loop failure in the run log, then exit non-zero."""
        detail = f"{type(exc).__name__}: {exc}"
        _run_event(logger, paths, run_id, step, "error", detail, level=logging.ERROR)
        log_run_footer(
            logger, stage="bigo", stats={"aborted": 1},
            duration_s=time.monotonic() - start_time,
        )
        print(f"{step} failed: {exc}", file=sys.stderr)
        sys.exit(1)

    # Bootstrap Django + LLM (MUST come before importing llm.invoke)
    try:
        bootstrap(args.provider, args.model)
    except Exception as exc:
        _die("bootstrap", exc)

    from llm.invoke import invoke_text
    from llm.usage import UsageAccumulator, render_usage_table

    usage = UsageAccumulator()
    report = RunReport()

    # `build_api` raises on a missing/ambiguous credential and `iter_cases` pages
    # the whole case list -- minutes of HTTP that an expired token or a 5xx can
    # sink. Unguarded, either one killed the run with a bare traceback.
    #
    # `SystemExit` is caught explicitly: the missing-credential path this guard
    # exists for goes through `basic_auth_from_env`, which raises `SystemExit`
    # (a BaseException, NOT an Exception). Catching only `Exception` let exactly
    # that case escape, leaving `run/start` as the last line in the events file
    # -- the same signature as a killed run, which is what these terminal events
    # were added to distinguish. KeyboardInterrupt is deliberately NOT caught.
    try:
        api = build_api(args)
    except (Exception, SystemExit) as exc:
        _die("build_api", exc)

    # Listing production is 16 requests and ~33s before the first case is even
    # looked at, and `--limit N` does not shorten it -- `select_for_run` slices
    # after the whole list is already in memory. Report each page through the
    # run logger, so the wait is legible live and its shape is on the record
    # afterwards -- an unnarrated 33s gap reads as a hang.
    def _list_progress(page, fetched, total):
        _run_event(
            logger, paths, run_id, "list_cases", "progress",
            f"page {page}, {fetched}/{total if total is not None else '?'} fetched",
        )

    try:
        all_cases = list(api.iter_cases(progress=_list_progress))
    except Exception as exc:
        _die("list_cases", exc)

    _run_event(logger, paths, run_id, "list_cases", "ok", f"{len(all_cases)} case(s) fetched")

    cases = select_for_run(all_cases, args)

    total = len(cases)
    log_run_header(
        logger, stage="bigo", base_url=args.api_base_url, dry_run=args.dry_run,
        provider=args.provider, model=args.model, n_selected=total,
        run_id=run_id, paths=paths,
    )
    _run_event(
        logger, paths, run_id, "select", "ok",
        f"{total} selected from {len(all_cases)} fetched",
    )
    if total == 0:
        print("No matching CIAA case(s) to process.", file=sys.stderr)
        print_summary(report.summary(), args.dry_run, "BIGO extraction")
        _run_event(logger, paths, run_id, "run", "complete", "0 selected; nothing to do")
        log_run_footer(
            logger, stage="bigo", stats=report.summary(),
            duration_s=time.monotonic() - start_time,
        )
        return report

    print(f"Found {total} matching case(s).")
    if args.force:
        print("  --force: re-extracting even for populated cases")
    if args.dry_run:
        print("  [DRY RUN] No changes will be saved.")

    for idx, case in enumerate(cases, 1):
        slug = case.get("slug") or "?"
        title = (case.get("title") or "")[:80]
        log_event(logger, paths["events"], run_id=run_id, stage="bigo", slug=slug,
                  step="start", status="start", detail=f"[{idx}/{total}] {title}")

        if case.get("bigo") and not args.force:
            report.record(slug, "bigo", "already", f"bigo already {case['bigo']}")
            log_event(logger, paths["events"], run_id=run_id, stage="bigo", slug=slug,
                      step="idempotency", status="already",
                      detail=f"bigo already {case['bigo']}")
            continue

        try:
            detail = api.get_case(slug)
        except Exception as exc:
            report.record(slug, "bigo", "error", f"case fetch failed: {exc}")
            log_event(logger, paths["events"], run_id=run_id, stage="bigo", slug=slug,
                      step="fetch", status="error", detail=str(exc),
                      level=logging.ERROR)
            continue

        # Cheap prerequisite check before paying for a markdown fetch: no
        # bound/converted press-release material at all.
        unmet = unmet_prerequisites(STAGE, detail)
        if unmet:
            for reason in unmet:
                report.record(slug, "bigo", "unmet", reason)
            log_event(logger, paths["events"], run_id=run_id, stage="bigo", slug=slug,
                      step="prereq", status="unmet", detail="; ".join(unmet),
                      level=logging.WARNING)
            continue

        text, text_unmet = source_text(detail, types=PRESS_TYPES)
        if not text.strip():
            reasons = text_unmet or ["no press-release source text"]
            for reason in reasons:
                report.record(slug, "bigo", "unmet", reason)
            log_event(logger, paths["events"], run_id=run_id, stage="bigo", slug=slug,
                      step="source", status="unmet", detail="; ".join(reasons),
                      level=logging.WARNING)
            continue

        log_event(logger, paths["events"], run_id=run_id, stage="bigo", slug=slug,
                  step="source", status="ok", detail=f"{len(text)} chars")

        try:
            bigo, shown = _extract_bigo(text, detail, invoke_text, usage)
        except Exception as exc:
            report.record(slug, "bigo", "error", f"LLM extraction failed: {exc}")
            log_event(logger, paths["events"], run_id=run_id, stage="bigo", slug=slug,
                      step="extract", status="error", detail=str(exc),
                      level=logging.ERROR)
            if args.verbose:
                import traceback

                traceback.print_exc()
            continue

        if bigo is None:
            report.record(slug, "bigo", "skipped", "LLM could not extract a reliable BIGO")
            log_event(logger, paths["events"], run_id=run_id, stage="bigo", slug=slug,
                      step="extract", status="skipped",
                      detail="LLM could not extract a reliable BIGO",
                      level=logging.WARNING)
            continue

        # Grounding gate. A number the source never states is wrong no matter how
        # confident the model was or how clean its quote looked -- and for a public
        # corruption figure, missing beats wrong. Skip rather than write.
        #
        # `shown` is everything the model was SHOWN, straight from the call that
        # built the prompt -- not just the markdown body. `_extract_bigo` also
        # sends `_source_metadata(...)`, and per its docstring the material
        # `display_name` is frequently where the बिगो is first stated
        # ("... उपर बिगो रु.९०,३९,६२०।३९ कायम"). Checking the body alone rejected
        # figures the model had read correctly out of the title.
        if not amount_is_grounded(shown, bigo):
            reason = f"bigo={bigo} is not stated anywhere in the source text"
            report.record(slug, "bigo", "skipped", reason)
            log_event(logger, paths["events"], run_id=run_id, stage="bigo", slug=slug,
                      step="grounding", status="skipped", detail=reason,
                      level=logging.WARNING)
            continue

        log_event(logger, paths["events"], run_id=run_id, stage="bigo", slug=slug,
                  step="extract", status="ok", detail=str(bigo))

        if args.dry_run:
            report.record(slug, "bigo", "would-enrich", f"bigo={bigo}")
            log_event(logger, paths["events"], run_id=run_id, stage="bigo", slug=slug,
                      step="write", status="would-enrich", detail=f"bigo={bigo}")
            continue

        try:
            api.patch_field(slug, "bigo", bigo)
            report.record(slug, "bigo", "enriched", f"bigo={bigo}")
            log_event(logger, paths["events"], run_id=run_id, stage="bigo", slug=slug,
                      step="write", status="enriched", detail=f"bigo={bigo}")
        except Exception as exc:
            report.record(slug, "bigo", "error", f"PATCH failed: {exc}")
            log_event(logger, paths["events"], run_id=run_id, stage="bigo", slug=slug,
                      step="write", status="error", detail=str(exc),
                      level=logging.ERROR)

    stats = report.summary()
    print_summary(stats, args.dry_run, "BIGO extraction")
    unmet_reasons = report.unmet_reasons()
    if unmet_reasons:
        print("  unmet reasons:")
        for reason, count in unmet_reasons.most_common():
            print(f"    {count} x {reason}")

    usage_summary = ""
    if usage.calls > 0:
        usage_summary = render_usage_table(usage.as_dict()["by_provider"], title="bigo usage")
        print()
        print(usage_summary)

    duration_s = time.monotonic() - start_time
    # Terminal run row in the events stream. Its absence is how you tell a killed
    # or crashed run from one that finished -- the .log footer alone cannot, since
    # the ledger never reads the .log.
    _run_event(
        logger, paths, run_id, "run", "complete",
        f"{format_counts(stats)} duration_s={duration_s:.1f} llm_calls={usage.calls}",
    )
    log_run_footer(
        logger, stage="bigo", stats=stats,
        duration_s=duration_s, usage_summary=usage_summary,
    )

    return report


if __name__ == "__main__":
    main()
