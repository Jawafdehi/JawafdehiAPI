"""DeepEval judge wired through the in-house provider abstraction.

DeepEval defaults its LLM-as-judge metrics (GEval, faithfulness, hallucination) to OpenAI.
We don't want that: evals must run on our own infra (data residency) and the judge model
must be pinnable independent of the model being graded (to avoid self-preference bias).
``ProviderJudge`` is a ``DeepEvalBaseLLM`` that routes every judge call through
``llm.invoke`` / ``llm.routing``, so the same Bedrock/proxy/CLI tiers grade the evals.

DeepEval is an OPTIONAL dependency (not in pyproject yet). Import is guarded so the eval
package stays importable without it; install with ``poetry add --group dev deepeval`` and
the judge metrics light up. See ``evals/README.md``.
"""

from __future__ import annotations

try:
    from deepeval.models.base_model import DeepEvalBaseLLM

    DEEPEVAL_AVAILABLE = True
except Exception:  # noqa: BLE001 - deepeval is optional; absence must not break imports
    DEEPEVAL_AVAILABLE = False
    DeepEvalBaseLLM = object  # type: ignore[assignment,misc]


_JUDGE_SYSTEM = (
    "You are a meticulous, strict-but-fair evaluator for Jawafdehi. You understand "
    "Nepali and English. Follow the rubric exactly and reply only as instructed."
)


class ProviderJudge(DeepEvalBaseLLM):
    """A DeepEval judge backed by the Jawafdehi provider router.

    Args:
        tier: which routing tier grades the evals ("premium" by default — match rubric
            complexity to judge capability, and keep it independent of the graded model).
    """

    def __init__(self, tier: str = "premium"):
        if not DEEPEVAL_AVAILABLE:
            raise RuntimeError(
                "deepeval is not installed; run `poetry add --group dev deepeval`"
            )
        self.tier = tier

    def load_model(self):  # DeepEvalBaseLLM hook
        return None

    def generate(self, prompt: str, schema=None) -> str:
        # Imported here so the module imports without Django/transport configured.
        from llm.invoke import invoke_text

        return invoke_text(
            system=_JUDGE_SYSTEM,
            content=prompt,
            max_tokens=1500,
            tier=self.tier,
        )

    async def a_generate(self, prompt: str, schema=None) -> str:
        return self.generate(prompt, schema)

    def get_model_name(self) -> str:
        from llm.routing import model_for_tier

        try:
            return f"jawafdehi:{self.tier}:{model_for_tier(self.tier)}"
        except Exception:  # noqa: BLE001 - reporting must never crash
            return f"jawafdehi:{self.tier}"
