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

HEAD_CHARS: int = 6_000
TAIL_CHARS: int = 25_000

_HEAD_LABEL = "\n\n[...अदालतको आदेशको सुरुको भाग...]\n\n"
_TAIL_LABEL = "\n\n[...अदालतको आदेश — ठहर खण्ड...]\n\n"


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
