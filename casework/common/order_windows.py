"""Which parts of a Special Court order the entities enricher reads."""

import re
from dataclasses import dataclass
from typing import Iterator

ENTITY_WINDOW_CHARS = 30_000
WINDOW_OVERLAP = 1_500
MAX_ENTITY_WINDOWS = 4
VERDICT_CHUNK_CHARS = 18_000
MAX_VERDICT_BACK_CHUNKS = 4
CAPTION_SEARCH_CHARS = 8_000
CAPTION_FALLBACK_CHARS = 2_000
PARAGRAPH_SLACK = 2_000

_CAPTION_END = re.compile(r"मुद्[दध]ा\s*[:ः]")
_TASARTHA = "तसर्थ"
_HOLDING_VERB = re.compile(r"ठहर्छ|ठहर्दछ|ठहर्‍याइ|ठहर्याइ|ठहराइ")
_ANALYSIS = re.compile(r"मिसिल\s*अध्ययन|निर्णय\s*गर्नु\s*पर्ने")


@dataclass(frozen=True)
class Window:
    start: int
    end: int
    total: int
    text: str

    def label(self) -> str:
        return f"अदालतको आदेश, अक्षर {self.start:,}–{self.end:,} (कुल {self.total:,})"


def caption_end(text: str) -> int:
    """End of the caption (judges, parties, case type): the first `मुद्दा:` in the first 8k chars."""
    m = _CAPTION_END.search(text, 0, CAPTION_SEARCH_CHARS)
    return m.end() if m else min(len(text), CAPTION_FALLBACK_CHARS)


def _cut_back(text: str, target: int, floor: int) -> int:
    if target >= len(text):
        return len(text)
    nl = text.rfind("\n", max(floor, target - PARAGRAPH_SLACK), target)
    return nl + 1 if nl != -1 and nl + 1 > floor + WINDOW_OVERLAP else target


def start_windows(text: str, size: int = ENTITY_WINDOW_CHARS,
                  limit: int = MAX_ENTITY_WINDOWS) -> Iterator[Window]:
    """The caption plus `size` chars, then further `size`-char windows, at most `limit`."""
    n, start, target = len(text), 0, caption_end(text) + size
    for _ in range(limit):
        end = _cut_back(text, target, start)
        yield Window(start, end, n, text[start:end])
        if end >= n:
            return
        start = end - WINDOW_OVERLAP
        target = start + size


def holding_start(text: str) -> tuple[int, str]:
    """Where the court's holding starts: last `तसर्थ`, else last holding verb, else the tail."""
    i = text.rfind(_TASARTHA)
    if i != -1:
        return i, "तसर्थ"
    verbs = [m.start() for m in _HOLDING_VERB.finditer(text)]
    if verbs:
        return verbs[-1], "holding-verb"
    return max(0, len(text) - VERDICT_CHUNK_CHARS), "tail"


def analysis_start(text: str) -> int | None:
    """Where the court's own analysis begins, searched after the caption."""
    m = _ANALYSIS.search(text, caption_end(text))
    return m.start() if m else None


def _backward(text: str, lo: int, hi: int, size: int) -> list[Window]:
    n, out, end = len(text), [], min(hi, len(text))
    while end > lo:
        start = max(lo, end - size)
        out.append(Window(start, end, n, text[start:end]))
        if start == lo:
            break
        end = start + WINDOW_OVERLAP
    return out


def end_windows(text: str, size: int = VERDICT_CHUNK_CHARS,
                max_back: int = MAX_VERDICT_BACK_CHUNKS) -> list[Window]:
    """Holding-to-end chunks, then up to `max_back` chunks before it, latest first."""
    hold, _ = holding_start(text)
    analysis = analysis_start(text)
    floor = analysis if analysis is not None and analysis < hold else 0
    back_floor = max(floor, hold - max_back * (size - WINDOW_OVERLAP))
    out = _backward(text, hold, len(text), size)
    if max_back and back_floor < hold:
        out += _backward(text, back_floor, hold + WINDOW_OVERLAP, size)[:max_back]
    return out
