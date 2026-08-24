"""Prompts owned by the tags app. Registered at app-ready.

One: ``case_tags.tagger``, which reads a case plus the current vocabulary and returns the
tags for three axes. Text lives in ``case_tags/prompt_templates/case_tags/`` — see
:mod:`llm.prompts` for why prompts are files rather than f-strings.
"""

from __future__ import annotations

from llm.prompts import PromptSpec, register

#: Registered name. Dotted, per the registry's convention — NOT the job kind, which is
#: ``case_tags_tagger`` with underscores. Same work, two namespaces, worth not conflating.
TAGGER_PROMPT = "case_tags.tagger"

tagger = register(
    PromptSpec(
        name=TAGGER_PROMPT,
        version=1,
        system_template="case_tags/tagger.system.md",
        content_template="case_tags/tagger.content.md",
        # Pinned to the strong tier and it must stay pinned. This is a
        # reuse-before-you-create judgement over ~40 terms with bilingual labels, and a
        # weak model's failure mode here is not an error — it is a plausible new term
        # beside an existing one, which is the exact defect the vocabulary exists to fix.
        # `llm.routing` resolves an unknown tier to the CHEAP model WITHOUT raising, so a
        # downgrade would surface as slow vocabulary drift rather than as a failure.
        tier="premium",
        # Sized against case_proposals.intent, which measured this the hard way: through
        # the CLI provider the budget becomes CLAUDE_CODE_MAX_OUTPUT_TOKENS and caps
        # reasoning AND answer together, so a budget sized for the answer alone is spent
        # thinking and the turn ends unfinished — billing a full premium call per attempt.
        # 8000 is the figure courts.extract_verdicts settled on for a comparable
        # read-and-judge prompt.
        max_tokens=8000,
        # No `required`. It applies to render_system() as well as render() (see the
        # PromptSpec docstring), and this system prompt is static — declaring the content
        # template's variables here made rendering the system half fail on a template
        # that does not reference them. Both are plain {{ }} holes in the content
        # template, which the missing-variable sentinel catches without declaration.
    )
)
