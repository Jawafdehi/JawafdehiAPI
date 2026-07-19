"""Tests for the three-way A/B comparison logic.

The single most dangerous failure mode for this module is MANUFACTURED
AGREEMENT: reporting "the arms agree" when in truth neither arm produced
anything. The whole point of Task 16 is to measure whether the port
preserves the donor's behaviour, and a comparator that scores empty-vs-empty
as a match would report a run where both arms did nothing as 100% agreement.
A large block of these tests exists purely to pin that down.
"""

from casework.ab.diff import (
    ABSENT,
    compare_field,
    is_absent,
    three_way_report,
)


# --------------------------------------------------------------------------
# Brief-mandated behaviour (task-16-brief.md Step 1)
# --------------------------------------------------------------------------


def test_bigo_exact_match_all_three():
    r = compare_field("bigo", 100, 100, 100)
    assert r["verdict"] == "all_agree"


def test_bigo_port_diverges_from_donor():
    r = compare_field("bigo", 100, 250, 100)
    assert r["verdict"] == "b_diverges"


def test_both_arms_agree_but_differ_from_golden_is_flagged():
    # A genuine behaviour change from the API migration -- must be explained,
    # never assumed benign.
    r = compare_field("bigo", 250, 250, 100)
    assert r["verdict"] == "both_diverge_from_golden"


def test_tags_compared_as_sets_not_order():
    r = compare_field("tags", ["a", "b"], ["b", "a"], ["a", "b"])
    assert r["verdict"] == "all_agree"


def test_timeline_order_is_significant():
    t1 = [{"date": "2024-01-01", "title": "क"}, {"date": "2024-02-01", "title": "ख"}]
    t2 = list(reversed(t1))
    assert compare_field("timeline", t1, t2, t1)["verdict"] == "b_diverges"


def test_report_counts_verdicts_and_lists_adjudication_targets():
    rows = [
        {"slug": "a", "field": "bigo", "verdict": "all_agree"},
        {"slug": "b", "field": "bigo", "verdict": "b_diverges"},
        {"slug": "c", "field": "tags", "verdict": "both_diverge_from_golden"},
    ]
    rep = three_way_report(rows)
    assert rep["counts"]["all_agree"] == 1
    assert {"b", "c"} == {r["slug"] for r in rep["needs_adjudication"]}


# --------------------------------------------------------------------------
# FALSE PARITY: the failure this task exists to detect
# --------------------------------------------------------------------------


def test_both_arms_empty_is_never_agreement():
    """Both arms produced nothing. That is NOT agreement -- it is no data."""
    r = compare_field("bigo", None, None, None)
    assert r["verdict"] == "no_output"
    assert r["verdict"] != "all_agree"


def test_both_arms_empty_list_is_never_agreement():
    r = compare_field("tags", [], [], [])
    assert r["verdict"] == "no_output"


def test_both_arms_empty_timeline_is_never_agreement():
    r = compare_field("timeline", [], [], [])
    assert r["verdict"] == "no_output"


def test_both_arms_empty_allegations_is_never_agreement():
    r = compare_field("key_allegations", [], [], [])
    assert r["verdict"] == "no_output"


def test_no_output_is_flagged_as_regression_when_golden_had_a_value():
    """June shipped a value; neither arm reproduced it. Loud, not silent."""
    r = compare_field("bigo", None, None, 913280)
    assert r["verdict"] == "no_output"
    assert r["diverges_from_golden"] is True


def test_no_output_without_golden_is_not_a_golden_regression():
    r = compare_field("bigo", None, None, None)
    assert r["diverges_from_golden"] is False


def test_no_output_rows_are_excluded_from_agreement_rate():
    """Agreement rate must be computed over cases that produced output."""
    rows = [
        {"slug": "a", "field": "bigo", "verdict": "all_agree"},
        {"slug": "b", "field": "bigo", "verdict": "no_output"},
        {"slug": "c", "field": "bigo", "verdict": "no_output"},
    ]
    rep = three_way_report(rows)
    # 1 of 1 comparable rows agreed -- NOT 1 of 3, and NOT 3 of 3.
    assert rep["comparable"] == 1
    assert rep["agreement_rate"] == 1.0
    assert rep["counts"]["no_output"] == 2


def test_readback_error_is_excluded_from_the_agreement_rate():
    """A row we failed to MEASURE is not evidence of agreement either way."""
    rows = [
        {"slug": "a", "field": "bigo", "verdict": "all_agree"},
        {"slug": "b", "field": "bigo", "verdict": "readback_error"},
    ]
    rep = three_way_report(rows)
    assert rep["comparable"] == 1
    assert rep["agreement_rate"] == 1.0
    assert rep["counts"]["readback_error"] == 1


def test_a_run_of_only_readback_errors_has_no_agreement_rate():
    rows = [{"slug": "a", "field": "bigo", "verdict": "readback_error"}]
    rep = three_way_report(rows)
    assert rep["agreement_rate"] is None
    assert rep["ab_agreement_rate"] is None
    assert rep["comparable"] == 0


def test_agreement_rate_is_none_when_nothing_was_comparable():
    """Both arms produced nothing across the board -> no rate, not 100%."""
    rows = [
        {"slug": "a", "field": "bigo", "verdict": "no_output"},
        {"slug": "b", "field": "bigo", "verdict": "no_output"},
    ]
    rep = three_way_report(rows)
    assert rep["agreement_rate"] is None
    assert rep["comparable"] == 0


def test_none_and_empty_list_are_both_absent_but_absent_never_equals_present():
    assert is_absent(None)
    assert is_absent([])
    assert is_absent("")
    assert is_absent({})
    assert not is_absent(0)  # a real extracted zero is a value, not absence
    assert not is_absent(["x"])


def test_zero_bigo_is_a_value_not_absence():
    """bigo=0 is a real extraction ('no amount found'), not a missing field."""
    r = compare_field("bigo", 0, 0, 0)
    assert r["verdict"] == "all_agree"
    assert r["verdict"] != "no_output"


def test_one_arm_empty_other_populated_is_divergence_not_agreement():
    r = compare_field("tags", ["a"], [], ["a"])
    assert r["verdict"] == "b_diverges"


def test_absent_sentinel_only_equals_itself():
    assert ABSENT == ABSENT
    assert ABSENT != None  # noqa: E711 -- identity of the sentinel is the point
    assert ABSENT != []
    # Exercise __eq__ directly, not just __ne__: a sentinel that reported
    # equality with any falsy value would silently unify "produced nothing"
    # with a real empty/zero value everywhere the equality ladder runs.
    assert not (ABSENT == [])
    assert not (ABSENT == None)  # noqa: E711
    assert not (ABSENT == 0)
    assert not (ABSENT == "")
    assert not (ABSENT == {})


# --------------------------------------------------------------------------
# Field-appropriate comparison
# --------------------------------------------------------------------------


def test_tags_reports_set_metrics_not_just_equality():
    # Deliberately ASYMMETRIC (|A|=3, |B|=2) so that precision and recall take
    # different values -- a symmetric fixture cannot detect the two being
    # swapped, which is the easiest mistake to make in this function.
    r = compare_field("tags", ["a", "b", "c"], ["b", "d"], ["a", "b", "c"])
    m = r["metrics"]
    assert m["n_a"] == 3
    assert m["n_b"] == 2
    assert m["jaccard"] == 0.25  # |{b}| / |{a,b,c,d}|
    assert m["precision"] == 0.5  # of B's 2 tags, 1 is in A
    assert m["recall"] == 1 / 3  # of A's 3 tags, B found 1
    assert m["precision"] != m["recall"]
    assert set(m["a_only"]) == {"a", "c"}
    assert set(m["b_only"]) == {"d"}
    assert set(m["shared"]) == {"b"}


def test_tags_jaccard_of_disjoint_sets_is_zero():
    r = compare_field("tags", ["a"], ["b"], ["a"])
    assert r["metrics"]["jaccard"] == 0.0


def test_bigo_metrics_report_exact_equality_only():
    r = compare_field("bigo", 100, 250, 100)
    assert r["metrics"]["equal"] is False
    assert r["metrics"]["a"] == 100
    assert r["metrics"]["b"] == 250


def test_timeline_metrics_compare_dates_and_counts():
    a = [{"date": "2024-01-01", "title": "x"}, {"date": "2024-02-01", "title": "y"}]
    b = [{"date": "2024-01-01", "title": "DIFFERENT PROSE"}]
    r = compare_field("timeline", a, b, a)
    m = r["metrics"]
    assert m["n_a"] == 2
    assert m["n_b"] == 1
    assert m["count_delta"] == -1
    assert m["date_jaccard"] == 0.5
    assert m["dates_equal_ordered"] is False


def test_timeline_reordered_dates_are_not_equal_ordered():
    """Same date SET, different order. `dates_equal_ordered` must say False --
    a set-equality implementation would call these identical and hide a real
    chronology divergence between the arms."""
    a = [{"date": "2024-01-01", "title": "x"}, {"date": "2024-02-01", "title": "y"}]
    b = [{"date": "2024-02-01", "title": "y"}, {"date": "2024-01-01", "title": "x"}]
    m = compare_field("timeline", a, b, a)["metrics"]
    assert m["dates_equal_ordered"] is False
    assert m["dates_equal_as_set"] is True
    assert m["date_jaccard"] == 1.0
    assert m["count_delta"] == 0


def test_timeline_same_dates_different_prose_is_reported_separately():
    """Dates matching while titles differ is the interesting middle case:
    structural agreement with prose divergence. Must be visible, not collapsed."""
    a = [{"date": "2024-01-01", "title": "क"}]
    b = [{"date": "2024-01-01", "title": "ख"}]
    r = compare_field("timeline", a, b, a)
    assert r["metrics"]["dates_equal_ordered"] is True
    assert r["metrics"]["date_jaccard"] == 1.0
    # but the entries are not identical, so the exact verdict still diverges
    assert r["verdict"] == "b_diverges"


def test_key_allegations_is_marked_as_requiring_the_reviewer():
    """Exact string equality between two LLM runs is meaningless prose noise."""
    r = compare_field("key_allegations", ["alpha"], ["beta"], ["alpha"])
    assert r["requires_reviewer"] is True
    assert r["exact_comparison_meaningful"] is False


def test_bigo_and_tags_do_not_require_the_reviewer():
    assert compare_field("bigo", 1, 1, 1)["requires_reviewer"] is False
    assert compare_field("tags", ["a"], ["a"], ["a"])["requires_reviewer"] is False


def test_key_allegations_still_reports_counts():
    r = compare_field("key_allegations", ["a", "b"], ["c"], [])
    assert r["metrics"]["n_a"] == 2
    assert r["metrics"]["n_b"] == 1


def test_entities_requires_reviewer_and_is_extraction_only():
    r = compare_field("entities", ["e1"], ["e2"], [])
    assert r["requires_reviewer"] is True
    assert r["write_path_comparable"] is False


# --------------------------------------------------------------------------
# three_way_report bookkeeping
# --------------------------------------------------------------------------


def test_report_counts_every_verdict_kind():
    rows = [
        {"slug": "a", "field": "bigo", "verdict": "all_agree"},
        {"slug": "b", "field": "bigo", "verdict": "all_differ"},
        {"slug": "c", "field": "bigo", "verdict": "a_diverges"},
    ]
    rep = three_way_report(rows)
    assert rep["counts"]["all_differ"] == 1
    assert rep["counts"]["a_diverges"] == 1
    assert {"b", "c"} == {r["slug"] for r in rep["needs_adjudication"]}


def test_report_is_empty_safe():
    rep = three_way_report([])
    assert rep["counts"] == {}
    assert rep["needs_adjudication"] == []
    assert rep["agreement_rate"] is None
    assert rep["comparable"] == 0


def test_report_groups_by_field():
    rows = [
        {"slug": "a", "field": "bigo", "verdict": "all_agree"},
        {"slug": "b", "field": "bigo", "verdict": "b_diverges"},
        {"slug": "c", "field": "tags", "verdict": "all_agree"},
    ]
    rep = three_way_report(rows)
    assert rep["by_field"]["bigo"]["comparable"] == 2
    assert rep["by_field"]["bigo"]["agreement_rate"] == 0.5
    assert rep["by_field"]["tags"]["agreement_rate"] == 1.0


def test_report_by_field_excludes_no_output_from_its_rate_too():
    rows = [
        {"slug": "a", "field": "timeline", "verdict": "all_agree"},
        {"slug": "b", "field": "timeline", "verdict": "no_output"},
    ]
    rep = three_way_report(rows)
    assert rep["by_field"]["timeline"]["comparable"] == 1
    assert rep["by_field"]["timeline"]["no_output"] == 1
    assert rep["by_field"]["timeline"]["agreement_rate"] == 1.0


def test_ab_agreement_counts_arms_agreeing_regardless_of_golden():
    """A-vs-B agreement is the port question; golden is a separate axis."""
    rows = [
        {"slug": "a", "field": "bigo", "verdict": "all_agree"},
        {"slug": "b", "field": "bigo", "verdict": "both_diverge_from_golden"},
        {"slug": "c", "field": "bigo", "verdict": "b_diverges"},
    ]
    rep = three_way_report(rows)
    # all_agree + both_diverge_from_golden are both "A and B produced the same"
    assert rep["ab_agreement_rate"] == 2 / 3
