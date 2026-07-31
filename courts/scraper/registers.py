"""Register-gap discovery — which docket numbers a court's register implies but
the mirror does not hold.

The cause-list crawlers (``crawl_date``) only ever see cases that appear on a
published daily hearing list, so a case registered and disposed *without* one
never enters the mirror at all. The ``ScrapedDate`` frontier cannot detect this:
it records which dates were visited, never which dockets were absent from them.

Court registers are sequential per ``(year, series)``, so the absent sequence
numbers **are** the gap. This module derives them from held rows alone — no
network — and the sweep then probes each candidate against the court's own
case-detail page. Everything here is pure apart from :func:`register_gaps`,
which is a thin DB wrapper around :func:`compute_gaps`.

Number shapes this has to survive (all observed in the live mirror):

- ``076-CR-0294``  the modern shape: 3-digit BS year, alpha series, padded seq
- ``080-02-3073``  a **numeric** series — "the series is the non-numeric token" is wrong
- ``080-C1-10961`` sequences exceed the pad width; a lexical max under-reports badly
- ``93-068-0128``  a legacy scheme where position 2 is the YEAR, not a series
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

NGM_DB = "ngm"

#: How far past the highest held slot to probe for a truncated register tail.
#: Sequence density only finds *interior* holes — if a register's last N numbers
#: were never listed, the register simply looks shorter than it is. The bound has
#: to clear the longest run of genuinely-consecutive missing slots, or the probe
#: stops inside a real run and declares the register finished early. The longest
#: run observed in the special court is 9 (``082-CR-0162..0170``), so 25 leaves
#: real headroom. Raise it, don't lower it.
DEFAULT_TAIL_PROBE = 25

#: ``<3-digit BS year>-<series>-<sequence>``. The series is ``[A-Z0-9]+`` because
#: district registers use numeric series (``080-02-``, ``080-07-``). The year is
#: anchored to exactly 3 digits, which is what excludes the legacy ``93-<year>-``
#: scheme (a 2-digit leading token) without needing a separate deny-list.
_MODERN = re.compile(r"^(\d{3})-([A-Z0-9]+)-(\d+)$")


@dataclass(frozen=True)
class RegisterKey:
    """One register: a ``(year, series)`` pair within a single court."""

    year: str
    series: str


@dataclass(frozen=True)
class ParsedNumber:
    key: RegisterKey
    seq: int
    pad: int  # zero-pad width of the sequence token as written


def parse_case_number(case_number: str) -> ParsedNumber | None:
    """Split a modern ``YYY-SERIES-NNNN`` docket number, else ``None``.

    ``None`` means "not enumerable", not "invalid" — legacy schemes and one-off
    shapes are real cases, they just can't be walked as a sequence.
    """
    m = _MODERN.match((case_number or "").strip())
    if not m:
        return None
    year, series, seq = m.groups()
    return ParsedNumber(key=RegisterKey(year=year, series=series), seq=int(seq), pad=len(seq))


def format_case_number(key: RegisterKey, seq: int, pad: int) -> str:
    """Render a candidate docket number.

    ``f"{seq:0{pad}d}"`` widens rather than truncates, so a sequence that has
    outgrown the register's usual pad width (``080-C1-10961`` against a pad of 4)
    still renders correctly.
    """
    return f"{key.year}-{key.series}-{seq:0{pad}d}"


def compute_gaps(
    case_numbers: Iterable[str], *, tail_probe: int = DEFAULT_TAIL_PROBE
) -> list[str]:
    """The docket numbers a set of held case numbers implies but does not contain.

    Interior holes plus ``tail_probe`` candidates past each register's highest held
    slot, returned in **probe priority order** — which matters because a sweep run
    is budgeted and will only ever reach a prefix of this list. A large court can
    imply tens of thousands of candidates (``kathmandudc`` alone implies ~84,000
    across 147 registers), so the order chosen here decides what actually gets
    fetched, and plain lexical sort would spend every run on the oldest registers.

    Priority is:

    1. **newest register year first** — recent dockets are the ones being cited
    2. **tail before interior** within a year — the tail is the growth edge, the
       only part of a register that is never final
    3. sequence ascending, for determinism

    Unparseable/legacy numbers are ignored rather than raising — a court whose
    whole register predates the modern scheme simply yields no candidates.
    """
    seqs: dict[RegisterKey, set[int]] = defaultdict(set)
    pads: dict[RegisterKey, Counter] = defaultdict(Counter)

    for raw in case_numbers:
        parsed = parse_case_number(raw)
        if parsed is None:
            continue
        seqs[parsed.key].add(parsed.seq)
        pads[parsed.key][parsed.pad] += 1

    ranked: list[tuple[int, int, str, int, str]] = []
    for key, held in seqs.items():
        # The modal width, not the max: one malformed row shouldn't re-pad a
        # whole register. Ties resolve to the wider form (most_common is stable
        # on insertion order, so sort explicitly).
        pad = sorted(pads[key].items(), key=lambda kv: (-kv[1], -kv[0]))[0][0]
        high = max(held)
        tail = range(high + 1, high + 1 + max(tail_probe, 0))
        # Registers start at 1; a register whose first slots were never listed is
        # a gap like any other.
        interior = (s for s in range(1, high) if s not in held)
        for kind, group in ((0, tail), (1, interior)):
            for seq in group:
                ranked.append(
                    (-int(key.year), kind, key.series, seq, format_case_number(key, seq, pad))
                )
    ranked.sort()
    return [number for *_, number in ranked]


def held_case_numbers(court_id: str, *, using: str = NGM_DB) -> list[str]:
    """Every case number the mirror holds for a court."""
    from courts.models import CourtCase

    return list(
        CourtCase.objects.using(using)
        .filter(court_id=court_id)
        .values_list("case_number", flat=True)
    )


def register_gaps(
    court_id: str, *, using: str = NGM_DB, tail_probe: int = DEFAULT_TAIL_PROBE
) -> list[str]:
    """:func:`compute_gaps` over everything the mirror holds for one court."""
    return compute_gaps(held_case_numbers(court_id, using=using), tail_probe=tail_probe)
