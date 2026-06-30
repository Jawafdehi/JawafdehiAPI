"""Model-independent eval layer for the Jawafdehi LLM pipelines.

See ``evals/README.md`` and ``docs/llm-prompt-centralization-and-evals.md``. The
deterministic metrics + calibration harness run offline (no Django, no network, no LLM);
the live model-comparison (``run_eval.py``) and judge calibration (``calibrate_judge.py
--run``) call the in-house providers read-only and are not run in CI.
"""
