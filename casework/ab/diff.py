"""Three-way comparison: Arm A (donor) vs Arm B (port) vs Arm G (June's shipped values).

The comparison is FIELD-APPROPRIATE, not uniform string equality:

  * ``bigo``            integer -- exact match or not.
  * ``tags``            set -- Jaccard / precision / recall, order-insensitive.
  * ``timeline``        list of dated entries -- dates and entry count compared
                        structurally; title/description prose judged separately
                        (by the case reviewer), never by string equality.
  * ``key_allegations`` list of prose strings -- exact equality between two LLM
                        runs is noise, so these rows are marked
                        ``requires_reviewer`` and their exact verdict must not
                        be presented as a quality measure.
  * ``entities``        extraction only. The donor's write path
                        (``create_entity(display_name)`` -> flat-id PATCH) 400s
                        against today's ``EntityPatchItemSerializer``, which
                        requires a canonical NES ``@id`` IRI, so the port is
                        deliberately extraction-only. Write-path behaviour is
                        NOT comparable and is flagged as such.

THE CENTRAL SAFETY PROPERTY -- absence is not agreement.

``None``, ``""``, ``[]`` and ``{}`` all mean "this arm produced nothing".
A naive comparator normalises them to a single value and then reports
empty-vs-empty as a match, which turns "both arms did nothing" into
"100% agreement" -- the exact false-parity result this whole task exists to
detect. So absence is normalised to the ``ABSENT`` sentinel, which compares
equal ONLY to itself and is short-circuited BEFORE the equality ladder: if
neither arm produced output the verdict is ``no_output``, never ``all_agree``.
``no_output`` rows are excluded from every agreement rate (they are not
evidence of agreement in either direction) and counted separately and
prominently.

Note that ``0`` is deliberately NOT absent: a bigo of 0 is a real extraction
outcome ("the document states no amount"), distinct from "the stage never ran".
"""

import collections
import json

SET_FIELDS = ("tags",)
ORDERED_FIELDS = ("timeline",)
# Fields whose values are free prose: two LLM runs will essentially never
# produce byte-identical output, so an exact verdict on these carries no
# quality signal and must be adjudicated by the case reviewer instead.
SEMANTIC_FIELDS = ("key_allegations", "entities")
# The donor's entity WRITE path cannot be compared at all (see module docstring).
EXTRACTION_ONLY_FIELDS = ("entities",)

ADJUDICATE = ("b_diverges", "a_diverges", "both_diverge_from_golden", "all_differ")
# Verdicts in which Arm A and Arm B produced the SAME value. Golden agreement
# is a separate axis: "both diverge from golden" still means the port matched
# the donor, which is the actual port question.
AB_AGREE = ("all_agree", "both_diverge_from_golden")


class _Absent:
    """Sentinel for "this arm produced no value".

    Equal only to itself. Crucially it is NOT equal to ``None`` or ``[]``,
    so it can never be silently unified with a real value by the equality
    ladder in :func:`compare_field`.
    """

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __eq__(self, other):
        return other is self

    def __ne__(self, other):
        return other is not self

    def __hash__(self):
        return hash("<ABSENT>")

    def __repr__(self):
        return "<ABSENT>"

    def __bool__(self):
        return False


ABSENT = _Absent()


def is_absent(value):
    """True when a value means "this arm produced nothing".

    ``0`` and ``0.0`` are values, not absence -- a real extracted zero must
    never be confused with a stage that did not run.
    """
    if value is None:
        return True
    if isinstance(value, (str, list, dict, tuple, set)) and len(value) == 0:
        return True
    return False


def _norm(field, value):
    """Normalise a value for exact comparison, preserving absence."""
    if is_absent(value):
        return ABSENT
    if field in SET_FIELDS:
        return frozenset(value)
    if field in ORDERED_FIELDS or field in SEMANTIC_FIELDS:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return value


def _as_list(value):
    return list(value) if not is_absent(value) else []


def _tag_metrics(a, b):
    """Set metrics with Arm A (donor) as reference and Arm B (port) as candidate."""
    sa, sb = set(_as_list(a)), set(_as_list(b))
    union = sa | sb
    inter = sa & sb
    return {
        "n_a": len(sa),
        "n_b": len(sb),
        "jaccard": (len(inter) / len(union)) if union else None,
        "precision": (len(inter) / len(sb)) if sb else None,
        "recall": (len(inter) / len(sa)) if sa else None,
        "a_only": sorted(sa - sb),
        "b_only": sorted(sb - sa),
        "shared": sorted(inter),
    }


def _dates(entries):
    out = []
    for e in _as_list(entries):
        if isinstance(e, dict):
            out.append(e.get("date"))
    return out


def _timeline_metrics(a, b):
    """Structural timeline comparison: dates and counts only.

    Title/description prose is deliberately NOT scored here -- that is the
    case reviewer's job. What this reports is whether the two arms found the
    same events on the same dates, in the same order.
    """
    la, lb = _as_list(a), _as_list(b)
    da, db = _dates(a), _dates(b)
    sa, sb = set(d for d in da if d), set(d for d in db if d)
    union = sa | sb
    inter = sa & sb
    return {
        "n_a": len(la),
        "n_b": len(lb),
        "count_delta": len(lb) - len(la),
        "dates_a": da,
        "dates_b": db,
        "date_jaccard": (len(inter) / len(union)) if union else None,
        "dates_equal_ordered": da == db,
        "dates_equal_as_set": sa == sb,
        "a_only_dates": sorted(sa - sb),
        "b_only_dates": sorted(sb - sa),
    }


def _metrics(field, a, b):
    if field in SET_FIELDS:
        return _tag_metrics(a, b)
    if field in ORDERED_FIELDS:
        return _timeline_metrics(a, b)
    if field in SEMANTIC_FIELDS:
        return {"n_a": len(_as_list(a)), "n_b": len(_as_list(b))}
    return {"a": a, "b": b, "equal": _norm(field, a) == _norm(field, b)}


def compare_field(field, a, b, g):
    """Compare one field across the three arms.

    Returns a row dict carrying the verdict, the raw values, presence flags,
    field-appropriate metrics, and honesty flags telling the reporter when the
    exact verdict must NOT be read as a quality measure.
    """
    na, nb, ng = _norm(field, a), _norm(field, b), _norm(field, g)
    a_present, b_present, g_present = na != ABSENT, nb != ABSENT, ng != ABSENT

    # Short-circuit BEFORE the equality ladder. Two arms that produced nothing
    # have not agreed about anything -- they have simply produced no data.
    if not a_present and not b_present:
        verdict = "no_output"
        diverges_from_golden = g_present
    else:
        if na == nb == ng:
            verdict = "all_agree"
        elif na == nb:
            verdict = "both_diverge_from_golden"
        elif na == ng:
            verdict = "b_diverges"
        elif nb == ng:
            verdict = "a_diverges"
        else:
            verdict = "all_differ"
        diverges_from_golden = na != ng or nb != ng

    semantic = field in SEMANTIC_FIELDS
    return {
        "field": field,
        "a": a,
        "b": b,
        "g": g,
        "verdict": verdict,
        "diverges_from_golden": diverges_from_golden,
        "a_present": a_present,
        "b_present": b_present,
        "g_present": g_present,
        "metrics": _metrics(field, a, b),
        # True when byte equality of this field is prose noise rather than a
        # behavioural signal -- the case reviewer adjudicates these.
        "requires_reviewer": semantic,
        "exact_comparison_meaningful": not semantic,
        # False when the arms' WRITE paths cannot be compared at all.
        "write_path_comparable": field not in EXTRACTION_ONLY_FIELDS,
    }


# Verdicts that are NOT evidence of agreement or disagreement and must stay
# out of every rate: `no_output` (neither arm produced anything) and
# `readback_error` (we failed to MEASURE the row at all). Counting either as
# a comparable row would let a broken run report an agreement percentage.
NON_COMPARABLE = ("no_output", "readback_error")


def _rates(rows):
    """Agreement rates over COMPARABLE rows only."""
    comparable = [r for r in rows if r["verdict"] not in NON_COMPARABLE]
    n = len(comparable)
    agree = sum(1 for r in comparable if r["verdict"] == "all_agree")
    ab_agree = sum(1 for r in comparable if r["verdict"] in AB_AGREE)
    return {
        "total": len(rows),
        "comparable": n,
        "no_output": sum(1 for r in rows if r["verdict"] == "no_output"),
        # None, never 1.0, when nothing was comparable: a run that produced
        # nothing has no agreement rate to report.
        "agreement_rate": (agree / n) if n else None,
        "ab_agreement_rate": (ab_agree / n) if n else None,
    }


def three_way_report(rows):
    """Aggregate comparison rows into counts, rates and adjudication targets."""
    counts = dict(collections.Counter(r["verdict"] for r in rows))
    by_field = {}
    for field in sorted({r.get("field") for r in rows if r.get("field")}):
        field_rows = [r for r in rows if r.get("field") == field]
        by_field[field] = dict(
            _rates(field_rows),
            counts=dict(collections.Counter(r["verdict"] for r in field_rows)),
        )
    return dict(
        _rates(rows),
        counts=counts,
        by_field=by_field,
        needs_adjudication=[r for r in rows if r["verdict"] in ADJUDICATE],
        no_output_rows=[r for r in rows if r["verdict"] == "no_output"],
    )
