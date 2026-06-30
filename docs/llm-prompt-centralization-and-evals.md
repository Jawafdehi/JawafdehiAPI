# Design: LLM Prompt Centralization & Model-Independent Eval Layer

## Status: 🟡 PROPOSED (RFC — not yet implemented)

## Overview

We run ~11 LLM "enricher" pipelines (title, description, allegations, timeline, bigo, evidence, card, related-entities, news, tags, missing-bigo) plus an LLM "case review" grader. Every prompt today is an inline Python string constant scattered across `casework/enrich_*.py`, `review/judge.py`, `cases/services/case_scraper.py` and `scripts/`. There is a mature provider/model abstraction (`llm/`), but **no prompt registry, no prompt versioning, and no eval layer at all**. The consequence: we cannot gate prompt edits, cannot prove a cheaper or different model is safe to swap, and cannot measure whether our Nepali LLM-judge is even reliable. This RFC proposes (1) centralizing prompts as versioned data, and (2) a model-independent eval/testing layer built on `pytest` + DeepEval, reusing the abstractions we already have.

## Problem Statement

1. **Prompt fragmentation.** Each enricher carries its own `SYSTEM_PROMPT` / `USER_PROMPT` inline. A single-word edit silently changes pipeline behaviour with no version history, no isolated test, and no fast rollback. There is real copy-paste duplication: the tag keyword dictionaries (`SECTOR_KEYWORDS`, `CORRUPTION_TYPE_KEYWORDS`, `REGION_KEYWORDS`) are defined verbatim in both `casework/enrich_tags.py` and `cases/services/tag_enricher.py`.
2. **No eval layer.** Existing tests are mocked-transport unit tests (`tests/test_llm_*.py`) plus deterministic rule tests (`tests/test_review_*.py`). There are no golden datasets, no output-quality regression, and no model-comparison harness. The review grader's N-sample variance is a self-consistency signal, not ground truth.
3. **Model-swap is unprovable.** `llm/routing.py` already lets us point the `premium`/`cheap` tiers at different providers/models, but we have no way to demonstrate that a swap (e.g. moving a routine enricher to the cheap tier, or onto a new model) preserves output quality. Swaps are therefore made on faith.
4. **Nepali reliability is unmeasured.** All of these prompts operate primarily in Nepali (Devanagari), a low-resource language for current models. We have never measured whether our LLM-judge agrees with our human reviewers on Nepali cases.

## Current State (grounded in the code)

- **Transport / model independence — already solved.** `llm/invoke.py` exposes a 3-function choke point (`invoke_text`, `invoke_json`, `invoke_with_tools`) that every enricher and the judge route through. `llm/routing.py` selects a provider per tier (`premium`/`cheap`); providers are Bedrock, an OpenAI-compatible proxy, Claude CLI and Codex CLI. Models are chosen by env var. `llm/usage.py` (`UsageAccumulator`) already tracks tokens per `(provider, tier, model)`, and `CaseReview.result` already stores `model_id_used` and `token_usage`.
- **Prompts-as-data — already proven internally.** `review/rule_defaults.py` is exactly the pattern the industry recommends: each rule is a data object with `title`, a markdown `description`, `good_examples` / `bad_examples`, `weight`, `is_gate` / `gate_min`, `applies_to` and `tier`. The judge (`review/judge.py`) samples each LLM rule N× and computes mean/variance/confidence. This is the model we should generalize to the enrichers — we are not inventing a new pattern, we are extending one we already trust.
- **The gap is everything between the prompt strings and the choke point**: there is no layer that loads prompts as versioned artifacts, validates outputs against a schema, logs which prompt version produced a field, or scores output quality against a reference.

## Industry Findings (2024–2026)

Condensed from a focused literature/tooling sweep (full citations in References).

- **Prompt management.** The consensus is to treat prompts as *immutable, externally-stored, semantically-versioned artifacts*, pinned per-environment, with eval-gated promotion and pointer-based rollback, and to **log the prompt version on every request**. The "prompt-as-code vs prompt-as-data" debate has resolved into *both*: store prompts as data, manage them with code discipline (review, CI, eval). Teams that deliberately avoid heavy SaaS platforms keep **plain prompt files in Git + a registry object + pytest**. The payoff threshold is ~50 prompts; at ~15–20 distinct prompts we are right where it starts paying off.
- **Eval stack.** Most teams run two layers: a code-first framework for CI gating + an observability layer for production. For a self-hosted Python/Django/pytest shop, the relevant code-first tools are **DeepEval** ("pytest for LLMs", 50+ metrics incl. faithfulness/hallucination, model-agnostic judge) and **promptfoo** (CLI/YAML, zero cloud deps, strong for prompt/model A-B). Braintrust/LangSmith are commercial all-in-one platforms — overkill and at odds with our self-hosting posture.
- **Golden datasets + CI gating.** Golden sets should come from **real, human-approved data**, expert-annotated to the expected output. Wire evals into PRs; block on regression. Gate on **per-cohort deltas, not the aggregate** — an aggregate can improve while a specific cohort (e.g. `bigo`) silently regresses. Promote escaped failures back into the golden set.
- **Structured-extraction eval** (most of our enrichers). Standard granularity is **two-tier accuracy**: field-level (proportion of correct fields) *and* document-complete (a doc fails if any field is wrong). Score on **accuracy + hallucination rate + format/schema conformance**, not a single number. Faithfulness must be **strictly grounded** — penalize any field not explicitly supported by the source.
- **LLM-as-judge reliability.** Use rubrics with concrete examples, separate reasoning from the decision field, match rubric complexity to judge capability (use the premium tier as judge), **pin the judge model independent of the graded model** (avoid self-preference bias), and ensemble/sample for high-stakes calls. Absolute/rubric scoring (not pairwise) is correct when you need a quality threshold and per-criterion diagnostics — which is our case.

## ⚠️ The Nepali Caveat (most important finding)

Multilingual-judging research is blunt: **LLM-as-judge reliability degrades badly in low-resource languages.** Documented, directly-relevant effects: cross-language judge consistency around Fleiss' κ ≈ 0.3 (near-random), worst in low-resource languages, not fixed by bigger models or multilingual fine-tuning; systematic **over-optimistic scoring** and over-assignment of middle-ground scores; and **"translationese bias"** (judges prefer machine-translated text over flawed-but-human references), again worst for low-resource languages. Design implications we will adopt as hard rules:

1. **Lean deterministic for Nepali.** Field-level checks (court-number regex, normalized `bigo` amount match, BS↔AD date validation, headcount guards, schema/format conformance, entity-presence) are language-agnostic and free. Push as much eval as possible onto these. We already have this philosophy in `review/rules_engine.py`.
2. **Calibrate the judge against our own human gold before trusting it.** We hold a labelled set: the 51 priority cases + round-2 reviews with human dispositions. Measure judge-vs-human agreement (κ) on *our own Nepali data*. If agreement is low, judge numeric scores are advisory, not gates.
3. **Keep ensembling and bilingual examples.** The N-sample variance the judge already runs, and the Nepali good/bad examples in `rule_defaults.py`, are exactly the recommended mitigations — keep both.

## Proposed Solution

### Phase 1 — Centralize prompts as versioned data

Generalize the `rule_defaults.py` pattern. Each prompt becomes a `PromptSpec` data object, not an inline string.

```
services/jawafdehi-api/
  llm/
    invoke.py            # add invoke_prompt() wrapper; log (prompt_key, version)
    prompts/
      __init__.py
      spec.py            # PromptSpec dataclass + registry loader
      registry.py        # key -> PromptSpec, version pinning, render()
      enrich_bigo.py     # one module per prompt (or a YAML/TOML file set)
      enrich_title.py
      ...
```

`PromptSpec` fields (mirrors a rule, plus eval hooks):

- `key` — stable identifier, e.g. `enrich.missing_bigo`.
- `version` — semantic, immutable; bump on any edit.
- `system` — system prompt template.
- `user_template` — user prompt template with **declared** variables (use `string.Template` or Jinja, replacing implicit f-strings so the variable surface is explicit).
- `tier` — `premium` | `cheap` (drives routing).
- `max_tokens`.
- `output_schema` — JSON Schema for the expected output (enables automatic validation + schema-conformance eval).
- `examples` — golden few-shot / reference pairs in Nepali (doubles as eval seed data).

New thin wrapper in `llm/invoke.py`:

```python
def invoke_prompt(spec, *, usage=None, **vars) -> dict | str:
    system = render(spec.system, vars)
    content = render(spec.user_template, vars)
    out = invoke_json(system, content, max_tokens=spec.max_tokens, tier=spec.tier, usage=usage)
    validate_schema(out, spec.output_schema)        # raise/repair on mismatch
    record_prompt_version(usage, spec.key, spec.version)  # traceability
    return out
```

Traceability: extend `UsageAccumulator` (and the `CaseReview.result` / enricher output envelope) to record `(prompt_key, version)` alongside the existing `model_id_used` and `token_usage`. Every produced field then traces back to an exact prompt version.

De-duplication: extract the shared tag keyword dictionaries into a single module (e.g. `casework/tag_vocabulary.py`) imported by both `enrich_tags.py` and `cases/services/tag_enricher.py`.

Rollout is mechanical and per-enricher: move the inline `SYSTEM_PROMPT`/`USER_PROMPT` into a `PromptSpec`, switch the call site to `invoke_prompt`, leave behaviour byte-identical (same text → same output). No behavioural change in Phase 1; it is pure extraction.

### Phase 2 — Model-independent eval layer (DeepEval + pytest)

```
  evals/
    __init__.py
    judge_model.py       # DeepEvalBaseLLM adapter -> llm/routing premium provider
    datasets/            # golden cases: input doc -> expected fields (versioned, from human-approved cases)
      enrich_missing_bigo/
      enrich_title/
      review/
    metrics/
      deterministic.py   # schema-valid, field-match, amount-normalize, court-regex, headcount, format-conformance
      rubric.py          # G-Eval rubric metrics for open-ended Nepali fields (title/description faithfulness)
    test_evals.py        # pytest entry: runs golden set, asserts thresholds — gated in CI
    run_eval.py          # CLI: --model/--tier -> per-field/per-rule delta table for model swaps
```

Key integration detail (DeepEval is model-agnostic but defaults its judge to OpenAI): we wrap our existing premium provider in a `DeepEvalBaseLLM` subclass (`evals/judge_model.py`) so all G-Eval / faithfulness metrics run **on our own infra through `llm/routing`**, never calling out to OpenAI. This also lets us **pin the judge model independent of the graded model** (the bias-mitigation requirement) and keeps eval inside our data-residency boundary.

Two check types, matching the structured-extraction research:

1. **Deterministic field-level checks** (cheap, language-agnostic, run on every PR): JSON-schema validity, normalized `bigo` amount equality, court-number regex, BS↔AD date validity, headcount guards, entity-presence, format conformance. Implemented as plain `pytest` asserts and/or DeepEval custom `BaseMetric` deterministic metrics. These carry the bulk of the signal at zero LLM cost — critical for the Nepali reliability problem.
2. **LLM-judge checks** (for open-ended fields only): faithfulness/grounding and rubric quality via DeepEval `GEval`, seeded with the same Nepali good/bad examples we keep in the registry. Run a small slice per-PR (smoke), the full set nightly, on the cheap tier with prompt caching to keep cost down.

Golden datasets: build from data we already have — the 51 priority cases, the round-2 reviews, and enriched NGM cases are human-approved (input → expected fields) pairs. Version the datasets; promote any escaped production failure back into them.

CI gate: a GitHub Actions job runs `evals/test_evals.py` against the `main` baseline on any PR that touches `llm/prompts/**`, `review/rule_defaults.py`, or model-config env. Block the merge on **per-cohort regression** (e.g. bigo-accuracy, title-headcount, description-faithfulness), not just the aggregate. This sits *before* the keel image build, so a regressing prompt never reaches a deployed image.

Model-migration as a first-class command: `python evals/run_eval.py --tier cheap --model <candidate>` produces a per-field / per-rule delta table proving (or disproving) that a cheaper/different model preserves quality — the exact capability we lack today, and the direct answer to "can we move this enricher to the cheap tier safely?".

### Phase 3 — Later / optional

- **Judge calibration (do this early, cheaply):** before trusting any LLM-judge gate, run the existing judge over the 51 human-reviewed cases and compute agreement (κ) with the human dispositions. This is a few hours of work, reuses `review/judge.py`, and tells us whether the judge is a gate or merely advisory for Nepali. Strongly recommended as the very first eval-layer task.
- **DSPy / GEPA prompt optimization:** only after golden sets exist. The literature is candid that DSPy's real win is the discipline of codified evals + reproducibility + model portability, and that it is "no substitute for careful manual prompting with SMEs" on nuanced tasks. For Nepali legal nuance our human reviewers stay primary; DSPy could later auto-tune few-shot examples for the cheap tier once the eval harness is in place.

## Tooling Decision

**DeepEval (pytest-native), home-hosted, judge routed through our own providers.** Rationale: we are a Python/Django/pytest shop that self-hosts and cares about data residency; DeepEval drops into our existing test suite, ships faithfulness/hallucination/G-Eval metrics out of the box, and — critically — supports a custom judge model so we can route grading through `llm/routing` instead of OpenAI. We deliberately do **not** adopt a SaaS eval platform (Braintrust/LangSmith): it would fight our self-hosting posture and add cost for capability we can reproduce with the provider abstraction, `judge.py`, and `UsageAccumulator` we already own. `promptfoo` may be added later, narrowly, for prompt/model A-B comparison runs; it is not required for the CI gate.

## Migration Order (~11 enrichers + sourcing scripts)

The enrichers are not the whole surface: the **sourcing / extraction scripts** carry LLM prompts too and are in scope for the same treatment. The prompt-bearing ones today are `cases/services/case_scraper.py` (the research-assistant scraper prompt) and `scripts/ciaa_extraction/extract_narrative.py` (defendant extraction from the CIAA annual report); the rest of `sourcing/` (`converter.py`, `jds_client.py`, `ngm_client.py`) and the regex-only extractors (`extract_sheet.py`, `extract_annual_report.py`, `validate.py`) have no prompts and need no prompt registry — but their structured outputs are still worth field-level golden checks.

Sequence by ground-truth availability (deterministic-checkable first, since those evals are free and high-signal):

1. `enrich_missing_bigo` — clean JSON schema, deterministic ground truth (the amount). **First slice (done — see `evals/`).**
2. `enrich_tags` — controlled vocabulary, set-overlap metrics; also fixes the keyword-dict duplication.
3. `enrich_title` — deterministic court-number/headcount guards + a rubric metric for headline quality.
4. `enrich_allegations`, `enrich_timeline`, `enrich_related_entities` — grounding/faithfulness rubric metrics + structural checks.
5. `enrich_description`, `enrich_card`, `enrich_evidence`, `enrich_news_articles` — mostly rubric/faithfulness; lower deterministic coverage.
6. **Sourcing scripts:** `scripts/ciaa_extraction/extract_narrative.py` (defendant fields = strong deterministic ground truth: names, charge sections, bigo, dates) then `cases/services/case_scraper.py` (research-assistant prompt → schema + grounding checks). Register their prompts; reuse the same deterministic metrics.
7. `review/judge.py` rule prompts — already data; bring under the same registry + add the calibration harness.

## Open Questions / Risks

- **Golden-set size vs. cost.** How many annotated cases per enricher is "enough" for a stable gate? Start with ~15–25 per enricher (mix of typical + known-hard edge cases) and grow from escaped failures.
- **Judge trust for Nepali.** If calibration shows low judge-human agreement, which fields can be gated by LLM-judge at all, versus deterministic-only? This is an empirical question the Phase 3 calibration answers.
- **Prompt-version storage.** File-based modules (versioned in Git) vs YAML/TOML files vs a DB table. Recommendation: start file-based in Git (simplest, reviewable, matches `rule_defaults.py`); revisit a DB-backed registry only if non-engineers need to edit prompts in a UI.
- **CI cost/latency.** Per-PR runs must stay cheap (cheap tier + caching + a small smoke slice); the full LLM-judge set runs nightly, not per-PR.

## First Slice (proposed concrete deliverable)

`enrich_missing_bigo`, end-to-end: a `PromptSpec` for its prompt; a small versioned golden dataset drawn from approved cases; a deterministic amount-match + schema-conformance metric; a `pytest` gate via DeepEval; and a `run_eval.py` invocation that prints a premium-vs-cheap delta table. This proves the entire loop on the smallest, most deterministic surface and becomes the template the other enrichers copy.

## References

Prompt management & versioning: Braintrust — Best Prompt Versioning Tools (https://www.braintrust.dev/articles/best-prompt-versioning-tools-2025); LangWatch — What is Prompt Management (https://langwatch.ai/blog/what-is-prompt-management-and-how-to-version-control-deploy-prompts-in-productions); Agenta — Definitive Guide to Prompt Management Systems (https://agenta.ai/blog/the-definitive-guide-to-prompt-management-systems).
Eval frameworks: TECHSY — 8 LLM Eval Tools Ranked (https://techsy.io/en/blog/best-llm-evaluation-tools); Comet — LLM Evaluation Frameworks Head-to-Head (https://www.comet.com/site/blog/llm-evaluation-frameworks/); helpmetest — RAGAS vs DeepEval vs PromptFoo vs Langfuse (https://helpmetest.com/blog/llm-evaluation-frameworks/).
Golden sets & CI gating: TestQuality — LLM Regression Testing Pipeline (https://testquality.com/llm-regression-testing-pipeline/); Traceloop — Automated Prompt Regression Testing with LLM-as-a-Judge and CI/CD (https://www.traceloop.com/blog/automated-prompt-regression-testing-with-llm-as-a-judge-and-ci-cd); Medium — Evaluation-First AI Product Engineering (https://medium.com/@falvarezpinto/evaluation-first-ai-product-engineering-golden-sets-drift-monitoring-and-release-gates-for-llm-2c3bfb3f1e7b).
Structured-extraction eval: arXiv — Real-Time Trustworthiness Scoring for LLM Structured Outputs (https://arxiv.org/pdf/2603.18014); MDPI — Benchmarking LLM-as-a-Judge for 5W1H Extraction (https://www.mdpi.com/2079-9292/15/3/659); arXiv — A Review of Faithfulness Metrics (https://arxiv.org/pdf/2501.00269).
LLM-as-judge: Mervin Praison — LLM-as-a-Judge Best Practices (https://mer.vin/2025/11/llm-as-a-judge-best-practices-for-consistent-evaluation/); Weights & Biases — Exploring LLM-as-a-Judge (https://wandb.ai/site/articles/exploring-llm-as-a-judge/).
Multilingual / low-resource: arXiv — Mitigating Translationese Bias in Multilingual LLM-as-a-Judge (https://arxiv.org/html/2603.10351); arXiv — Ready to Translate, Not to Represent? (https://arxiv.org/pdf/2510.07877).
Prompt optimization: arXiv — Is It Time To Treat Prompts As Code? (DSPy) (https://arxiv.org/html/2507.03620v1); Towards Data Science — Systematic LLM Prompt Engineering Using DSPy (https://towardsdatascience.com/systematic-llm-prompt-engineering-using-dspy-optimization/).
