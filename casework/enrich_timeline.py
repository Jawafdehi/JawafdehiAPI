#!/usr/bin/env python
"""Extract CIAA Special Court case timelines via LLM (DB-free). LOCAL WRITES ONLY.

Ported from the deleted `casework/enrich_timeline.py` (recovered at donor
commit `0321a85`). Reads a case's press-release AND court-order source text
entirely over the Jawafdehi HTTP API, asks the premium LLM tier (with a
`convert_date` tool loop) to reconstruct the case's factual timeline, and
writes exactly ONE field: `api.patch_field(case_slug, "timeline", entries)`
(donor `enrich_timeline.py:358`). Never touches the database directly.

CONCERNS / BRIEF-VS-DONOR DIFFERENCES (flagged for the dispatcher, not
silently "fixed" -- see task-14c-report.md for the full writeup):

1. NO BS-DATE REGEX IN THE DONOR. The task-14 brief's Step 2 test asked for a
   `validate_timeline_items(items) -> (ok, bad_items)` helper that rejects a
   `date_bs` failing `^\\d{4}-\\d{2}-\\d{2}$`. That function and that
   client-side regex check do NOT exist anywhere in the donor's history
   (`git log --all -p -- casework/enrich_timeline.py` never mentions
   `validate_timeline_items`). The donor's real `_clean_entry` (ported here
   unchanged) validates ONLY the AD `date`/`end_date` via `is_valid_iso_date`;
   `date_bs`/`end_date_bs` are accepted as opaque strings straight from the
   LLM with no shape check. The regex `^\\d{4}-\\d{2}-\\d{2}$` DOES exist in
   this codebase, but server-side, in
   `cases/caseworker_serializers.py::TimelineItemSerializer._BS_DATE_RE` --
   the case PATCH endpoint's own validation, which predates this task and is
   out of scope. This port does not invent a duplicate client-side check the
   donor never had.

2. NGM ENDPOINT MIGRATED. The donor's `_get_ngm_data` called
   `GET /ngm/court_case/{special:NNN-CR-NNNN}` -- a single flat payload
   (registration/verdict dates, case_status, embedded hearings) from the
   pre-collapse FastAPI NGM service. That route is GONE (see `config/urls.py`:
   "HARD CUT (2026-07-01): the former `/api/nes/` and `/api/ngm/` prefixes are
   removed"). The current read plane is `courts.urls` at
   `/api/courtcases/{court}/{case_number}/` (composite key) plus a separate
   `/hearings/` sub-resource. `_get_ngm_data` here is rewired to call both.
   Two further real differences from the donor payload, not overcome-able by
   rewiring alone:
     - `CourtCaseSerializer` (courts/serializers.py) has NO `verdict_date_ad`
       or `verdict_judge` field at all -- the current schema does not model a
       verdict date/judge on the court case row. Those keys are simply never
       populated in `ngm_data` here; `_format_ngm_section` (ported verbatim)
       already treats them as optional via `.get()`, so this degrades
       silently and safely (the section is omitted) rather than crashing or
       fabricating a verdict date.
     - Hearings are fetched as a single page (`PlatformCursorPagination`,
       `page_size=50`; see `jawafdehi_shared/drf/base.py`), not the donor's
       complete list. 50 covers ordinary case hearing counts; a case with a
       longer procedural history could have earlier hearings omitted from the
       NGM context section. This is a best-effort context aid for anchoring
       LLM-proposed dates, not the entries source of truth (that is
       source_text below), so a truncated hearing list degrades the anchor's
       completeness, not its correctness.

3. FOUR-WAY SOURCE PRIORITY COLLAPSED TO TWO. The donor's
   `MILESTONE_SOURCE_TYPES` ordered FOUR old `DocumentSource.source_type`
   strings (`AG_ABHIYOG_PATRA` charge sheet > `COURT_ORDER` >
   `CIAA_PRESS_RELEASE` > `COURT_FILING_OTHER`), and only `COURT_ORDER` text
   got the summarize-if-long treatment. The current material-type vocabulary
   (`casework.common.pipeline.PRESS_TYPES` / `COURT_TYPES`) only distinguishes
   two buckets: `COURT_TYPES` (`court_order`) and `PRESS_TYPES`
   (`press_release`, `ciaa_press_release`, `charge_sheet` -- charge_sheet is
   bundled in with press release, not a separate top-priority bucket). This
   port preserves the donor's core invariant -- court-order text is
   prioritised ahead of press text and summarised (not head-truncated) when
   it would not fit the budget whole, so the फैसला/ठहर at the end of a long
   judgment is not silently dropped -- but a very long `charge_sheet` material
   would NOT get that same summarisation treatment (it falls in the
   PRESS_TYPES bucket, which is fed whole/clamped, matching how
   `enrich_missing_bigo.py`/`enrich_allegations.py` already treat press
   content). `casework.common.pipeline.STAGES["timeline"].requires_materials
   == PRESS_TYPES + COURT_TYPES` was verified against the donor and is
   unchanged here.

4. STAGE-LEVEL MATERIAL GATE IS NEW. The donor had no prerequisite DAG at all
   and could, in principle, emit a timeline from NGM hearing data alone even
   with zero bound documents (`_process_case`: `if not source_text and not
   ngm_data: skip`). `unmet_prerequisites(STAGE, detail)` (Task 11
   infrastructure, unchanged here) now requires at least one converted
   PRESS_TYPES/COURT_TYPES material before this stage runs at all -- a case
   with NGM hearings but zero bound documents is now gated out before the
   NGM-only path in `_process_case`'s donor logic would ever have been
   reached. This is inherited Task 11 design (see `pipeline.py`'s own
   docstring), not a change made in this file; the OR-with-`ngm_data` check
   below is kept anyway as a second, harmless safety net.

Usage:
    python casework/enrich_timeline.py --dry-run
    python casework/enrich_timeline.py --slug case-0123
    python casework/enrich_timeline.py --limit 10 --verbose
    python casework/enrich_timeline.py --apply
"""

import argparse
import logging
import os
import sys
import urllib.parse
from typing import Optional

from casework.common.api import CaseworkApi
from casework.common.cli import add_common_args, print_summary, setup_logging
from casework.common.llm import bootstrap, tier_for
from casework.common.materials import source_text
from casework.common.parse import is_valid_iso_date, parse_extraction_response
from casework.common.pipeline import (
    COURT_TYPES,
    PRESS_TYPES,
    STAGES,
    RunReport,
    unmet_prerequisites,
)
from casework.common.select import COURTCASE_SEGMENT, SPECIAL_COURT, select_cases

log = logging.getLogger("casework.enrich_timeline")

STAGE = STAGES["timeline"]

# The donor read these from `CASEWORK_TIMELINE_SOURCE_CHARS` /
# `CASEWORK_TIMELINE_MAX_TOKENS` via an `env_int()` helper that lived in the
# deleted `casework/common.py` and was never re-created in the Task 5-11
# common package (no env-configurable knob exists anywhere else in
# `casework.common` -- see `enrich_missing_bigo.py`'s identical note). Fixed
# at the donor's own default values (`enrich_timeline.py:67-68`).
TIMELINE_SOURCE_CHARS = 60000
TIMELINE_MAX_TOKENS = 8000

# Verdict-summarisation knobs + prompt, ported from the deleted
# `casework/common.py` (donor commit 0321a85, NOT from enrich_timeline.py
# itself -- `summarize_verdict` was shared by `enrich_description.py` and
# `enrich_timeline.py`; only the latter is in this task's scope). Same
# env_int-not-ported fixed-constant treatment as above.
VERDICT_SUMMARY_TRIGGER = 12000
VERDICT_SUMMARY_TARGET = 8000
VERDICT_SUMMARY_MAX_TOKENS = 8000
VERDICT_SUMMARY_CHUNK_CHARS = 150000

EXTRACTION_SYSTEM_PROMPT = """\
You are a Nepali legal analyst reconstructing the FACTUAL TIMELINE of a \
corruption case investigated by Nepal's CIAA (अख्तियार दुरुपयोग अनुसन्धान आयोग) \
and tried at the Special Court (विशेष अदालत).

Your goal is to capture the factual milestones of the case — what happened and \
when — NOT the routine court-procedure log. The court's hearing-by-hearing \
progression (पेशी/sunwai) is already tracked separately as the case's Pragati \
Bibaran by case number, so DO NOT emit one entry per hearing. Use the hearing \
records only to anchor the dates of the milestones below.

MILESTONES TO EXTRACT (include each only when grounded in the sources):
1. Initial factual events (THE PRIORITY) — the dated acts that make up the alleged
   scheme, BEFORE the complaint. Emit EACH distinct dated event as its OWN entry —
   e.g. for procurement: bid submission, technical-committee formation, bid call,
   evaluation / parameter-tailoring, award, contract, each payment; for
   fraud/property: each fraudulent registration or alteration, acquisition,
   transfer, or payment. This pre-complaint chain is exactly what reviewers find
   missing, so extract it in detail from the charge sheet / court order. Emit a
   SINGLE span entry (with "date" + "end_date") ONLY when the sources give no
   granular dates — e.g. just a जाँच अवधि or a period the accused held office.
2. Complaint (उजुरी निवेदन) — when the complaint was registered at the CIAA.
3. CIAA investigation (अनुसन्धान) — when the CIAA began/decided to investigate,
   if distinct from the complaint.
4. Press release (प्रेस विज्ञप्ति) — when the CIAA publicly announced the case.
5. Chargesheet filed / case registered (अभियोगपत्र/आरोपपत्र दायर, मुद्दा दर्ता)
   — when the CIAA filed the chargesheet at the Special Court.
6. Interim court order (अन्तरिम आदेश) — any interim order dates, if issued.
7. Special Court verdict (विशेष अदालतको फैसला) — judgment date and outcome
   (conviction / acquittal "सफाई" / partial).
8. Supreme Court appeal (सर्वोच्च अदालतमा पुनरावेदन) — when an appeal was filed.
9. Supreme Court verdict (सर्वोच्च अदालतको फैसला) — final judgment.

ENTRY FORMAT — each entry is a JSON object:
- "date": AD date "YYYY-MM-DD" (Gregorian). REQUIRED.
- "date_bs": the Bikram Sambat date "YYYY-MM-DD" as it appears in the source.
  REQUIRED — every Nepali legal document states dates in BS; record it.
- "end_date": AD "YYYY-MM-DD" — ONLY for the incident-period entry (milestone 1).
- "end_date_bs": the BS date for end_date — only when end_date is present.
- "title": short Nepali label (देवनागरी, 4-12 words) naming the milestone.
- "description": 1-3 Nepali sentences with specifics (amounts, section numbers,
  press-release number, bench, outcome). Optional but strongly encouraged.

DATE CONVERSION TOOL (MANDATORY):
You have a `convert_date` tool that converts between AD (Gregorian) and BS
(Bikram Sambat) using Nepal's official calendar. LLMs routinely get BS<->AD
conversion wrong by days or months — so you MUST NOT convert dates in your head.
- For every date taken from a source document (stated in BS), call convert_date
  with mode="bs_to_ad" to get the AD "date"; keep the original BS as "date_bs".
- For every date taken from the NGM hearing records (in AD), call convert_date
  with mode="ad_to_bs" to get "date_bs"; keep the AD as "date".
- Batch dates into one tool call where possible (the tool accepts a list).
- Use ONLY the tool's output for "date"/"date_bs"; never adjust or round it.
- Verify every entry's "date" and "date_bs" are a matching pair the tool
  returned before emitting it.

QUALITY RULES:
- Order entries chronologically, earliest first.
- Every entry must be grounded in the provided sources. Do NOT fabricate dates,
  events, amounts, or outcomes.
- Omit a milestone entirely if the sources do not support it. Fewer, accurate
  entries are better than padded ones.
- RESTRAINT — do NOT over-granularise. Suppress routine repetition: no per-hearing
  पेशी, no per-day evaluation/committee meeting, no per-tranche payment row, no
  per-order धरौटी/थुनछेक entry. MERGE such repetitive series into ONE span entry
  (e.g. a single "थुनछेक/धरौटी आदेशहरू" span). Keep milestone 1 to the events that
  move the scheme forward, not every clerical step.
- If the Special Court ACQUITTED (सफाई), the pre-complaint factual events are
  ALLEGATIONS, not established facts — phrase their titles/descriptions as alleged
  (e.g. end with "...गरेको आरोप"), and attribute acts only to the accused the charge
  sheet names (not co-defendants from a linked मुद्दा).
- Trust "date_bs" EXACTLY as written in the source; convert it with convert_date. If
  an AD gloss in the source disagrees with the BS, keep the BS and use the tool's AD.
- The Special Court verdict entry (milestone 7) MUST state the outcome
  (दोषी / सफाई / आंशिक), per defendant or overall — never omit it.
- Do NOT emit routine hearing/पेशी entries — synthesize them into milestone 7.
"""

EXTRACTION_USER_PROMPT = """\
Reconstruct the factual timeline for the following CIAA Special Court case.

Case title: {case_title}

Instructions:
- Extract the factual milestones defined in the system prompt that the sources
  support.
- Every entry needs "date" (AD YYYY-MM-DD), "date_bs" (BS YYYY-MM-DD), and a
  Nepali "title".
- Break milestone 1 into MULTIPLE entries — one per distinct dated initial-fact
  event the sources give (see the system prompt). Use a single "date" + "end_date"
  span (with "date_bs" + "end_date_bs") ONLY when no granular dates exist.
- Convert every date with the convert_date tool — do not convert dates yourself.
  Source dates are BS (use bs_to_ad); NGM dates are AD (use ad_to_bs).
- Use the NGM hearing data only to anchor milestone dates (chargesheet
  registration, verdict). Do NOT create one entry per hearing.
- Order entries chronologically; only include milestones the sources support.

Return ONLY a valid JSON array of entry objects. No markdown, no prose.
Format:
[{{"date": "YYYY-MM-DD", "date_bs": "YYYY-MM-DD", "title": "नेपाली शीर्षक", "description": "विवरण"}}]

{ngm_section}

DOCUMENT TEXT (chargesheet, press release, court order — use for milestones,
narrative, amounts, and any dates not in the NGM data):

{source_text}
"""

VERDICT_SUMMARY_SYSTEM_PROMPT = f"""\
You are a Nepali legal analyst. You are given the full text of a Special Court \
(विशेष अदालत) judgment (फैसला) in a CIAA corruption case. Produce a faithful \
Nepali summary (देवनागरी, government/court register; keep English technical terms \
as-is) that a downstream writer will use to draft the "विशेष अदालतको फैसलाको सार" \
section of a public case record.

Capture ONLY what the judgment states — never infer or invent:
- फैसला मिति (judgment date) and the इजलास / न्यायाधीशहरू (the bench, by name).
- नि.नं. / मुद्दा नं. and the parties (वादी / प्रतिवादीहरू).
- For EACH defendant: the outcome — दोषी (convicted, with कैद/जरिवाना/बिगो असुल) or
  सफाई (acquitted) — and the court's key reasoning for it.
- Any legal principle the court applied or relied on, noting whether it cites a
  Supreme Court precedent (नजिर) — a Special Court ruling does not itself set one.
- The disputed बिगो the court accepted or rejected, and why.
- Every concrete DATE the judgment cites for a factual event (the alleged conduct,
  bids, committee decisions, payments, registrations, complaint, chargesheet) —
  keep the BS date as written; a downstream timeline extractor relies on these.

Be specific (names, दफा, amounts, dates) but concise — aim for about \
{VERDICT_SUMMARY_TARGET} characters. Output plain Nepali prose/short lists, NOT JSON.
"""

_DEVANAGARI_TO_ASCII_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")


def convert_date(dates: list, mode: str) -> dict:
    """Convert YYYY-MM-DD dates between AD and BS via the `nepali` package.

    Returns {input: converted-or-"Error: ..."}. Runs in-process (no network);
    the same calendar math the jawafdehi-mcp convert_date tool uses. Ported
    verbatim from the deleted `casework/common.py` (donor commit 0321a85) --
    the donor's own tool, not a call to the jawafdehi-mcp server.
    """
    import datetime as _dt

    from nepali.datetime import nepalidate

    if mode not in ("ad_to_bs", "bs_to_ad"):
        raise ValueError("mode must be 'ad_to_bs' or 'bs_to_ad'")
    if not isinstance(dates, list):
        raise ValueError("dates must be a list of YYYY-MM-DD strings")

    results: dict = {}
    for raw in dates:
        if not isinstance(raw, str):
            results[str(raw)] = "Error: date must be a YYYY-MM-DD string"
            continue
        normalized = (
            raw.strip().translate(_DEVANAGARI_TO_ASCII_DIGITS).replace("/", "-")
        )
        parts = normalized.split("-")
        if len(parts) != 3:
            results[raw] = "Error: date must be in YYYY-MM-DD format"
            continue
        try:
            year, month, day = int(parts[0]), int(parts[1]), int(parts[2])
            if mode == "ad_to_bs":
                converted = nepalidate.from_date(_dt.date(year, month, day)).strftime(
                    "%Y-%m-%d"
                )
            else:
                converted = (
                    nepalidate(year, month, day)
                    .to_datetime()
                    .date()
                    .strftime("%Y-%m-%d")
                )
            results[raw] = converted
        except Exception as exc:  # noqa: BLE001
            results[raw] = f"Error: {exc}"
    return results


def convert_date_tool():
    """An llm.tools.Tool wrapping convert_date for invoke_with_tools."""
    from llm.tools import Tool

    return Tool(
        name="convert_date",
        description=(
            "Convert dates between AD (Gregorian) and BS (Bikram Sambat) using "
            "Nepal's official calendar (Asia/Kathmandu). LLMs frequently get "
            "BS<->AD conversion wrong; always use this tool instead of converting "
            "in your head."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "dates": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Dates in YYYY-MM-DD format.",
                },
                "mode": {
                    "type": "string",
                    "enum": ["ad_to_bs", "bs_to_ad"],
                    "description": "Direction of conversion.",
                },
            },
            "required": ["dates", "mode"],
        },
        run=convert_date,
        run_path="casework.enrich_timeline:convert_date",
    )


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


def summarize_verdict(verdict_text: str, invoke_text, usage):
    """LLM summary of a long Special Court verdict. Ported from the deleted
    `casework/common.py` (donor commit 0321a85), where it was shared by
    `enrich_description.py` and `enrich_timeline.py`.

    Long judgments are summarised in MULTIPLE passes (one per chunk) and the
    per-chunk summaries concatenated, so the WHOLE document is covered — a single
    head-truncated pass drops the फैसला/ठहर, which sits at the end. Returns the
    summary string, or None on total failure.
    """
    if not verdict_text or not invoke_text:
        return None
    chunk = max(20000, VERDICT_SUMMARY_CHUNK_CHARS)
    chunks = [verdict_text[i : i + chunk] for i in range(0, len(verdict_text), chunk)]
    n = len(chunks)
    summaries: list = []
    for idx, part in enumerate(chunks):
        framing = (
            "Summarise this Special Court judgment as instructed.\n\n"
            if n == 1
            else f"This is part {idx + 1} of {n} of a long Special Court judgment "
            "(split only by length, mid-sentence boundaries possible). Summarise the "
            "substantive content of THIS part as instructed; the फैसला/ठहर may appear "
            "in a later part.\n\n"
        )
        try:
            result = invoke_text(
                system=VERDICT_SUMMARY_SYSTEM_PROMPT,
                content=framing + part,
                tier="premium",
                usage=usage,
                max_tokens=VERDICT_SUMMARY_MAX_TOKENS,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("Verdict part %d/%d summarisation failed: %s", idx + 1, n, exc)
            continue
        if result and result.strip():
            summaries.append((idx + 1, result.strip()))
    if not summaries:
        return None
    if n == 1:
        return summaries[0][1]
    log.info("Verdict summarised in %d passes (of %d parts)", len(summaries), n)
    # Label with the ORIGINAL part index so a failed/skipped chunk doesn't
    # renumber the survivors (खण्ड 3/5 must stay 3/5, not become 2/5).
    return "\n\n".join(f"[खण्ड {part_idx}/{n}]\n{s}" for part_idx, s in summaries)


def _assemble_source_text(court_text: str, press_text: str, invoke_text, usage) -> str:
    """Build the source block within TIMELINE_SOURCE_CHARS.

    Adapted from the donor's `_assemble_source_text` (which iterated a
    priority-ordered list of (source_type, text) tuples) to the current
    two-bucket material vocabulary (see module docstring, concern 3): the
    COURT_TYPES ("court_order") text is prioritised first and summarised
    (not head-truncated) whenever it would not fit the budget whole, so a long
    judgment's tail — the ठहर/outcome and the late factual findings — isn't
    truncated away. The PRESS_TYPES text (press release + charge sheet) is
    added next, whole, up to the remaining budget.
    """
    prepared: list = []
    # Summarise the court order whenever it won't fit whole — not only past the
    # verdict trigger — so a judgment in the (budget, trigger) band gets a
    # date-preserving digest instead of a head-clamp that drops its tail/verdict.
    summary_threshold = min(VERDICT_SUMMARY_TRIGGER, TIMELINE_SOURCE_CHARS)
    if court_text:
        if len(court_text) > summary_threshold:
            summary = summarize_verdict(court_text, invoke_text, usage)
            if summary:
                log.info(
                    "Verdict summarised: %d -> %d chars", len(court_text), len(summary)
                )
                prepared.append(("COURT_ORDER (फैसला सारांश)", summary))
            else:
                # Summary failed — fall back to a truncated head.
                prepared.append(("COURT_ORDER", court_text[:VERDICT_SUMMARY_TARGET]))
        else:
            prepared.append(("COURT_ORDER", court_text))
    if press_text:
        prepared.append(("PRESS_RELEASE", press_text))

    parts: list = []
    remaining = TIMELINE_SOURCE_CHARS
    for label, text in prepared:
        if remaining <= 0:
            log.warning("Timeline source budget spent; dropped a %s source", label)
            break
        chunk = _clamp(text, remaining, label)
        parts.append(f"[{label}]\n{chunk}")
        remaining -= len(chunk)
    return "\n\n---\n\n".join(parts)


def _special_case_number(case: dict) -> Optional[str]:
    """Extract the special-court case number from `case["court_cases"]` IRIs.

    Adapted from the donor's `_get_ngm_data`, which scanned for a
    "special:NNN-CR-NNNN"-shaped string (the pre-collapse court-reference
    format). The current `court_cases` entries are full courtcase IRIs
    (".../courtcase/special/081-cr-0098" -- see `casework.common.select`),
    so this scans for the SPECIAL_COURT segment instead of a colon prefix.
    """
    segment = f"{COURTCASE_SEGMENT}{SPECIAL_COURT}/"
    for ref in case.get("court_cases") or []:
        if not isinstance(ref, str):
            continue
        lowered = ref.lower()
        idx = lowered.find(segment)
        if idx == -1:
            continue
        tail = ref[idx + len(segment) :].strip("/")
        number = tail.split("/")[0] if tail else ""
        if number:
            return number.lower()
    return None


def _get_ngm_data(case: dict, api: CaseworkApi) -> Optional[dict]:
    """Fetch NGM court-case + hearing records for the case's special-court ref.

    Rewired for the current `/api/courtcases/{court}/{case_number}/` +
    `.../hearings/` composite-key read plane (see module docstring, concern 2)
    -- the donor's single `/ngm/court_case/{ref}` endpoint no longer exists.
    Best-effort: any failure returns None rather than aborting the case.
    """
    number = _special_case_number(case)
    if not number:
        return None

    quoted = urllib.parse.quote(number)
    try:
        detail = api.get(f"/courtcases/{SPECIAL_COURT}/{quoted}/")
    except Exception as exc:
        log.warning("NGM case query failed for %s: %s", number, exc)
        return None
    if not isinstance(detail, dict):
        return None

    hearings = []
    try:
        hearing_page = api.get(f"/courtcases/{SPECIAL_COURT}/{quoted}/hearings/")
        if isinstance(hearing_page, dict):
            hearings = hearing_page.get("results") or []
    except Exception as exc:
        log.warning("NGM hearings query failed for %s: %s", number, exc)

    return {
        "registration_date_ad": detail.get("registration_date_ad"),
        "case_status": detail.get("case_status"),
        "hearings": hearings,
        # NOTE: no verdict_date_ad / verdict_judge -- CourtCaseSerializer does
        # not model them (see module docstring, concern 2). _format_ngm_section
        # treats both as optional via .get(), so this degrades silently.
    }


def _format_ngm_section(ngm_data: Optional[dict]) -> str:
    """Format the flat NGM API payload as a prompt section. Ported verbatim
    from the donor -- every field is read with `.get()`, so the current
    payload's missing verdict_date_ad/verdict_judge keys simply omit those
    lines rather than raising or fabricating them."""
    if not ngm_data:
        return ""

    lines = [
        "NGM STRUCTURED HEARING DATA (ground-truth AD dates — convert to BS "
        "with the convert_date tool; use only to anchor milestone dates):",
        "",
    ]
    reg_date = ngm_data.get("registration_date_ad")
    verdict_date = ngm_data.get("verdict_date_ad")
    case_status = ngm_data.get("case_status")

    if reg_date:
        lines.append(f"- Case registration: {reg_date}")
    if case_status:
        lines.append(f"- Case status: {case_status}")

    hearings = ngm_data.get("hearings") or []
    if hearings:
        lines.append(f"- Hearings ({len(hearings)} records):")
        for h in hearings:
            h_date = h.get("hearing_date_ad", "")
            h_decision = h.get("decision_type") or ""
            h_remarks = (h.get("remarks") or "")[:200]
            line = f"  * {h_date}"
            if h_decision:
                line += f" — {h_decision}"
            if h_remarks:
                line += f" — {h_remarks}"
            lines.append(line)

    if verdict_date:
        lines.append(f"- Verdict date: {verdict_date}")
        verdict_judge = ngm_data.get("verdict_judge")
        if verdict_judge:
            lines.append(f"  Judge: {verdict_judge}")

    return "\n".join(lines) + "\n"


def _extract_timeline(
    source_text_: str,
    case_title: str,
    invoke_with_tools,
    usage,
    ngm_data: Optional[dict] = None,
) -> Optional[list]:
    """Call the LLM (with the convert_date tool) to extract timeline entries."""
    # source_text_ is already budgeted by _assemble_source_text (TIMELINE_SOURCE_CHARS).
    prompt = EXTRACTION_USER_PROMPT.format(
        case_title=case_title,
        ngm_section=_format_ngm_section(ngm_data),
        source_text=source_text_,
    )

    response_text = invoke_with_tools(
        system=EXTRACTION_SYSTEM_PROMPT,
        content=prompt,
        tools=[convert_date_tool()],
        max_tokens=TIMELINE_MAX_TOKENS,
        tier=tier_for("timeline"),
        usage=usage,
        max_iterations=30,
    )

    return _parse_timeline_response(response_text)


def _parse_timeline_response(response_text: str) -> Optional[list]:
    """Parse the LLM response into clean, validated timeline entries."""
    raw = parse_extraction_response(response_text, wrapper_keys={"timeline", "entries"})
    if raw is None:
        return None

    clean = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        entry = _clean_entry(item)
        if entry is not None:
            clean.append(entry)

    if not clean:
        return None
    clean.sort(key=lambda e: e["date"])
    return clean


def _clean_entry(item: dict) -> Optional[dict]:
    """Validate and normalise a single LLM-produced entry. Ported verbatim
    from the donor -- date_bs/end_date_bs are accepted as opaque strings, with
    NO regex/shape validation (see module docstring, concern 1)."""
    date_val = str(item.get("date") or "").strip()
    title_val = str(
        item.get("title") or item.get("event") or item.get("name") or ""
    ).strip()
    if not date_val or not title_val:
        return None
    if not is_valid_iso_date(date_val):
        log.warning("Dropping entry with non-ISO date: %s", date_val)
        return None

    entry: dict = {"date": date_val, "title": title_val}

    desc_val = str(
        item.get("description") or item.get("desc") or item.get("detail") or ""
    ).strip()
    if desc_val:
        entry["description"] = desc_val

    date_bs = str(item.get("date_bs") or "").strip()
    if date_bs:
        entry["date_bs"] = date_bs

    end_date = str(item.get("end_date") or "").strip()
    if end_date:
        if not is_valid_iso_date(end_date):
            log.warning("Dropping invalid end_date %s; keeping entry", end_date)
        elif end_date < date_val:
            log.warning(
                "Dropping end_date %s before date %s; keeping entry", end_date, date_val
            )
        else:
            entry["end_date"] = end_date
            end_date_bs = str(item.get("end_date_bs") or "").strip()
            if end_date_bs:
                entry["end_date_bs"] = end_date_bs

    return entry


def build_api(args):
    """Construct the client. Basic (local DEV_AUTH) unless a token is given."""
    if args.api_token:
        return CaseworkApi(args.api_base_url, token=args.api_token)
    return CaseworkApi(
        args.api_base_url,
        basic=(os.getenv("CASEWORK_API_USER", "abgen"),
               os.getenv("CASEWORK_API_PASSWORD", "local-dev-only")),
    )


def main(argv=None):
    """Main entry point."""
    ap = argparse.ArgumentParser(
        description="Extract CIAA Special Court case timelines via LLM (DB-free).",
        epilog="Reads/writes cases entirely over the Jawafdehi HTTP API.",
    )
    add_common_args(ap)
    args = ap.parse_args(argv)

    setup_logging(args.verbose)

    # Bootstrap Django + LLM (MUST come before importing llm.invoke)
    try:
        bootstrap(args.provider, args.model)
    except Exception as exc:
        print(f"Bootstrap failed: {exc}", file=sys.stderr)
        sys.exit(1)

    from llm.invoke import invoke_text, invoke_with_tools
    from llm.usage import UsageAccumulator, render_usage_table

    api = build_api(args)
    usage = UsageAccumulator()
    report = RunReport()

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
    if total == 0:
        print("No matching CIAA case(s) to process.", file=sys.stderr)
        print_summary(report.summary(), args.dry_run, "Timeline extraction")
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
        # the detail fetch and passes that same `title` on, never `detail.get("title")`.
        title = case.get("title") or ""
        print(f"\n[{idx}/{total}] {slug} — {title[:80]}")

        if case.get("timeline") and not args.force:
            report.record(
                slug, "timeline", "already", f"timeline already {case['timeline']}")
            print("  timeline already populated — skipping (use --force to re-extract)")
            continue

        # Donor-preserved fallback: a detail-fetch failure does not abort the
        # case -- the donor caught `requests.HTTPError` and fell back to the
        # LIST-shaped `case` dict (`detail = case`). `CaseworkApi` raises
        # urllib errors, not `requests.HTTPError`, so this is widened to
        # `Exception` for the new HTTP client (see enrich_allegations.py's
        # identical rationale).
        try:
            detail = api.get_case(slug)
        except Exception as exc:
            detail = case
            print(f"  (using summary instead of detail: {exc})")

        unmet = unmet_prerequisites(STAGE, detail)
        if unmet:
            for reason in unmet:
                report.record(slug, "timeline", "unmet", reason)
            print(f"  Unmet prerequisite(s): {'; '.join(unmet)}")
            continue

        court_text, court_unmet = source_text(detail, types=COURT_TYPES)
        press_text, press_unmet = source_text(detail, types=PRESS_TYPES)
        ngm_data = _get_ngm_data(detail, api)

        if not court_text.strip() and not press_text.strip() and not ngm_data:
            reasons = (court_unmet + press_unmet) or [
                "no press-release or court-order source text"]
            for reason in reasons:
                report.record(slug, "timeline", "unmet", reason)
            print(f"  No source content found: {'; '.join(reasons)}")
            continue

        combined_source_text = (
            _assemble_source_text(court_text, press_text, invoke_text, usage)
            if (court_text or press_text) else ""
        )
        print(f"  Source content: {len(combined_source_text)} chars assembled")
        if ngm_data:
            hearings = ngm_data.get("hearings") or []
            print(f"  NGM data: {len(hearings)} hearing(s)")
        else:
            print("  NGM data: none")

        try:
            entries = _extract_timeline(
                source_text_=combined_source_text,
                case_title=title,
                invoke_with_tools=invoke_with_tools,
                usage=usage,
                ngm_data=ngm_data,
            )
        except Exception as exc:
            report.record(slug, "timeline", "error", f"LLM extraction failed: {exc}")
            print(f"  LLM extraction failed: {exc}")
            if args.verbose:
                import traceback

                traceback.print_exc()
            continue

        if not entries:
            report.record(slug, "timeline", "skipped", "LLM returned no timeline entries")
            print("  LLM returned no timeline entries — skipping")
            continue

        print(f"  Extracted {len(entries)} entry(s)")
        for i, entry in enumerate(entries, 1):
            span = f" → {entry['end_date']}" if entry.get("end_date") else ""
            print(
                f"    {i}. {entry.get('date', '?')}{span} — {entry.get('title', '?')[:80]}"
            )

        if args.dry_run:
            report.record(slug, "timeline", "would-enrich", f"{len(entries)} entries")
            print("  [DRY RUN] Would PATCH but --dry-run is set")
            continue

        try:
            api.patch_field(slug, "timeline", entries)
            report.record(slug, "timeline", "enriched", f"{len(entries)} entries")
            print(f"  [UPDATED] {slug}")
        except Exception as exc:
            report.record(slug, "timeline", "error", f"PATCH failed: {exc}")
            print(f"  Failed to PATCH timeline: {exc}")

    stats = report.summary()
    print_summary(stats, args.dry_run, "Timeline extraction")
    unmet_reasons = report.unmet_reasons()
    if unmet_reasons:
        print("  unmet reasons:")
        for reason, count in unmet_reasons.most_common():
            print(f"    {count} x {reason}")

    if usage.calls > 0:
        print()
        print(render_usage_table(usage.as_dict()["by_provider"], title="timeline usage"))

    return report


if __name__ == "__main__":
    main()
