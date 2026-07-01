"""DB-free review runner used by the API-driven poller.

This is the pure computational core of a review, extracted from the old
``pipeline.run_review`` so it can run on a poller that talks ONLY to the
casework HTTP API (claim job -> process -> submit result) and never touches the
``CaseReview`` / ``ReviewConfig`` database tables.

Inputs are plain data (a case dict + a config dict, both delivered by the claim
endpoint); outputs are a plain result dict the poller posts back. likhit source
conversion happens here, locally on the poller; the converted markdown is NOT
uploaded — only the final scored result is submitted.
"""

import time

from django.conf import settings

from . import bedrock_judge, casetype, code_rules, converter, jds_client, scorer


class _Config:
    """Minimal stand-in for ReviewConfig (scorer reads these attributes)."""

    def __init__(self, data=None):
        data = data or {}
        self.pass_threshold = int(data.get("pass_threshold", 80))
        self.revise_threshold = int(data.get("revise_threshold", 60))
        self.llm_samples = int(data.get("llm_samples", 3))


def process_case(case, config=None, on_stage=None):
    """Run convert -> analyse -> score for a case dict. Returns a result payload.

    `case`   : the normalized case dict (from the claim endpoint).
    `config` : dict with pass_threshold / revise_threshold / llm_samples.
    `on_stage(stage)` : optional callback for progress (best-effort, may no-op).

    Returns a dict the poller submits to the result endpoint:
        {case_title, case_state, case_type, source_count, sources_converted,
         result, duration_seconds}
    Raises on failure (the poller reports it to the fail/result endpoint).
    """
    cfg = _Config(config)

    def stage(name):
        if on_stage:
            try:
                on_stage(name)
            except Exception:  # noqa: BLE001 - progress reporting is best-effort
                pass

    t0 = time.monotonic()

    # 1. Sources from the case (pure; no DB).
    stage("converting_sources")
    sources = jds_client.extract_sources(case)
    source_count = len(sources)

    # 2. likhit conversion — LOCAL to the poller, used only to feed the
    #    per-source Bedrock analysis below. The markdown is NOT persisted: NGM
    #    Materials own their own links, written through the material upload API,
    #    so the old "attach markdown back to the DocumentSource" maintenance path
    #    is retired (ADR: cases own no documents).
    converted = converter.convert_all(sources)
    sources_converted = sum(
        1 for s in converted if s.get("conversion_status") in ("converted", "attached")
    )

    # 3. Per-source analysis (Bedrock).
    stage("analyzing_sources")
    ctype = casetype.detect(case)
    source_analyses = bedrock_judge.analyze_sources(
        scorer.build_case_summary(case), converted, ctype["label"]
    )

    # 4. Rule-centered scoring (Bedrock). Rules are code-enforced (no DB).
    stage("scoring")
    rules = code_rules.get_enabled_rules()
    result = scorer.score_case(
        case, converted, rules, cfg, source_analyses=source_analyses
    )
    result["model_id_used"] = settings.BEDROCK_MODEL_ID

    return {
        "case_title": case.get("title", "") or "",
        "case_state": case.get("state", "") or "",
        "case_type": (result.get("case_type") or {}).get("type", ""),
        "source_count": source_count,
        "sources_converted": sources_converted,
        "result": result,
        "duration_seconds": round(time.monotonic() - t0, 1),
    }
