"""Registry entry for the BIGO-extraction prompt.

The prompt text is sourced live from ``casework.enrich_missing_bigo`` so there is exactly
one copy in the tree: this spec is a *view* over the production constants, not a duplicate.
A drift-guard test (``evals/test_enrich_missing_bigo_eval.py``) asserts the two stay
byte-identical. The Phase-1 migration described in
``docs/llm-prompt-centralization-and-evals.md`` inverts this dependency (the enricher
imports from the registry); until then we keep the enricher untouched and read from it.
"""

from __future__ import annotations

from casework.enrich_missing_bigo import (
    EXTRACTION_SYSTEM_PROMPT,
    EXTRACTION_USER_PROMPT,
)
from llm.prompts.spec import PromptSpec

OUTPUT_SCHEMA = {
    "type": "object",
    "required": ["bigo", "confidence", "evidence_quote", "press_release_type"],
    "properties": {
        "bigo": {"type": "integer", "nullable": True},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "evidence_quote": {"type": "string"},
        "press_release_type": {
            "type": "string",
            "enum": ["sting_operation", "appeal_review", "charge_filing", "other"],
        },
    },
}

SPEC = PromptSpec(
    key="enrich.missing_bigo",
    version="1.0.0",
    system=EXTRACTION_SYSTEM_PROMPT,
    user_template=EXTRACTION_USER_PROMPT,
    tier="premium",
    max_tokens=2000,
    output_schema=OUTPUT_SCHEMA,
    examples="evals/datasets/enrich_missing_bigo/golden.json",
    source_ref="casework/enrich_missing_bigo.py::_extract_bigo_from_source",
    variables=("case_id", "case_title", "source_context", "markdown"),
    notes=(
        "Extracts BIGO (बिगो, the damage-claim amount) from a CIAA press release. "
        "The model returns the schema above; deterministic post-processing "
        "(_coerce_bigo_int / _parse_bigo_response in the enricher) normalises Devanagari "
        "digits, strips the paisa suffix, and gates on confidence + bigo-context keywords."
    ),
)
