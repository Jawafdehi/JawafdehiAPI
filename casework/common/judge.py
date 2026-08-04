"""The cheap-model adequacy gate: is this field value real, or a placeholder?

Ported VERBATIM from the deleted `casework/common.py` (donor commit `0321a85`,
`judge_description_adequacy` / `_parse_judge_verdict` / the `_JUDGE_*`
constants).

WHY A MODEL AND NOT A REGEX. The thing being detected is a template stub, and
the stubs on real data are not blank -- 2,666 of 2,918 DRAFT cases carry a
`short_description` reading `अख्तियार दुरुपयोग अनुसन्धान आयोगले विशेष अदालतमा
दायर गरेको मुद्दा 076-CR-0182, प्रतिवादी: बिनोद कुमार भूजेल समेत ५।`. That is
120-odd characters of grammatical Nepali naming a real case and a real
defendant. An emptiness test calls it done; a length test calls it done; a
keyword blocklist catches this generation of stub and not the next one. What
actually distinguishes it is that it would fit any case, and that is a
judgement.

IT FAILS TOWARD REGENERATING. An unparseable verdict, a failed call, or text
under `_JUDGE_MIN_CHARS` all return `adequate=False`. The asymmetry is
deliberate: a needless regeneration costs one cheap call, while silently
keeping a placeholder ships template text to the public case list, where it is
indistinguishable from real editorial output.
"""

import logging

from casework.common.parse import parse_object_response

log = logging.getLogger("casework.judge")

# Below this length a text can't carry a real description -- judged a placeholder
# without spending an LLM call.
_JUDGE_MIN_CHARS = 15
_JUDGE_TEXT_BUDGET = 2000

#: Output budget for the verdict. 2000, NOT the donor's 300
#: (`0321a85:casework/common.py:1016`) -- DEVIATION, measured on the 2026-08-04
#: local smoke run. Through the `claude_cli` provider every judge call died with
#: `API Error: Claude's response exceeded the 300 output token maximum`, and
#: because this function fails toward regenerating, a systematically failing
#: judge does not fail loudly: it silently reports every value inadequate. The
#: visible symptom was `enrich_card` rewriting `short_description` on EVERY run
#: -- no idempotency, and one wasted generation call per case per run. The
#: verdict JSON itself is tiny; the budget has to cover the framing the provider
#: wraps around a reply, which measured ~2,250 output tokens even for a
#: 250-character answer.
_JUDGE_MAX_TOKENS = 2000

_JUDGE_SYSTEM_PROMPT = """\
You are a strict data-quality reviewer for Jawafdehi, a civic archive of Nepal's \
anti-corruption cases. You judge whether a given TEXT is an ADEQUATE value for the \
named field, or merely a PLACEHOLDER / STUB that should be regenerated.

INADEQUATE (adequate=false) when the text is, for example:
- empty, whitespace, or a placeholder ("description here", "TODO", "N/A", "-",
  "News Source", "खाली", "विवरण यहाँ");
- a bare restatement of the title / case number / file name with no substance;
- auto-generated filler or a generic boilerplate line that fits any case;
- so vague or truncated that it does not inform a reader.

ADEQUATE (adequate=true) when the text conveys real, specific, substantive
information appropriate to the field (names, amounts, dates, what the document is
or what it shows), even if imperfect.

Reply with ONLY a JSON object, no prose:
{"adequate": true, "reason": "<short reason>"}
"""


def _parse_judge_verdict(response_text):
    """(adequate, reason) from the judge reply, or None when no object carrying a
    BOOLEAN `adequate` is found.

    The bool check is a `predicate`, not a post-hoc test, so a near-miss object
    (`{"adequate": "yes"}`) is rejected and the scan continues to the next `{`
    -- donor behaviour, and the difference between reading a stray preamble and
    reading the verdict.
    """
    obj = parse_object_response(
        response_text, predicate=lambda o: isinstance(o.get("adequate"), bool))
    if obj is None:
        return None
    reason = obj.get("reason")
    reason = reason.strip() if isinstance(reason, str) else ""
    return obj["adequate"], reason or "(no reason given)"


def judge_description_adequacy(
    text, *, kind, invoke_text, usage=None, context="", tier="cheap"
):
    """Judge whether ``text`` is an adequate value for a ``kind`` field.

    Returns ``(adequate, reason)``. Blank or sub-``_JUDGE_MIN_CHARS`` text is
    judged inadequate WITHOUT an LLM call. Otherwise the cheap tier decides.

    ``invoke_text`` is the ``llm.invoke.invoke_text`` callable, passed in after
    bootstrap -- `casework.common` never imports `llm` itself, so that a module
    imported at test-collection time cannot drag Django settings in with it.
    """
    stripped = (text or "").strip()
    if len(stripped) < _JUDGE_MIN_CHARS:
        return False, f"blank or too short (<{_JUDGE_MIN_CHARS} chars)"

    user_prompt = (
        f"FIELD: {kind}\n"
        + (f"CONTEXT: {context}\n" if context else "")
        + f'\nTEXT TO JUDGE:\n"""\n{stripped[:_JUDGE_TEXT_BUDGET]}\n"""\n\n'
        "Return ONLY the JSON object."
    )
    try:
        response_text = invoke_text(
            system=_JUDGE_SYSTEM_PROMPT,
            content=user_prompt,
            max_tokens=_JUDGE_MAX_TOKENS,
            tier=tier,
            usage=usage,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("adequacy judge call failed (%s): %s", kind, exc)
        return False, f"judge call failed: {exc}"

    verdict = _parse_judge_verdict(response_text)
    if verdict is None:
        log.warning("adequacy judge returned an unparseable verdict for %s", kind)
        return False, "judge response unparseable; treating as inadequate"
    return verdict
