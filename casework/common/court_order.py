"""Character-anchored readers that pick which part of a court order an LLM sees.

Four readers, two consumers. The extraction reads `court_order_head` (caption,
party list and bench, which sit in the first 3% of all 38 orders in the sample)
plus `court_order_thahar` (a window forward from the `ठहर खण्ड` marker, the
operative section). A per-defendant verdict reads `court_order_verdict_zone`
(marker to end). `court_order_tail` is the ending, and the fallback both
marker-anchored readers use when an order carries no marker.

Every limit is an absolute character count, never a percentage of the input: a
tail is roughly 12% of a document, and on the 379,484-char order in the sample
that is 45,538 chars, past PROMPT_HARD_MAX on its own.
"""

import logging

log = logging.getLogger("casework.court_order")

HEAD_CHARS: int = 6_000
THAHAR_CHARS: int = 15_500
THAHAR_MARKER = "ठहर खण्ड"

_HEAD_LABEL = "\n\n[...अदालतको आदेशको सुरुको भाग...]\n\n"
# Says only "the ending", because that is all a tail is. It used to claim
# `ठहर खण्ड`, which is exactly what the reader reaching for it did NOT find.
_TAIL_LABEL = "\n\n[...अदालतको आदेशको अन्त्यको भाग...]\n\n"
_THAHAR_LABEL = "\n\n[...अदालतको आदेशको ठहर खण्डबाट अंश...]\n\n"


def court_order_head(text: str, limit: int = HEAD_CHARS, label: bool = True) -> str:
    """Return the first `limit` chars of a court order, labelled as a fragment when truncated."""
    if not text:
        return ""
    if len(text) <= limit:
        return text
    head = text[:limit]
    return _HEAD_LABEL + head if label else head


def court_order_tail(text: str, limit: int, label: bool = True) -> str:
    """Return the last `limit` chars of a court order, labelled as a fragment when truncated.

    `limit` is required: a tail has no size of its own, it borrows the size of
    whichever reader fell back to it.
    """
    if not text:
        return ""
    if len(text) <= limit:
        return text
    tail = text[-limit:]
    return _TAIL_LABEL + tail if label else tail


def court_order_thahar(text: str, limit: int = THAHAR_CHARS, label: bool = True) -> str:
    """Return `limit` chars starting at the first `ठहर खण्ड` marker, falling
    back to `court_order_tail` when the marker is absent.

    The marker is looked for FIRST, before any length short-circuit: only a
    fragment that actually starts at the marker may wear `_THAHAR_LABEL`, and
    a short order used to come back whole under it, marker or no marker.

    15,500 clears PROMPT_HARD_MAX with the two section headers and two
    fragment labels also counted in, while still dominating the old
    marker-anchored slice's 12,000-char window -- the bar the A/B measured.
    """
    if not text:
        return ""
    idx = text.find(THAHAR_MARKER)
    if idx == -1:
        return court_order_tail(text, limit, label)
    window = text[idx : idx + limit]
    return _THAHAR_LABEL + window if label else window


# A marker window and a tail are two fixed-size ranges with a HOLE between
# them -- and on 2 of 7 measured production judgments (2026-08-31), the
# operative holding sat in that hole: likhu-tamakoshi missed by 456 chars
# (holding 8,456 chars from EOF, past the old 8,000-char tail), and
# case-081-cr-0046 missed by 19,427 (deep in the gap, 0 of 9 defendants
# scored). Marker-to-end has no gap by construction, so it replaces the
# two-window union as the primary shape.
#
# The population this runs against is ~546 cases, so a document longer than
# every sample measured so far is not a hypothetical -- the fallback for
# that case must not reintroduce a gap of its own. It does not: past the
# cap, this returns the last VERDICT_ZONE_CHARS chars of the WHOLE document
# as one contiguous slice (via `court_order_tail`), never a marker window
# plus a separate tail. What that drops is the beginning of an unusually
# long ठहर खण्ड -- recital, not the holding, which sits at the end.
#
# VERDICT_ZONE_CHARS is the largest marker-to-end span measured across the 7
# sampled orders (356,775 - 253,520 = 103,255, likhu-tamakoshi); the other 5
# that carry a marker measure 60,832 / 31,415 / 36,749 / 22,291 / 49,807, so
# none of the 7 need the fallback.
VERDICT_ZONE_CHARS: int = 103_255

_VERDICT_LABEL = "\n\n[...अदालतको आदेशको ठहर तथा अन्त्यको भाग...]\n\n"


def court_order_verdict_zone(text: str, limit: int = VERDICT_ZONE_CHARS,
                             label: bool = True) -> str:
    """Return the marker-to-end span of a court order, capped at `limit` -- the
    slice `accused_verdicts` reads to decide a per-defendant disposition.

    The marker is looked for FIRST, before any length short-circuit -- the
    same inversion `court_order_thahar` records as a past bug. Under the cap a
    length-first reader returns the whole document, recital included, and the
    zone is re-sent once per name chunk.

    Marker-to-end has no gap by construction. When that span still exceeds
    the cap, or there is no marker at all, this falls back to
    `court_order_tail` -- the last `limit` chars of the WHOLE document as one
    contiguous slice, never a second, disjoint window: a gap must be
    structurally impossible, not merely unlikely on the documents measured so
    far.
    """
    if not text:
        return ""
    idx = text.find(THAHAR_MARKER)
    if idx != -1 and len(text) - idx <= limit:
        window = text[idx:]
        return _VERDICT_LABEL + window if label else window
    return court_order_tail(text, limit, label)


VERDICT_SUMMARY_TRIGGER = 12000
VERDICT_SUMMARY_TARGET = 8000
VERDICT_SUMMARY_MAX_TOKENS = 8000
VERDICT_SUMMARY_CHUNK_CHARS = 150000

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
