"""Eval gate for the BIGO enricher — runs in CI, no LLM/network required.

These are the deterministic, model-independent checks: real-data field-level accuracy,
schema conformance, prompt-variable rendering, and a drift guard proving the prompt
registry is byte-identical to the live enricher. The optional DeepEval judge test skips
unless ``deepeval`` is installed.

    poetry run pytest evals/test_enrich_missing_bigo_eval.py
"""

from __future__ import annotations

import pytest

from casework.enrich_missing_bigo import (
    EXTRACTION_SYSTEM_PROMPT,
    EXTRACTION_USER_PROMPT,
)
from evals.metrics import deterministic as det
from llm.prompts import get
from llm.prompts.spec import validate_output

GOLDEN = det.load_golden()


@pytest.mark.parametrize(
    "entry",
    GOLDEN["amount_coercion"],
    ids=[e["court_case"] for e in GOLDEN["amount_coercion"]],
)
def test_amount_coercion_matches_real_bigo(entry):
    """Each real CIAA amount string normalises to the human-approved bigo integer."""
    assert det.coerce_amount(entry["raw_amount"]) == entry["expected_bigo"]


def test_amount_coercion_field_accuracy_is_perfect():
    """The whole real coercion golden set scores 100% (regression gate)."""
    rep = det.score_amount_coercion(GOLDEN["amount_coercion"])
    assert rep["field_accuracy"] == 1.0, rep["results"]


def test_schema_accepts_valid_and_rejects_malformed():
    schema = get("enrich.missing_bigo").output_schema
    valid = {
        "bigo": 11001199,
        "confidence": "high",
        "evidence_quote": "बिगो रु. १,१०,०१,१९९।७५ कायम",
        "press_release_type": "charge_filing",
    }
    malformed = {"bigo": "lots", "confidence": "definitely", "evidence_quote": 5}
    assert validate_output(valid, schema) == []
    assert validate_output(malformed, schema), "malformed output should be flagged"


def test_null_bigo_is_schema_valid():
    """A sting/appeal release yields bigo=null, which must conform (nullable integer)."""
    schema = get("enrich.missing_bigo").output_schema
    null_case = {
        "bigo": None,
        "confidence": "high",
        "evidence_quote": "रंगेहात पक्राउ",
        "press_release_type": "sting_operation",
    }
    assert validate_output(null_case, schema) == []


def test_bigo_field_match_metric():
    assert det.bigo_field_match(11001199, 11001199) is True
    assert det.bigo_field_match(None, None) is True
    assert det.bigo_field_match(123, 456) is False


def test_registry_spec_matches_live_enricher():
    """Drift guard: the registry prompt is a view over the enricher, never a stale copy."""
    spec = get("enrich.missing_bigo")
    assert spec.system == EXTRACTION_SYSTEM_PROMPT
    assert spec.user_template == EXTRACTION_USER_PROMPT
    assert spec.version == "1.0.0"


def test_prompt_renders_declared_variables():
    spec = get("enrich.missing_bigo")
    rendered = spec.render_user(
        case_id="case-081-cr-0136",
        case_title="Oxygen plant",
        source_context="title: ...",
        markdown="बिगो रु. १,१०,०१,१९९।७५",
    )
    assert "case-081-cr-0136" in rendered
    assert "१,१०,०१,१९९।७५" in rendered


def test_deepeval_judge_routes_through_providers_when_installed():
    """If deepeval is installed, the judge must be model-pinned to our own tiers."""
    pytest.importorskip("deepeval")
    from evals.judge_model import ProviderJudge

    judge = ProviderJudge(tier="premium")
    assert judge.get_model_name().startswith("jawafdehi:")
