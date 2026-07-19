"""Case selection against the current control-plane payload shape."""

COURTCASE_SEGMENT = "/courtcase/"
SPECIAL_COURT = "special"
ENRICHABLE_STATES = ("DRAFT", "IN_REVIEW")


def _refs(case):
    return [str(r) for r in (case.get("court_cases") or [])]


def _parts(ref):
    """('special', '081-cr-0098') from a courtcase IRI, else ('', '').

    The segment search is itself case-insensitive: an all-caps IRI (as a
    caller might pass) must still be recognised, otherwise this silently
    degrades to the same "nothing matched" landmine it exists to guard
    against.
    """
    lowered = ref.lower()
    if COURTCASE_SEGMENT not in lowered:
        return "", ""
    idx = lowered.index(COURTCASE_SEGMENT) + len(COURTCASE_SEGMENT)
    tail = ref[idx:].strip("/")
    bits = tail.split("/")
    if len(bits) < 2:
        return "", ""
    return bits[0].lower(), bits[1].lower()


def is_ciaa_special_court_case(case):
    return any(_parts(r)[0] == SPECIAL_COURT for r in _refs(case))


def court_number(case):
    for ref in _refs(case):
        court, number = _parts(ref)
        if court == SPECIAL_COURT:
            return number
    for ref in _refs(case):
        _, number = _parts(ref)
        if number:
            return number
    return ""


def matches_fiscal_year(case, fiscal_year):
    """Case-insensitive: the canonical IRI lowercases the case number.

    Both sides are normalised by stripping leading zeros (donor
    `casework/common.py:420`, ``fy = fiscal_year.lstrip("0") or "0"``): the
    canonical IRI carries a zero-padded case number (``081-cr-0098``), so an
    un-normalised comparison against ``--fiscal-year 81`` selects ZERO cases
    -- a silent, total selection failure that prints "No matching CIAA
    case(s)" and looks like a clean run. The ``or "0"`` fallback exists for
    an all-zero fiscal year (``"0"``/``"00"``), where a naive ``lstrip("0")``
    would otherwise collapse to ``""``.
    """
    if not fiscal_year:
        return True
    fy = fiscal_year.lower().lstrip("0") or "0"
    for r in _refs(case):
        _, number = _parts(r)
        if "-cr-" not in number:
            continue
        prefix = number.split("-cr-")[0].lstrip("0") or "0"
        if prefix == fy:
            return True
    return False


def is_enrichable_state(case):
    return case.get("state") in ENRICHABLE_STATES


def select_cases(cases, *, fiscal_year=None, slugs=(), court_cases=()):
    """Explicit slugs/court-cases bypass the state gate; bulk selection does not."""
    slugs, court_cases = set(slugs), {c.lower() for c in court_cases}
    if slugs or court_cases:
        return [
            c for c in cases
            if c.get("slug") in slugs or court_number(c) in court_cases
        ]
    return [
        c for c in cases
        if is_enrichable_state(c) and matches_fiscal_year(c, fiscal_year)
    ]
