# SPDX-License-Identifier: Hippocratic-3.0
"""Recognising "the model could not finish" across providers.

This lives here rather than beside one caller because the same failure has been
diagnosed from scratch twice — in ``courts`` (the verdict extractor, API#407) and
in ``case_proposals`` (the intent job, API#411). Both times the symptom was an
error that does not mention tokens at all, and both times it was read as an
exhausted token budget.

**``error_max_turns`` has two causes, and they want opposite fixes.**

*Out of turns.* An assistant turn that runs long asks to continue. If the turn cap
denies it, the whole call aborts as ``error_max_turns`` — "Reached maximum number
of turns (N)" — returning nothing, having billed for the work already done. The
remedy is a higher cap, and a bigger token budget does nothing at all.

*Out of output tokens.* ``claude -p`` has no output-cap flag, so
``llm.providers.cli`` enforces the caller's budget through
``CLAUDE_CODE_MAX_OUTPUT_TOKENS``, which caps *everything the model emits,
reasoning included* — unlike the API's ``max_tokens``, which bounds the response
and leaves thinking its own allowance. A model that spends the allowance thinking
also ends its turn unfinished, and surfaces the same way. The remedy here is a
bigger budget, and a higher turn cap does nothing.

**An earlier version of this docstring asserted the second cause was the only
one**, and said in terms that raising ``--max-turns`` "would not help". That was
wrong, and it was load-bearing: it is why the intent job's budget was raised twice
and still failed. Measured on ``case_proposal.intent`` (2026-08-03, identical
payload and budget, arms interleaved) the call succeeded 3/5 at one turn and 5/5
at three, while raising the budget from 2000 to 8000 to 32000 changed nothing.
``llm/providers/cli.py`` now defaults the cap to ``CLAUDE_CLI_MAX_TURNS`` (3).

**So the two are not distinguishable from the error text**, and this module does
not pretend otherwise: :func:`is_exhaustion` answers only "is a retry at a larger
budget worth paying for?" With the turn cap raised, a surviving ``error_max_turns``
is more likely to be a genuine budget problem, which is what makes that retry a
reasonable fallback rather than a guess.

Telling them apart would need ``num_turns`` from the CLI's result envelope, and
**no caller can currently reach it**: ``ClaudeCliProvider.invoke_text`` returns
``data["result"]`` as stripped text and discards the rest. Distinguishing the two
properly therefore means exposing that metadata first — worth doing if this
ambiguity bites again, but it is not available to reason about today.
"""

from __future__ import annotations

import re

#: Plain substrings that mean "out of room", matched case-insensitively.
#: Deliberately narrow: a convert failure, a missing document, an auth 403 or a
#: malformed response must NOT be retried at a larger budget — that spends
#: several times as much to fail the same way.
EXHAUSTION_MARKERS = ("error_max_turns", "maximum number of turns")

#: The API side of the same condition, where the provider reports it properly as
#: a stop reason. Matched as a PATTERN rather than as the bare substring
#: "max_tokens", which the version this was lifted from used.
#:
#: That bare form is too generous in a way that inverts the whole point: the
#: string "max_tokens" also appears in *configuration* errors — "invalid
#: max_tokens value", "max_tokens: field required" — which are bugs, not
#: exhaustion. Treating one as exhaustion buys an escalated call at several times
#: the price and then fails identically, which is exactly what this module exists
#: to prevent. Requiring it to be attached to a stop reason keeps the meaning.
_STOP_REASON_MAX_TOKENS = re.compile(r"""stop[_ ]?reason["']?\s*[:=]\s*["']?max_tokens""", re.I)


def is_exhaustion(exc: BaseException | str) -> bool:
    """True if ``exc`` looks like the model ran out of room, not out of luck.

    Args:
        exc: The raised exception, or a message already extracted from one.

    Returns:
        Whether a retry at a larger token budget is worth paying for.
    """
    message = str(exc).lower()
    if any(marker.lower() in message for marker in EXHAUSTION_MARKERS):
        return True
    return bool(_STOP_REASON_MAX_TOKENS.search(message))
