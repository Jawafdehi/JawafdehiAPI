# LLM eval layer

Model-independent evaluation for the Jawafdehi LLM pipelines. This is the first vertical slice (the BIGO enricher) of the plan in `../docs/llm-prompt-centralization-and-evals.md`. It is the template the other enrichers — and the sourcing scripts — copy.

## What's here

- `../llm/prompts/` — the prompt registry. Each prompt is a `PromptSpec` (key, immutable version, system + user templates, tier, output schema, golden examples). `enrich_missing_bigo.py` is the first entry; it is a *view* over the live enricher constants (single source of truth, drift-guarded), not a copy.
- `metrics/deterministic.py` — cheap, language-agnostic field-level checks (exact bigo match + JSON-schema conformance). Reuses the production `_coerce_bigo_int`, so there is no second copy of that logic. Runs offline.
- `judge_model.py` — a DeepEval `DeepEvalBaseLLM` that routes the judge through our own provider router (`llm/routing`) instead of OpenAI, so evals stay on our infra and the judge model is pinned independent of the graded model. DeepEval is optional; the import is guarded.
- `calibrate_judge.py` — measures how well the LLM case-review judge agrees with human dispositions (Cohen's kappa + confusion matrix) on real cases. This is the gate-vs-advisory decision for the Nepali judge.
- `datasets/` — real golden data (see "Real data" below).
- `test_*.py` — the CI gate (deterministic + schema + drift guard + the kappa math; DeepEval test skips if not installed).
- `run_eval.py` — live, read-only, end-to-end eval and model-migration comparison.

## Run it

Offline (no Django DB, no network, no LLM, no credentials — these are what CI runs):

    poetry run python -m evals.metrics.deterministic          # real CIAA amount strings -> bigo, 7/7
    poetry run python -m evals.calibrate_judge --demo         # kappa on the real label set (illustrative machine column)
    poetry run pytest evals/test_enrich_missing_bigo_eval.py evals/test_calibrate_judge.py

Live (read-only — never PATCHes; NOT run in CI). Defaults to the `claude_cli` provider (`claude -p`, subscription auth) — the same one the enrichers/pollers use — and needs the `bigo-enrichment` extras (`poetry install -E bigo-enrichment`) for source conversion. Reads cases over the API, so set a token (a systemwide read-only DRF token is enough):

    export JAWAFDEHI_API_TOKEN=...        # read-only is sufficient
    export JAWAFDEHI_API_BASE_URL=https://portal.jawafdehi.org
    poetry run python -m evals.run_eval --limit 5                  # accuracy on real cases via claude -p
    poetry run python -m evals.run_eval --compare                 # premium vs cheap migration verdict
    poetry run python -m evals.calibrate_judge --run --limit 5    # real judge-vs-human agreement (each case = a full review)

Verified working (`claude-opus-4-8`): `run_eval` extracts the correct bigo for the real cases; `calibrate_judge --run` ran the full review pipeline (likhit converts the Nepali PDFs) over all 7 cases and produced `n=7, accuracy=0.714, kappa=0.462` (saved to `datasets/judge_calibration/last_run.json`). That kappa is below the 0.6 gate threshold → on these labels the judge is ADVISORY, not a hard gate — which is the whole point of measuring it. The judge agrees on all four clear PASS cases and the clear REJECT, but over-rejects the two borderline cases (081-CR-0044 published, 081-CR-0136 in-review). Caveat: the labels are the publication-state PROXY, so those disagreements may mean the judge is over-strict OR genuinely catching issues; the definitive kappa needs the real 51 priority-review verdicts (swap them into `labels.json`, unchanged harness).

### Converter OOM guard (a real bug this surfaced)

081-CR-0022 has a 9.3 MB image-heavy procurement-bid PDF (`source:073dccea`) that is the only source on the case with no pre-converted MARKDOWN. The review converter runs pdfminer on it live and it OOM-SIGKILLs the whole review process (rc 137, ~9 min) — uncatchable in-process, even with 39 GB free. **The production review poller runs the same converter, so submitting this case (or any with a large RAW-only PDF) to the judge would OOM-kill the poller.** The harness now defends with `--skip-oversized-mb` (default 8): it drops un-converted PDFs above that size (loudly logged, never silent) before the review — faithful, because prod has no markdown for that source either. With the guard, 081-CR-0022 reviews cleanly (→ PASS). The real fix belongs in `sourcing/converter.py`: convert PDFs in a subprocess bounded by `RLIMIT_AS` + a wall-clock timeout (so an OOM/timeout becomes a caught per-source failure, not a dead poller), and/or pre-skip by size/page-count. That is a production change, pending approval.

To light up the DeepEval judge metrics: `poetry add --group dev deepeval` (the harness already routes it through our providers).

## Real data

Every golden value is drawn from a real Jawafdehi case, with the cited court case number, and was cross-checked against the human-approved `bigo` on the published case. The amount-coercion set exercises the genuine CIAA formatting edges seen in production: danda `।` paisa (081-CR-0116, 081-CR-0136), dot `.` paisa (081-CR-0044), slash `/` paisa, arba-scale figures (081-CR-0095), and the "paisa digits must not merge into rupees" trap. The judge-calibration labels use each case's real publication state as a documented PROXY for the human disposition (PUBLISHED→PASS, IN_REVIEW→REVISE, DRAFT→REJECT); swap in the real reviewer dispositions (e.g. the 51 priority-review verdicts) and nothing else changes.

## No prod changes

This slice is entirely additive. It does not edit any production file: the enricher (`casework/enrich_missing_bigo.py`), the transport (`llm/invoke.py`), and the review pipeline are untouched. The registry reads the enricher's prompt constants; the `invoke_prompt` wrapper lives in `llm/prompts/runner.py` rather than in `llm/invoke.py`. The Phase-1 migration (separate change) inverts that — the enricher imports its prompt from the registry, and `invoke_prompt` folds into `llm/invoke.py`.

## Adding the next pipeline

1. Add a `PromptSpec` module under `llm/prompts/` and register its key (generalise from `enrich_missing_bigo.py`).
2. Capture real golden cases under `datasets/<pipeline>/` from already-approved data.
3. Write deterministic metrics first (free, language-agnostic — especially important for Nepali); add a DeepEval rubric metric only for genuinely open-ended fields.
4. Add a `test_*_eval.py` gate and wire it into the CI job that blocks on per-cohort regression.

The same treatment applies to the **sourcing scripts** (`sourcing/`, `scripts/ciaa_extraction/`, `cases/services/case_scraper.py`): centralise their prompts into the registry and add field-level golden checks where they extract structured data. They are explicitly in scope in the RFC migration order.
