# SPDX-License-Identifier: Hippocratic-3.0
"""Recognising "the model ran out of room" across providers.

This lives here rather than beside one caller because the same failure has now
been diagnosed twice from scratch — once in ``courts`` (the verdict extractor,
API#407) and once in ``case_proposals`` (the intent job, which shipped a 1200
budget that could not finish a single answer). Both times the symptom was an
error that does not mention tokens at all.

**How exhaustion presents on the CLI provider.** ``claude -p`` has no output-cap
flag, so ``llm.providers.cli`` enforces the caller's budget through
``CLAUDE_CODE_MAX_OUTPUT_TOKENS``. That variable caps *everything the model
emits, reasoning included* — it is not the API's ``max_tokens``, which bounds
the response and leaves thinking to its own budget. A model that spends the
allowance thinking simply ends its turn unfinished; the CLI wants another turn to
continue; ``--max-turns 1`` denies it; and the run aborts as
``error_max_turns`` — "Reached maximum number of turns (1)".

So the turn limit is the messenger and the token budget is the cause. Raising
``--max-turns`` would not help and would let the model ramble at cost. The fix is
always a bigger budget.

That distinction is the part worth keeping in one place: a budget sized as though
it only had to cover the answer is a budget that cannot finish one.
"""

from __future__ import annotations

#: Substrings that mean "out of room", matched case-insensitively against the
#: exception text. Deliberately narrow: a convert failure, a missing document, an
#: auth 403 or a malformed response must NOT be retried at a larger budget —
#: that spends several times as much to fail the same way.
EXHAUSTION_MARKERS = ("error_max_turns", "maximum number of turns", "max_tokens")


def is_exhaustion(exc: BaseException | str) -> bool:
    """True if ``exc`` looks like the model ran out of room, not out of luck.

    Args:
        exc: The raised exception, or a message already extracted from one.

    Returns:
        Whether a retry at a larger token budget is worth paying for.
    """
    message = str(exc).lower()
    return any(marker.lower() in message for marker in EXHAUSTION_MARKERS)
