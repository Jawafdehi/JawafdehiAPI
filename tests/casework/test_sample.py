"""Tests for A/B sample selection.

The sample frame is a claim about what the A/B can generalise to, so the
exclusion rules (adapter-unmapped material, charge-sheet-only cases) must be
exact -- including a case into the sample that Arm A structurally cannot read
would manufacture an empty-vs-empty "agreement".
"""

from casework.ab.sample import (
    build_frame,
    has_mapped_material,
    has_repeated_mapped_type,
    is_charge_sheet_only,
    select_sample,
    stratum,
)


def case(*types, **kw):
    return dict({"material_types": list(types), "n_evidence": len(types)}, **kw)


def test_mapped_material_detection():
    assert has_mapped_material(case("press_release"))
    assert has_mapped_material(case("court_order"))
    assert has_mapped_material(case("ciaa_press_release"))
    assert not has_mapped_material(case("news", "legal_corpus"))
    assert not has_mapped_material(case())


def test_charge_sheet_alone_is_excluded_but_not_alongside_mapped_material():
    # The adapter does not map charge_sheet, so a case with ONLY a charge
    # sheet is invisible to Arm A -- excluded.
    assert is_charge_sheet_only(case("charge_sheet"))
    assert is_charge_sheet_only(case("charge_sheet", "news"))
    # But a charge sheet alongside a press release is fine: Arm A still sees
    # the press release.
    assert not is_charge_sheet_only(case("charge_sheet", "press_release"))
    assert not is_charge_sheet_only(case("news"))


def test_frame_excludes_unreadable_and_errored_cases():
    survey = {
        "good": case("press_release"),
        "cs": case("charge_sheet"),
        "newsonly": case("news"),
        "bad": {"error": "boom"},
    }
    frame, excluded = build_frame(survey)
    assert set(frame) == {"good"}
    assert excluded["cs"] == "charge_sheet_only"
    assert excluded["newsonly"] == "no_adapter_mapped_material"
    assert excluded["bad"] == "survey_error"


def test_strata_classification():
    assert stratum(case("press_release", "court_order")) == "press+court"
    assert stratum(case("press_release")) == "press_only"
    assert stratum(case("court_order")) == "court_only"
    assert stratum(case("news")) == "none"


def test_repeated_mapped_type_flags_aggregation_caveat():
    assert has_repeated_mapped_type(case("press_release", "press_release"))
    assert has_repeated_mapped_type(case("court_order", "court_order", "news"))
    assert not has_repeated_mapped_type(case("press_release", "court_order"))


def test_sample_is_deterministic_for_a_seed():
    survey = {f"c{i}": case("press_release", "court_order") for i in range(50)}
    a = select_sample(survey, n=10, seed="x")["slugs"]
    b = select_sample(survey, n=10, seed="x")["slugs"]
    assert a == b
    assert len(a) == 10


def test_sample_changes_with_seed():
    survey = {f"c{i}": case("press_release", "court_order") for i in range(50)}
    a = select_sample(survey, n=10, seed="x")["slugs"]
    b = select_sample(survey, n=10, seed="y")["slugs"]
    assert a != b


def test_sample_never_includes_an_excluded_case():
    survey = {f"c{i}": case("press_release") for i in range(20)}
    survey["cs"] = case("charge_sheet")
    survey["news"] = case("news")
    out = select_sample(survey, n=15, seed="s")
    assert "cs" not in out["slugs"]
    assert "news" not in out["slugs"]


def test_every_non_empty_stratum_is_represented():
    survey = {f"pc{i}": case("press_release", "court_order") for i in range(40)}
    survey["p1"] = case("press_release")
    survey["c1"] = case("court_order")
    out = select_sample(survey, n=10, seed="s")
    assert set(out["strata"]) == {"press+court", "press_only", "court_only"}
    assert out["strata"]["press_only"] == ["p1"]
    assert out["strata"]["court_only"] == ["c1"]
    assert len(out["slugs"]) == 10


def test_sample_size_is_capped_by_frame_size():
    survey = {"a": case("press_release"), "b": case("court_order")}
    out = select_sample(survey, n=99, seed="s")
    assert len(out["slugs"]) == 2
    assert out["frame_size"] == 2


def test_empty_frame_yields_empty_sample_not_an_error():
    out = select_sample({"n": case("news")}, n=5, seed="s")
    assert out["slugs"] == []
    assert out["frame_size"] == 0


def test_sample_reports_the_aggregation_caveat_subgroup():
    survey = {f"c{i}": case("press_release", "court_order") for i in range(10)}
    survey["dup"] = case("press_release", "press_release", "court_order")
    out = select_sample(survey, n=11, seed="s")
    assert "dup" in out["slugs"]
    assert out["repeated_mapped_type"] == ["dup"]


def test_allocation_is_proportional_to_stratum_size():
    survey = {f"pc{i}": case("press_release", "court_order") for i in range(90)}
    survey.update({f"p{i}": case("press_release") for i in range(10)})
    out = select_sample(survey, n=20, seed="s")
    # press+court is 90% of the frame, so it must dominate the sample.
    assert len(out["strata"]["press+court"]) > len(out["strata"]["press_only"])
    assert len(out["slugs"]) == 20
