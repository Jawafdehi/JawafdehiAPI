"""Worker-side handler for ``case_tags_tagger`` jobs.

Runs where the LLM provider lives, not in the API process — the premium tier resolves to
a CLI provider, so the model call happens on the worker and the answer comes back over
``POST /api/jobs/<id>/result/``.

Mirrors ``case_proposals/job_handlers.py``: render the registered prompt against the
payload the server resolved, invoke, return the parsed JSON. No validation here on
purpose — the worker is the least trusted participant, so checking happens server-side in
:mod:`case_tags.write` where it cannot be skipped by pointing a different worker at the
queue.
"""

from __future__ import annotations

from typing import Any

import structlog

from llm.invoke import invoke_json
from llm.prompts import get as get_prompt

from case_tags.prompts import TAGGER_PROMPT

logger = structlog.get_logger(__name__)


def handle_case_tags_tagger(payload: dict, *, on_stage: Any) -> dict:
    """Tag one case. Returns the model's JSON for the server to validate and apply."""
    case = (payload or {}).get("case")
    if not isinstance(case, dict):
        raise ValueError("case_tags_tagger payload is missing the resolved 'case' dict.")
    vocabulary = (payload or {}).get("vocabulary")
    if not isinstance(vocabulary, list):
        raise ValueError("case_tags_tagger payload is missing 'vocabulary'.")

    spec = get_prompt(TAGGER_PROMPT)
    on_stage("prompting")
    system = spec.render_system()
    content = spec.render(
        case=case,
        vocabulary=vocabulary,
        current_tags=", ".join(case.get("tags") or []),
    )

    on_stage("invoking")
    answer = invoke_json(system, content, max_tokens=spec.max_tokens, tier=spec.tier)
    logger.info(
        "case_tags.tagger.answered",
        case_slug=case.get("slug"),
        axes=sorted(k for k in answer if isinstance(answer.get(k), list)),
    )
    return answer


HANDLERS = {"case_tags_tagger": handle_case_tags_tagger}
