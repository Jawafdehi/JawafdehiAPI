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


def test_unresolved_material_is_reported_distinctly_from_no_material():
    # A case fetched from the case LIST endpoint has evidence entries whose
    # `material` is null (only the DETAIL endpoint resolves materials).
    # Before the fix, materials_of_type() silently dropped these entries and
    # unmet_prerequisites reported the exact same "no bound material of type
    # X" string as a case with genuinely zero evidence -- collapsing "we
    # can't tell yet" into "definitely can't", which is exactly the kind of
    # false parity this module exists to prevent (see Task 16 concern).
    list_case = {"slug": "x", "evidence": [{"material_iri": "i", "material": None}]}
    empty_case = {"slug": "y", "evidence": []}
    list_unmet = unmet_prerequisites(STAGES["bigo"], list_case)
    empty_unmet = unmet_prerequisites(STAGES["bigo"], empty_case)
    assert list_unmet != empty_unmet
    assert any("UNRESOLVED" in u and "DETAIL" in u for u in list_unmet)
    assert not any("UNRESOLVED" in u for u in empty_unmet)


def test_unresolved_material_alongside_a_convertible_one_reports_both():
    # A case can have BOTH an unresolved (list-endpoint) entry and a
    # resolved-but-unconverted entry at once -- both reasons must surface,
    # not just one.
    case = {"slug": "z", "evidence": [
        {"material_iri": "i1", "material": None},
        {"material_iri": "i2", "material": {"material_type": "press_release",
         "urls": [{"link": "u2", "role": "RAW"}]}},
    ]}
    unmet = unmet_prerequisites(STAGES["bigo"], case)
    assert any("UNRESOLVED" in u for u in unmet)
    assert any("no MARKDOWN role" in u for u in unmet)


def test_tags_requires_no_materials():
    # Donor enrich_tags.py never reads evidence/materials -- it classifies
    # from case fields (title, key_allegations, court_cases, description)
    # alone. Its only "evidence" occurrence is the literal tag string
    # "evidence tamper", not a materials read.
    assert STAGES["tags"].requires_materials == ()


def test_tags_does_not_gate_on_missing_bigo_field():
    # Donor treats bigo as optional context: _detect_amount_tier(None)
    # returns None (the amount-tier tag is simply omitted) and the LLM
    # prompt builder guards `if bigo is not None` -- the donor tags cases
    # fine with an unknown disputed amount. A requires_fields hard gate
    # would incorrectly skip every such case.
    case = dict(CASE_MD, bigo=None)
    assert unmet_prerequisites(STAGES["tags"], case) == []
    assert STAGES["tags"].requires_fields == ()


def test_tags_still_orders_after_bigo():
    # Dropping the hard gate must not drop the ordering preference -- bigo
    # should still run before tags when both are requested.
    assert "bigo" in STAGES["tags"].requires_stages


def test_entities_accepts_court_order_alone():
    # Donor enrich_related_entities.py::_get_content_for_case collects press
    # release and court order content independently; the caller only skips
    # when BOTH are absent. A press-only requires_materials would strand
    # every court-order-only case.
    case = {"slug": "c", "evidence": [
        {"material_iri": "i", "material": {"material_type": "court_order",
         "urls": [{"link": "u", "role": "MARKDOWN"}]}}]}
    assert unmet_prerequisites(STAGES["entities"], case) == []


def test_entities_accepts_press_release_alone():
    assert unmet_prerequisites(STAGES["entities"], CASE_MD) == []


def test_entities_requires_materials_includes_court_types():
    from casework.common.pipeline import COURT_TYPES, PRESS_TYPES
    assert set(COURT_TYPES) <= set(STAGES["entities"].requires_materials)
    assert set(PRESS_TYPES) <= set(STAGES["entities"].requires_materials)


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
