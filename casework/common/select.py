"""Case selection against the current control-plane payload shape."""

COURTCASE_SEGMENT = "/courtcase/"
SPECIAL_COURT = "special"

# Bulk enrichment is DRAFT-only. `ENRICHABLE_STATES` used to be
# `("DRAFT", "IN_REVIEW")`, which meant every bulk run (any run not scoped by
# an explicit --slug/--court-case) also swept in cases a human moderator
# already had open in the review queue. Rewriting a case out from under its
# reviewer is a scope violation for this project, so IN_REVIEW is gone from
# the default -- and, below, is refused outright for bulk selection.
DEFAULT_ENRICHABLE_STATE = "DRAFT"
ENRICHABLE_STATES = (DEFAULT_ENRICHABLE_STATE,)

# States bulk selection may never gate on, whatever the caller asks for.
# A default is not a guarantee: with only the narrowed `ENRICHABLE_STATES`
# above, `--state IN_REVIEW` (or a caller passing `states=("IN_REVIEW",)`)
# walks straight back into the violation the DRAFT default exists to prevent.
# The invariant is "bulk never touches IN_REVIEW", so it is enforced here --
# in the single function every enricher's bulk selection goes through -- and
# fails loud (ValueError) rather than quietly enriching review-queue cases.
FORBIDDEN_BULK_STATES = frozenset({"IN_REVIEW"})


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


def is_enrichable_state(case, states=ENRICHABLE_STATES):
    return case.get("state") in states


def select_cases(cases, *, fiscal_year=None, slugs=(), court_cases=(),
                 states=ENRICHABLE_STATES):
    """Explicit slugs/court-cases bypass the state gate; bulk selection does not.

    That bypass is deliberate and is kept: naming `--slug X` (or
    `--court-case 081-CR-0098`) is one operator asking for one case they have
    already looked at, and the run is still dry by default (`--apply` is the
    only way to write). It is also the only way to re-run a single case that
    has moved past DRAFT, which is how these enrichers are actually debugged.
    The hazard this module guards is the *bulk* sweep, where nobody has looked
    at the individual cases -- so the state gate, and the IN_REVIEW refusal
    below, apply to the bulk branch only.

    `states` is what the caller's `--state` resolved to (default DRAFT); it is
    a parameter rather than a hard-coded constant so the choice is visible at
    the call site instead of buried in this module.
    """
    slugs, court_cases = set(slugs), {c.lower() for c in court_cases}
    if slugs or court_cases:
        return [
            c for c in cases
            if c.get("slug") in slugs or court_number(c) in court_cases
        ]
    forbidden = sorted(FORBIDDEN_BULK_STATES.intersection(states))
    if forbidden:
        raise ValueError(
            f"bulk case selection may not target state(s) {', '.join(forbidden)}: "
            "cases in the review queue are out of scope for enrichment. Select "
            "them one at a time with --slug/--court-case if that is really what "
            "you mean."
        )
    return [
        c for c in cases
        if is_enrichable_state(c, states) and matches_fiscal_year(c, fiscal_year)
    ]
