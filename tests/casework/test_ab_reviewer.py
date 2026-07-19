"""Tests for scoring A/B output with the case reviewer.

Two properties matter most. First, scoring must be OFFLINE and deterministic
-- if an LLM-graded rule leaked in, the benchmark would carry sampling noise
and two identical inputs could score differently. Second, both arms must be
graded against the SAME rule set; otherwise a rule-set difference would
masquerade as a quality difference.
"""

import pytest

from casework.ab.reviewer import (
    FIELDS,
    RELEVANT_RULES,
    _Config,
    assert_same_rule_basis,
    deterministic_rules,
    score,
    score_arms,
    splice,
)


def base_case(**kw):
    return dict({
        "slug": "case-080-cr-0001-test",
        "title": "परीक्षण मुद्दा 080-CR-0001",
        "state": "DRAFT",
        "case_type": "CORRUPTION",
        "description": "एक परीक्षण मुद्दा।",
        "court_cases": ["https://ngm.jawafdehi.org/courtcase/special/080-CR-0001"],
        "evidence": [],
        "entities": [],
        "bigo": None,
        "tags": [],
        "timeline": [],
        "key_allegations": [],
    }, **kw)


# ------------------------------------------------------------- splicing ---


def test_splice_replaces_only_the_enriched_fields():
    case = base_case()
    out = splice(case, {"bigo": 500, "tags": ["क"]})
    assert out["bigo"] == 500
    assert out["tags"] == ["क"]
    # everything the rule set keys off must be untouched
    assert out["court_cases"] == case["court_cases"]
    assert out["case_type"] == case["case_type"]
    assert out["title"] == case["title"]


def test_splice_does_not_mutate_the_input_case():
    case = base_case()
    splice(case, {"bigo": 999})
    assert case["bigo"] is None


def test_splice_ignores_unknown_keys():
    out = splice(base_case(), {"not_a_field": 1})
    assert "not_a_field" not in out


def test_splice_refuses_to_touch_rule_basis_fields_even_if_asked():
    """`court_cases` / `case_type` decide WHICH rules apply. If an arm's
    values could overwrite them, the two arms could be graded against
    different rule sets and the score gap would be an artefact."""
    case = base_case()
    out = splice(case, {
        "bigo": 5,
        "court_cases": ["https://ngm.jawafdehi.org/courtcase/special/999-CR-9999"],
        "case_type": "SOMETHING_ELSE",
        "evidence": [{"material": {"material_type": "news"}}],
    })
    assert out["court_cases"] == case["court_cases"]
    assert out["case_type"] == case["case_type"]
    assert out["evidence"] == case["evidence"]
    assert out["bigo"] == 5


def test_fields_are_the_four_under_test():
    assert set(FIELDS) == {"bigo", "tags", "timeline", "key_allegations"}


# ------------------------------------------------------ offline / rules ---


def test_only_deterministic_rules_are_used():
    """An LLM-graded rule would make the benchmark non-reproducible and
    would fire a network call during scoring."""
    from review import code_rules

    rules = deterministic_rules()
    assert rules, "expected a non-empty deterministic rule set"
    assert all(r.kind == code_rules.KIND_DETERMINISTIC for r in rules)
    assert not any(r.kind == code_rules.KIND_LLM for r in rules)


def test_scoring_never_calls_the_llm(monkeypatch):
    """Hard proof the benchmark is offline: make any invoke blow up."""
    import llm.invoke as invoke_mod

    def _boom(*a, **kw):
        raise AssertionError("the reviewer benchmark must not call an LLM")

    for name in ("invoke_json", "invoke_text"):
        if hasattr(invoke_mod, name):
            monkeypatch.setattr(invoke_mod, name, _boom)
    out = score(base_case(), {"bigo": 500, "tags": ["क", "ख", "ग"]})
    assert out["overall_score"] is not None


def test_scoring_is_deterministic():
    values = {"bigo": 500, "tags": ["क"], "key_allegations": ["एक"]}
    a = score(base_case(), values)
    b = score(base_case(), values)
    assert a["overall_score"] == b["overall_score"]
    assert a["rules"] == b["rules"]


def test_relevant_rules_are_reported():
    out = score(base_case(), {"bigo": 500})
    assert set(out["rules"]) <= set(RELEVANT_RULES)
    assert "bigo_amount_present" in out["all_rules"]


# ------------------------------------------------- discriminating power ---


def test_bigo_presence_moves_the_bigo_rule():
    without = score(base_case(), {"bigo": None})
    with_ = score(base_case(), {"bigo": 913280})
    assert (with_["rules"]["bigo_amount_present"]["score"]
            > without["rules"]["bigo_amount_present"]["score"])


def test_more_timeline_entries_score_higher():
    few = score(base_case(), {"timeline": [
        {"date": "2024-01-01", "title": "क", "description": "विवरण"}]})
    many = score(base_case(), {"timeline": [
        {"date": f"2024-0{i}-01", "title": "क",
         "description": "पर्याप्त लामो विवरण यहाँ राखिएको छ।"} for i in range(1, 6)]})
    assert (many["rules"]["timeline_completeness"]["score"]
            >= few["rules"]["timeline_completeness"]["score"])


def test_allegations_and_tags_only_move_structural_completeness():
    """Documents the reviewer's WEAK coverage of these two fields: they have
    no dedicated rule, so a score change can only appear in the structural
    aggregate. If this ever fails, the report's caveats need rewriting."""
    none = score(base_case(), {"key_allegations": [], "tags": []})
    some = score(base_case(), {
        "key_allegations": ["क", "ख", "ग", "घ"], "tags": ["क", "ख", "ग"]})
    moved = {k for k in none["all_rules"]
             if none["all_rules"][k]["score"] != some["all_rules"][k]["score"]}
    assert moved <= {"structural_completeness"}
    assert (some["rules"]["structural_completeness"]["score"]
            > none["rules"]["structural_completeness"]["score"])


# --------------------------------------------------------- rule basis ---


def test_same_rule_basis_passes_for_identically_typed_cases():
    a = splice(base_case(), {"bigo": 1})
    b = splice(base_case(), {"bigo": 2})
    assert assert_same_rule_basis(a, b) is not None


def test_differing_case_type_is_rejected_not_silently_scored():
    a = base_case()
    b = base_case(case_type="OTHER", court_cases=[])
    from review import casetype

    if casetype.detect(a) == casetype.detect(b):
        pytest.skip("fixtures do not produce distinct case types")
    with pytest.raises(AssertionError):
        assert_same_rule_basis(a, b)


def test_score_arms_grades_every_arm_on_the_same_basis():
    out = score_arms(base_case(), {
        "A": {"bigo": 100, "tags": ["क"]},
        "B": {"bigo": 100, "tags": ["क"]},
    })
    assert set(out) == {"A", "B"}
    assert out["A"]["overall_score"] == out["B"]["overall_score"]


def test_score_arms_separates_arms_that_produced_different_output():
    out = score_arms(base_case(), {
        "A": {"bigo": 913280, "timeline": [
            {"date": "2024-01-01", "title": "क", "description": "विवरण"}]},
        "B": {"bigo": None, "timeline": []},
    })
    assert out["A"]["overall_score"] > out["B"]["overall_score"]


def test_empty_output_scores_lower_than_real_output_not_equal():
    """The false-parity guard, at the benchmark level: an arm that produced
    nothing must not score the same as one that produced real content."""
    nothing = score(base_case(), {
        "bigo": None, "tags": [], "timeline": [], "key_allegations": []})
    something = score(base_case(), {
        "bigo": 913280, "tags": ["क", "ख", "ग"],
        "timeline": [{"date": "2024-01-01", "title": "क", "description": "विवरण"}],
        "key_allegations": ["क", "ख", "ग", "घ"]})
    assert something["overall_score"] > nothing["overall_score"]


def test_config_defaults_are_explicit():
    cfg = _Config()
    assert cfg.pass_threshold == 80
    assert cfg.revise_threshold == 40
    assert cfg.llm_samples == 1
