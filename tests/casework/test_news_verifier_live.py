"""Does the CONFIGURED model actually clear the false-positive bar?

Every other test in this package answers the verifier with `stub_invoke_json`,
a fake. That is right for logic -- it makes the suite deterministic and free --
but it means nothing in CI has ever asked the question that decides whether
this stage is safe to run: *does the model we are about to point at production
attach articles to the wrong case?*

`news_labelled_set.py` was built to answer exactly that. It holds 10 (case,
article) pairs that genuinely match and 10 that look plausible and do not, two
of which are real production mis-binds. A `no_match` pair that reaches a bind
is a named person publicly tied to a corruption case that is not theirs.

WHY THIS IS OPT-IN RATHER THAN A CI GATE. It calls a real model, so it is
non-deterministic, costs tokens and needs provider credentials. Making it a
gate would make CI flaky and expensive. Making it *absent* is worse -- the
tier is a configuration choice, and a configuration choice with no way to check
it is a guess. So it is a marked test an operator runs deliberately:

    CASEWORK_LIVE_MODEL_EVAL=1 uv run pytest -m live_model -s
    CASEWORK_LIVE_MODEL_EVAL=1 CASEWORK_EVAL_MODEL=sonnet \
        uv run pytest -m live_model -s

RUN IT WHEN: changing a stage's tier, changing a model, or before a batch large
enough that a wrong bind would be expensive to unpick. Measured 2026-08-07 on
`haiku` at the cheap tier: 1 of 10 -- pair 12, case 080-CR-0174 against an
article about the same accused's OTHER case, returned `high` confidence. That
is the precise failure `confidence == "high"` exists to refuse.
"""
import os
from datetime import date

import pytest
from django.test import override_settings

from casework.news_search import Article, verify_batch
from tests.casework.news_labelled_set import LABELLED_PAIRS

pytestmark = [
    pytest.mark.live_model,
    pytest.mark.skipif(
        not os.environ.get("CASEWORK_LIVE_MODEL_EVAL"),
        reason="calls a real LLM; set CASEWORK_LIVE_MODEL_EVAL=1 to run"),
]


def _case_from(pair):
    """The DETAIL payload shape `verify_batch` reads, from a labelled pair."""
    source = pair["case"]
    return {
        "slug": source["slug"],
        "title": source["title"],
        "short_description": source["short_description"],
        "description": source.get("short_description") or "",
        "key_allegations": list(source["key_allegations"]),
        "court_cases": ["https://jawafdehi.org/courtcase/special/"
                        + source["court_case_no"].lower()],
        "entities": [{"display_name": n, "type": "accused"}
                     for n in source["accused"]],
        "timeline": [],
    }


def _article_from(pair):
    raw = pair["article"]
    published = raw.get("published")
    return Article(
        url=raw["url"],
        title=raw.get("title") or "",
        text=raw.get("text") or "",
        published=date.fromisoformat(published[:10]) if published else None,
        snippet=(raw.get("text") or "")[:180],
    )


def test_the_configured_model_binds_none_of_the_no_match_pairs(capsys):
    """ZERO false positives. Not a metric to optimise -- an assertion.

    False NEGATIVES are counted and printed but do not fail: missing a real
    article costs coverage, which a later run can recover. Binding the wrong
    one publishes a claim about a named person, which it cannot.
    """
    from casework.common.llm import tier_for
    from llm.invoke import invoke_json
    from llm.usage import UsageAccumulator

    # `override_settings`, NOT `casework.common.llm.bootstrap`. Bootstrap works
    # by setting env vars, and `config.settings` reads those with `os.getenv` at
    # module-import time -- which in a pytest session has already happened before
    # any test body runs. Calling it here would be a silent no-op and this test
    # would quietly measure whatever the ambient config was, which is the exact
    # class of bug it exists to catch. `routing.provider_for_tier` and
    # `model_for_tier` both read `settings` per call, so overriding works.
    provider = os.environ.get("CASEWORK_EVAL_PROVIDER") or "claude_cli"
    model = os.environ.get("CASEWORK_EVAL_MODEL") or ""
    overrides = {"REVIEW_LLM_PROVIDER_PREMIUM": provider,
                 "REVIEW_LLM_PROVIDER_CHEAP": provider}
    if model:
        overrides.update(CLAUDE_CLI_MODEL_PREMIUM=model,
                         CLAUDE_CLI_MODEL_CHEAP=model)

    usage = UsageAccumulator()
    tier = tier_for("news")
    false_positives, false_negatives = [], []

    with capsys.disabled(), override_settings(**overrides):
        print(f"\n  decision tier: {tier}   provider: {provider}   "
              f"model: {model or '(configured)'}")
        for index, pair in enumerate(LABELLED_PAIRS, 1):
            verdicts = verify_batch([_article_from(pair)], _case_from(pair),
                                    invoke_json, usage, tier=tier)
            verdict = verdicts[0][1] if verdicts else None
            bindable = bool(verdict and verdict.is_bindable)
            wanted = pair["label"] == "match"
            if bindable and not wanted:
                false_positives.append((index, pair, verdict))
            elif wanted and not bindable:
                false_negatives.append(index)
            print(f"  {index:2d}. {pair['label']:9s} -> "
                  f"{'BIND' if bindable else 'no bind':7s}"
                  f"{'   <== FALSE POSITIVE' if bindable and not wanted else ''}")
        print(f"\n  false positives: {len(false_positives)}   "
              f"false negatives: {len(false_negatives)}")

    assert not false_positives, "\n".join(
        f"pair {index} ({pair['case']['court_case_no']}) bound an article that "
        f"is not about it: confidence={v.confidence!r} "
        f"event={v.event_type!r} reason={(v.reason or '')[:160]!r}"
        for index, pair, v in false_positives)
