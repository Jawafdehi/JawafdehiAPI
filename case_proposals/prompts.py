# SPDX-License-Identifier: Hippocratic-3.0
"""Prompts owned by the proposals app. Registered at app-ready.

One so far: ``case_proposal.intent``, which turns an observed signal plus a case
snapshot into a drafted change intent. The text lives in
``case_proposals/prompt_templates/case_proposal/`` — see :mod:`llm.prompts` for
why prompts are files rather than f-strings.
"""

from __future__ import annotations

from llm.prompts import PromptSpec, register

#: Name under which the spec is registered. Dotted, per the registry's
#: convention — note this is NOT the job kind, which is ``case_proposal_intent``
#: (job kinds use underscores; see case_proposals.job_kind). They name the same
#: piece of work from two sides and it is worth not conflating them.
INTENT_PROMPT = "case_proposal.intent"

intent = register(
    PromptSpec(
        name=INTENT_PROMPT,
        version=1,
        system_template="case_proposal/intent.system.md",
        content_template="case_proposal/intent.content.md",
        # Pinned to the strong tier and it must stay pinned. Weak models do not
        # reliably emit the tagged-union shape this expects, and `llm.routing`
        # resolves anything it does not recognise to the CHEAP model without
        # raising — so a downgrade here would surface as a slow rise in
        # unparseable results, not as an error.
        tier="premium",
        # Sized for the model's REASONING, not for the answer. A drafted intent is
        # small, and this used to say so — "the budget is for the rationale and for
        # salvageable overrun, not for the model to think out loud", at 1200 tokens.
        # That reasons about the API's `max_tokens`, which bounds the response and
        # leaves thinking its own allowance. The tier this is pinned to reaches the
        # model through the CLI provider, where the budget becomes
        # CLAUDE_CODE_MAX_OUTPUT_TOKENS and caps reasoning and answer together.
        #
        # So 1200 was not a tight budget, it was an impossible one: the first
        # production invocation spent it thinking, ended its turn unfinished, and
        # aborted as `error_max_turns` — having billed a full premium call. Every
        # attempt failed the same way, twice per job before it went dead.
        #
        # 8000 is not a guess. `courts.extract_verdicts` measured exactly this on a
        # comparable read-and-judge prompt: 1200 failed 3 of 24 eval cases outright
        # after spending ~4800 output tokens, and 8000 is the figure that run
        # settled on. Overrun beyond it escalates once — see
        # `case_proposals.job_handlers.ESCALATED_MAX_TOKENS`.
        max_tokens=8000,
        # `language` is read only inside an {% if %}, which the missing-variable
        # sentinel cannot see. Without declaring it, omitting the key silently
        # produces an English prompt for a Nepali case.
        required=("language",),
    )
)
