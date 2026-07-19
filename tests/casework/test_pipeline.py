# tests/casework/test_pipeline.py
import pytest

from casework.common.pipeline import (
    RunReport, STAGES, order_stages, unmet_prerequisites,
)

CASE_NO_MD = {"slug": "a", "evidence": [
    {"material_iri": "i", "material": {"material_type": "press_release",
     "urls": [{"link": "u", "role": "RAW"}]}}]}
CASE_MD = {"slug": "b", "bigo": 100, "evidence": [
    {"material_iri": "i", "material": {"material_type": "press_release",
     "urls": [{"link": "u", "role": "MARKDOWN"}]}}]}


def test_convert_runs_before_every_enricher():
    order = order_stages(["tags", "bigo", "convert", "timeline"])
    assert order[0] == "convert"


def test_tags_runs_after_bigo():
    order = order_stages(["tags", "bigo"])
    assert order.index("bigo") < order.index("tags")


def test_order_is_deterministic():
    assert order_stages(["tags", "bigo"]) == order_stages(["bigo", "tags"])


def test_unknown_stage_raises():
    with pytest.raises(KeyError):
        order_stages(["nope"])


def test_missing_markdown_is_an_unmet_prerequisite_not_a_skip():
    unmet = unmet_prerequisites(STAGES["bigo"], CASE_NO_MD)
    assert unmet, "no-MARKDOWN must be reported, not silently skipped"
    assert any("MARKDOWN" in u for u in unmet)


def test_satisfied_case_has_no_unmet_prerequisites():
    assert unmet_prerequisites(STAGES["bigo"], CASE_MD) == []


def test_one_converted_material_is_enough_even_if_others_are_not():
    # Mirrors materials.source_text: partial availability across MULTIPLE
    # bound materials is fine as long as at least one has usable text --
    # this is not the "all materials must be converted" reading. Both brief
    # fixtures (CASE_MD, CASE_NO_MD) carry exactly one bound material, so
    # `not any(...)` and `not all(...)` are indistinguishable against them;
    # mutation testing showed switching to `all` left every other test
    # green. This case has two.
    case = {"slug": "d", "evidence": [
        {"material_iri": "i1", "material": {"material_type": "press_release",
         "urls": [{"link": "u1", "role": "MARKDOWN"}]}},
        {"material_iri": "i2", "material": {"material_type": "press_release",
         "urls": [{"link": "u2", "role": "RAW"}]}},
    ]}
    assert unmet_prerequisites(STAGES["bigo"], case) == []


def test_no_bound_material_at_all_is_reported_distinctly_from_unconverted():
    # unmet_prerequisites has two separate material-related branches: "no
    # bound material of this type at all" vs. "bound, but none converted"
    # (CASE_NO_MD covers the latter). A case with no evidence exercises the
    # former -- without this test that whole branch can be gutted with no
    # test noticing (confirmed by mutation: folding it into the "not
    # converted" branch left the full suite green).
    case = {"slug": "e", "evidence": []}
    unmet = unmet_prerequisites(STAGES["bigo"], case)
    assert unmet, "zero bound materials must be reported, not silently ready"
    assert any("no bound material" in u for u in unmet)


def test_tags_reports_missing_bigo_dependency():
    case = dict(CASE_MD, bigo=None)
    assert any("bigo" in u for u in unmet_prerequisites(STAGES["tags"], case))


def test_run_report_separates_unmet_from_skipped():
    r = RunReport()
    r.record("a", "bigo", "unmet", "no MARKDOWN")
    r.record("b", "bigo", "skipped", "already filled")
    r.record("c", "bigo", "enriched")
    s = r.summary()
    assert s["unmet"] == 1 and s["skipped"] == 1 and s["enriched"] == 1


# --- Structural / declaration-level tests ---------------------------------
#
# order_stages() breaks ties between two INDEPENDENT stages by visiting
# sorted(names) at the top level. For the actual stage names in this module,
# that tie-break happens to agree with every real dependency edge except
# "allegations" (which alphabetically precedes "convert"): "bigo" < "tags",
# "convert" < "entities", "convert" < "timeline". That means an order_stages()
# assertion like `order.index("bigo") < order.index("tags")` (the brief's own
# test above) passes identically whether or not STAGES["tags"] actually
# declares bigo as a dependency -- deleting that dependency does not change
# the output, because alphabetical order already puts bigo first. Proven by
# mutation (see task-11-report.md): removing "bigo" from tags'
# requires_stages left test_tags_runs_after_bigo GREEN.
#
# The fix is to pin the dependency *declarations* directly, independent of
# the sorting algorithm's tie-break behaviour, and to add one order_stages
# case ("allegations" before "convert" alphabetically, but must come after
# it in the real order) that cannot pass by alphabetical accident.

def test_bigo_declares_convert_dependency():
    assert "convert" in STAGES["bigo"].requires_stages


def test_tags_declares_bigo_and_convert_dependencies():
    assert "bigo" in STAGES["tags"].requires_stages
    assert "convert" in STAGES["tags"].requires_stages


def test_timeline_declares_convert_dependency():
    assert "convert" in STAGES["timeline"].requires_stages


def test_allegations_declares_convert_dependency():
    assert "convert" in STAGES["allegations"].requires_stages


def test_entities_declares_convert_dependency():
    assert "convert" in STAGES["entities"].requires_stages


def test_convert_precedes_allegations_despite_alphabetical_order():
    # "allegations" < "convert" alphabetically, so this can only pass if the
    # dependency edge is actually honoured -- a decisive counter-example to
    # the alphabetical-accident gap described above.
    order = order_stages(["allegations", "convert"])
    assert order.index("convert") < order.index("allegations")


def test_order_stages_is_not_a_pass_through():
    # Guards against the degenerate "return list(names) unchanged"
    # implementation, which several of the assertions above would not
    # otherwise rule out on their own.
    assert order_stages(["tags", "bigo"]) != ["tags", "bigo"]


def test_stage_names_match_llm_tier_names():
    # casework.common.llm.TIERS keys must line up 1:1 with the enricher
    # stage names here (see llm.py docstring) -- a silent mismatch makes
    # tier_for() fall back to the default tier without any error.
    from casework.common.llm import TIERS
    assert set(TIERS) <= set(STAGES)


def test_cycle_in_requires_stages_is_detected(monkeypatch):
    import casework.common.pipeline as p
    from dataclasses import replace

    stages = dict(p.STAGES)
    stages["convert"] = replace(stages["convert"], requires_stages=("bigo",))
    monkeypatch.setattr(p, "STAGES", stages)
    with pytest.raises(ValueError):
        p.order_stages(["convert", "bigo"])
