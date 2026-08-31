"""A character-anchored zone reader for court-order text.

WHY ZONES AND NOT ONE SLICE. The slice this replaces anchored on the literal
`ठहर खण्ड` and took 12,000 chars forward from that marker. Measured over 37
production court orders (2026-08-31), that reached the operative verdict in
only 10 of 37: the verdict actually sits a median 2,852 chars from the END of
the file (p90 8,389, max 38,792), and the marker is a section heading two-
thirds of the way through the document, not immediately before the verdict.

A 25,000-char TAIL reaches the verdict in 36 of 37 measured orders (10,000
reaches only 34). Caption, party list and bench sit in the first 3% of every
order in the sample (38/38); a 6,000-char HEAD clears that on a median
60,842-char order.

Both zones are capped in characters, never a percentage of the input. The
tail is roughly 12% of a document -- on the 379,484-char order in the sample
that would be 45,538 chars, which blows past PROMPT_HARD_MAX outright.
"""

import logging

log = logging.getLogger("casework.court_order")

HEAD_CHARS: int = 6_000
TAIL_CHARS: int = 25_000
THAHAR_CHARS: int = 15_500
THAHAR_MARKER = "ठहर खण्ड"

_HEAD_LABEL = "\n\n[...अदालतको आदेशको सुरुको भाग...]\n\n"
_TAIL_LABEL = "\n\n[...अदालतको आदेश — ठहर खण्ड...]\n\n"
_THAHAR_LABEL = "\n\n[...अदालतको आदेशको ठहर खण्डबाट अंश...]\n\n"


def court_order_head(text: str, limit: int = HEAD_CHARS, label: bool = True) -> str:
    """Return the first `limit` chars of a court order, labelled as a fragment when truncated."""
    if not text:
        return ""
    if len(text) <= limit:
        return text
    head = text[:limit]
    return _HEAD_LABEL + head if label else head


def court_order_tail(text: str, limit: int = TAIL_CHARS, label: bool = True) -> str:
    """Return the last `limit` chars of a court order, labelled as a fragment when truncated."""
    if not text:
        return ""
    if len(text) <= limit:
        return text
    tail = text[-limit:]
    return _TAIL_LABEL + tail if label else tail


def court_order_thahar(text: str, limit: int = THAHAR_CHARS, label: bool = True) -> str:
    """Return `limit` chars starting at the first `ठहर खण्ड` marker, falling
    back to `court_order_tail` when the marker is absent.

    15,500 clears PROMPT_HARD_MAX with the two section headers and two
    fragment labels also counted in, while still dominating the old
    marker-anchored slice's 12,000-char window -- the bar the A/B measured.
    """
    if not text:
        return ""
    if len(text) <= limit:
        return text
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
# two-window union below as the primary shape.
#
# VERDICT_ZONE_CHARS is the largest marker-to-end span measured across those
# 7 orders (356,775 - 253,520 = 103,255, likhu-tamakoshi); the other 5 that
# carry a marker measure 60,832 / 31,415 / 36,749 / 22,291 / 49,807. Set
# there, none of the 7 need the fallback below.
#
# VERDICT_TAIL_CHARS is derived, not independently chosen, so the fallback's
# total budget still traces back to that one evidence-based number: sized as
# VERDICT_ZONE_CHARS - VERDICT_THAHAR_CHARS, it comfortably clears the
# 8,456-char miss that sank likhu-tamakoshi under the old 8,000-char tail,
# and it proves the fallback's marker window and tail can never overlap --
# the fallback only triggers once the whole span exceeds VERDICT_ZONE_CHARS,
# at which point `tail_start > marker_end` always holds, so no
# overlap-collapse case exists here (unlike the two-window design this
# replaces).
VERDICT_ZONE_CHARS: int = 103_255
VERDICT_THAHAR_CHARS: int = 20_000
VERDICT_TAIL_CHARS: int = VERDICT_ZONE_CHARS - VERDICT_THAHAR_CHARS

_VERDICT_LABEL = "\n\n[...अदालतको आदेशको ठहर तथा अन्त्यको भाग...]\n\n"
_VERDICT_GAP_LABEL = "\n\n[...बीचको अंश हटाइएको...]\n\n"


def court_order_verdict_zone(text: str, label: bool = True) -> str:
    """Return the marker-to-end span of a court order, capped at
    `VERDICT_ZONE_CHARS` -- the slice `accused_verdicts` reads to decide a
    per-defendant disposition.

    Falls back to a marker window plus a tail only when marker-to-end still
    exceeds the cap; see the constants above for why that fallback cannot
    reproduce the gap this replaces.
    """
    if not text:
        return ""
    if len(text) <= VERDICT_ZONE_CHARS:
        return text

    idx = text.find(THAHAR_MARKER)
    if idx == -1:
        return court_order_tail(text, VERDICT_ZONE_CHARS, label)

    span = len(text) - idx
    if span <= VERDICT_ZONE_CHARS:
        window = text[idx:]
        return _VERDICT_LABEL + window if label else window

    marker_window = text[idx : idx + VERDICT_THAHAR_CHARS]
    tail_window = text[-VERDICT_TAIL_CHARS:]
    if label:
        return _VERDICT_LABEL + marker_window + _VERDICT_GAP_LABEL + tail_window
    return marker_window + tail_window


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
