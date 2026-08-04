"""Case selection against the current control-plane payload shape."""

import csv
import os

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


def slugs_from_batch_csv(path):
    """Ordered, de-duplicated slugs from a batch CSV's ``slug`` column.

    Same format the binder consumes (``bind_materials.py --batch-csv``), so a
    CSV produced by ``select_batch.py`` feeds the enrichers unchanged; extra
    columns (material IRIs, bigo, tiers) are ignored.

    File order is preserved because it is load-bearing: ``--limit N`` must
    mean "the first N rows I listed", not "N arbitrary rows".

    Every failure here is a hard ``SystemExit`` rather than an empty list. An
    empty list would reach ``select_cases`` as an empty allowlist, which falls
    through to BULK selection -- so a truncated, mis-columned or misspelled
    batch file would silently enrich every enrichable case instead of nothing.
    Read with ``utf-8-sig`` so an Excel-exported BOM cannot turn the ``slug``
    header into ``\\ufeffslug`` and trip the missing-column path.
    """
    if not os.path.isfile(path):
        raise SystemExit(f"--batch-csv not found: {path}")
    seen, out = set(), []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames or "slug" not in reader.fieldnames:
            raise SystemExit(
                f"--batch-csv {path} has no `slug` column (found: "
                f"{', '.join(reader.fieldnames or []) or 'nothing'})")
        for row in reader:
            slug = (row.get("slug") or "").strip()
            if not slug or slug in seen:
                continue
            seen.add(slug)
            out.append(slug)
    if not out:
        raise SystemExit(f"--batch-csv {path} lists no slugs")
    return out


def select_cases(cases, *, fiscal_year=None, slugs=(), court_cases=(), batch_slugs=None):
    """Explicit slugs/court-cases bypass the state gate; bulk selection does not.

    ``batch_slugs`` (from ``--batch-csv``) is a HARD allowlist: nothing outside
    it is ever selected, and results come back in batch order so ``--limit``
    takes the operator's first rows. Any other selector can only narrow that
    set further, never widen it.

    Unlike the ``slugs=``/``court_cases=`` bypass, a batch still passes the
    state gate. The asymmetry is deliberate: a stale batch CSV listing a case
    that has since been PUBLISHED must not be re-enriched, and the two error
    directions are not equally costly -- over-selection is invisible in the
    console and writes to reviewed cases, while under-selection shows up as an
    ``n_selected`` lower than the batch's row count. Use ``--slug`` for a
    deliberate one-off override of the gate.
    """
    slugs, court_cases = set(slugs), {c.lower() for c in court_cases}
    if batch_slugs is not None:
        by_slug = {c.get("slug"): c for c in cases}
        picked = []
        for slug in batch_slugs:
            case = by_slug.get(slug)
            if case is None or not is_enrichable_state(case):
                continue
            if (slugs or court_cases) and not (
                slug in slugs or court_number(case) in court_cases
            ):
                continue
            if not matches_fiscal_year(case, fiscal_year):
                continue
            picked.append(case)
        return picked
    if slugs or court_cases:
        return [
            c for c in cases
            if c.get("slug") in slugs or court_number(c) in court_cases
        ]
    return [
        c for c in cases
        if is_enrichable_state(c) and matches_fiscal_year(c, fiscal_year)
    ]


def select_for_run(cases, args):
    """The one selection path every enricher uses: batch + filters + ``--limit``.

    Replaces the five copies of "call ``select_cases``, then slice by
    ``args.limit``" that had drifted across the enrichers. Centralising it is
    what makes ``--limit`` respect batch order everywhere instead of only
    wherever it was wired by hand.
    """
    batch = getattr(args, "batch_csv", None)
    if batch is not None and not str(batch).strip():
        # The flag is present but empty. `--batch-csv "$BATCH"` with an unset
        # variable gets here. Treating it as "no batch" would silently widen the
        # run to the whole corpus, so refuse instead. `_batch_csv_arg` already
        # blocks this at parse time; this covers programmatic callers that build
        # an args namespace by hand.
        raise SystemExit(
            "--batch-csv was given an empty path -- refusing to fall back to a "
            "full-corpus run. Check the variable you passed is set.")
    batch_slugs = slugs_from_batch_csv(batch) if batch else None
    selected = select_cases(
        cases,
        fiscal_year=args.fiscal_year,
        slugs=args.slug,
        court_cases=args.court_case,
        batch_slugs=batch_slugs,
    )
    limit = getattr(args, "limit", 0)
    return selected[:limit] if limit else selected
