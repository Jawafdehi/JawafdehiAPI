"""Review-specific LLM judge built on the generic llm package.

The judge is generic over *rules*. Because the operator expects **hundreds of
rules**, we do NOT stuff every rule into one giant prompt. Instead each LLM
rule is graded by its own focused prompt, and every (rule x sample) invocation
is dispatched **in parallel** through a bounded thread pool. This keeps each
prompt small/accurate and makes wall-clock time scale with the pool size rather
than the rule count. Each rule is sampled `n_samples` times so the scorer can
compute a real **mean + variance (confidence)** per rule.
"""

import json
import statistics
from concurrent.futures import ThreadPoolExecutor, as_completed

from django.conf import settings

from llm.exhaustion import is_exhaustion
from llm.invoke import invoke_json


class JudgeUnavailable(RuntimeError):
    """The judge could not be reached, so some rules have no grade at all.

    Distinct from "the model replied but omitted a rule key": that is a content
    problem the per-rule retry handles, and its neutral 50 must not fail a gate.
    This one means the *call* never landed -- expired/revoked credentials, a 429
    session cap, a network fault -- and the review is therefore unscorable. It
    must reach the queue as a job failure rather than being folded into a score,
    because a partially-judged review still reads as a plausible ~70.
    """


# Failures that are NOT evidence the judge is unreachable. A dirty or truncated
# reply means the call landed and `salvage_json` still could not make an object
# of it; the rest are our own bugs (a malformed rule dict, a typo in prompt
# building). Both leave a rule ungraded, and both must keep the lenient
# degrade-to-neutral path -- failing the job over them would turn every content
# hiccup into a dead-lettered review. Anything else a provider raises for a real
# transport fault (a 429 cap wrapped in RuntimeError, an expired token, a
# timeout, a botocore ClientError) is not enumerable, so transport is the
# default and this list is the exception.
_NON_TRANSPORT_ERRORS = (
    json.JSONDecodeError,
    TypeError,
    AttributeError,
    KeyError,
    IndexError,
    NotImplementedError,
)


def _is_transport_error(exc):
    """Did the call fail to *land*, as opposed to landing badly or hitting a bug?"""
    if isinstance(exc, _NON_TRANSPORT_ERRORS):
        return False
    # Out of room is not out of reach. Both causes -- the output cap and the turn
    # cap -- abort a call the provider ACCEPTED and billed: the judge was there,
    # it just could not finish inside the budget we gave it. Calling that
    # unreachable dead-letters a whole review over rules that are merely
    # ungraded, which is what the lenient degrade-to-neutral path is for. By the
    # time one of these reaches here `_invoke_rule` has already retried the rule
    # at a larger budget, so the room really has run out.
    if is_exhaustion(exc):
        return False
    return True


def _task_rule_keys(task):
    """The rule keys a task is carrying; the narrative task carries none.

    Reachability is per rule, so a failure is only evidence about the rules whose
    grades that particular call was going to produce.
    """
    kind = task[0]
    if kind == "batch":
        return [r["key"] for r in task[2]]
    if kind == "rule":
        return [task[2]["key"]]
    return []


# Bounded parallelism for LLM calls. Bedrock throttles aggressively, so we
# keep this modest; with hundreds of rules this is the wall-clock divisor.
def _api_max_workers():
    """Bounded parallelism for API-provider calls (read at call time so
    override_settings / env changes after import take effect)."""
    return int(getattr(settings, "BEDROCK_MAX_WORKERS", 8))


def _effective_max_workers():
    """Get max workers adjusted for provider type.

    CLI providers (codex_cli, claude_cli) have tighter subprocess limits,
    so we use REVIEW_CLI_MAX_WORKERS instead of the larger BEDROCK_MAX_WORKERS.
    """
    from llm.routing import provider_for_tier

    for tier in ("premium", "cheap"):
        if provider_for_tier(tier).name in ("codex_cli", "claude_cli"):
            return int(getattr(settings, "REVIEW_CLI_MAX_WORKERS", 2))
    return _api_max_workers()


# Cap on the combined source-excerpt block handed to the judge per rule. The old
# 8000 was far too small: with many sources the relevant document (e.g. the
# press release carrying the bigo amount) was cut off, making the judge wrongly
# report it as "truncated / unverifiable". Opus 4.8's context handles a generous
# budget; raise it so figures deep in a source are actually visible.
def _source_excerpts_cap():
    return int(getattr(settings, "JUDGE_SOURCE_EXCERPTS_CAP", 120000))

# Whether to prompt-cache the shared rule-grading prefix. The same case data +
# source-excerpt block is re-sent for every (rule x sample) call; with hundreds
# of rules that block dominates input cost. Marking it with `cache_control` makes
# Bedrock bill it once per case (cache write) and ~free on every later call
# (cache read) for the cache TTL, which the back-to-back fan-out keeps warm.
def _prompt_cache_enabled():
    return bool(getattr(settings, "BEDROCK_PROMPT_CACHE", True))

# Provider names whose calls are separate subprocesses with NO cross-call prompt
# cache — each call re-bills (and re-cache-writes) the whole context. For these we
# batch many rules into one call so the expensive case context is sent once per
# batch instead of once per rule. bedrock/proxy keep the per-rule + cache path.
CLI_PROVIDERS = ("codex_cli", "claude_cli")
# Rules per batched CLI call. 1 disables batching (per-rule path; for A/B).
def _rule_batch_size():
    return int(getattr(settings, "REVIEW_RULE_BATCH_SIZE", 8))


# Output budgets for grading ONE rule, smallest first. Two rungs, because a
# single rung has no answer to a rule that cannot fit in it: on a CLI provider
# the budget becomes CLAUDE_CODE_MAX_OUTPUT_TOKENS, which caps *reasoning as well
# as the answer* (see llm/exhaustion.py), so a reasoning-heavy rule can spend the
# lot thinking and return nothing at all. The grade itself is small -- a score, a
# rationale, a few issues -- so the headroom on the second rung is for thinking,
# not for a longer verdict.
#
# The first rung stays at `invoke_json`'s own default so this changes nothing for
# a rule that already fits; only a rule that overflows pays for a second call.
def _rule_budgets():
    first = int(getattr(settings, "REVIEW_RULE_MAX_TOKENS", 900))
    retry = int(getattr(settings, "REVIEW_RULE_MAX_TOKENS_RETRY", 4000))
    return [first, retry] if retry > first else [first]

REVIEW_SYSTEM = (
    "You are a meticulous editorial reviewer for Jawafdehi.org, an open civic "
    "archive of Nepali anti-corruption cases. You grade case quality against a "
    "single explicit rule. You are strict but fair. You understand Nepali and "
    "English. You ALWAYS reply with a single valid JSON object and nothing else."
)


def _invoke_rule(content, tier, usage):
    """Grade one rule, escalating the output budget if the model runs out of room.

    Escalation is gated on :func:`llm.exhaustion.is_exhaustion` rather than on
    "did it fail", so a dead credential, a 429 or a bug in our prompt building
    still fails on the first call instead of buying a second, dearer one to fail
    in exactly the same way.
    """
    budgets = _rule_budgets()
    last = len(budgets) - 1
    for i, budget in enumerate(budgets):
        try:
            return invoke_json(
                REVIEW_SYSTEM, content, max_tokens=budget, tier=tier, usage=usage
            )
        except Exception as exc:  # noqa: BLE001 - decide by cause, then re-raise
            if i == last or not is_exhaustion(exc):
                raise


def _rule_context_block(case_summary, source_excerpts, case_type_label):
    """The case data + source excerpts shared verbatim by every rule call.

    Kept as a standalone leading block so it can be marked `cache_control` and
    billed once per case instead of once per (rule x sample) invocation.
    """
    return f"""CASE TYPE: {case_type_label or "unknown"}

CASE DATA:
{json.dumps(case_summary, ensure_ascii=False, indent=2)[:30000]}

SOURCE DOCUMENT EXCERPTS (converted to markdown):
{source_excerpts[:_source_excerpts_cap()]}"""


def _rule_instruction_block(rule):
    """The per-rule grading instruction (varies per call; never cached)."""
    block = f"### Rule `{rule['key']}` — {rule['title']}\n{rule.get('description','')}"
    if rule.get("good_examples"):
        block += f"\nGOOD: {rule['good_examples']}"
    if rule.get("bad_examples"):
        block += f"\nBAD: {rule['bad_examples']}"
    return f"""Grade the Jawafdehi case above (CASE DATA + SOURCE DOCUMENT EXCERPTS)
against the ONE rule below. Reply with JSON only.

RULE TO GRADE:
{block}

Score the rule 0-100 with a short rationale, the concrete issues you found, and
a list of concrete, actionable SUGGESTIONS the caseworker can apply to improve
the case against THIS rule (each suggestion an imperative one-liner, e.g.
"Add the special-court verdict date to the timeline"). If the rule is fully
satisfied, return an empty suggestions list. Judge ONLY against this rule.
Reply EXACTLY in this JSON shape:
{{"score": <int 0-100>, "rationale": "<str>", "issues": ["<str>"], "suggestions": ["<str>"]}}"""


def _build_single_rule_content(context_block, rule):
    """User-message content for grading one rule against the shared context.

    When prompt caching is on, return two content blocks — the shared context
    (marked `cache_control`) followed by the per-rule instruction — so Bedrock
    reuses the cached prefix across every rule. Otherwise fall back to a single
    concatenated string (identical text, no cache markers).
    """
    instruction = _rule_instruction_block(rule)
    if _prompt_cache_enabled():
        return [
            {
                "type": "text",
                "text": context_block,
                "cache_control": {"type": "ephemeral"},
            },
            {"type": "text", "text": instruction},
        ]
    return f"{context_block}\n\n{instruction}"


def _build_batch_instruction(rules):
    """Grading instruction for a BATCH of rules (one call grades all of them)."""
    blocks = []
    for rule in rules:
        b = f"### Rule `{rule['key']}` — {rule['title']}\n{rule.get('description', '')}"
        if rule.get("good_examples"):
            b += f"\nGOOD: {rule['good_examples']}"
        if rule.get("bad_examples"):
            b += f"\nBAD: {rule['bad_examples']}"
        blocks.append(b)
    rules_block = "\n\n".join(blocks)
    return f"""Grade the Jawafdehi case above (CASE DATA + SOURCE DOCUMENT EXCERPTS)
against EACH of the {len(rules)} rules below. Reply with JSON only.

RULES TO GRADE:
{rules_block}

For EACH rule: score 0-100 with a short rationale, the concrete issues you found,
and a list of concrete, actionable SUGGESTIONS (imperative one-liners; empty list
if the rule is fully satisfied). Judge each rule INDEPENDENTLY against ONLY that
rule. Return exactly one entry per rule key. Reply EXACTLY in this JSON shape:
{{"rules": {{"<rule_key>": {{"score": <int 0-100>, "rationale": "<str>", "issues": ["<str>"], "suggestions": ["<str>"]}}}}}}"""


def _build_batch_content(context_block, rules):
    """User-message content grading several rules against the shared context."""
    instruction = _build_batch_instruction(rules)
    if _prompt_cache_enabled():
        return [
            {
                "type": "text",
                "text": context_block,
                "cache_control": {"type": "ephemeral"},
            },
            {"type": "text", "text": instruction},
        ]
    return f"{context_block}\n\n{instruction}"


def _chunk(seq, size):
    """Yield consecutive chunks of `seq` of length `size`."""
    for i in range(0, len(seq), max(1, size)):
        yield seq[i : i + size]


def _build_narrative_prompt(case_summary, source_excerpts, case_type_label):
    return f"""Give a 2-3 sentence overall editorial assessment (narrative) of this
Jawafdehi case quality. Reply with JSON only.

CASE TYPE: {case_type_label or "unknown"}

CASE DATA:
{json.dumps(case_summary, ensure_ascii=False, indent=2)[:6000]}

SOURCE DOCUMENT EXCERPTS:
{source_excerpts[:5000]}

Reply EXACTLY: {{"narrative": "<str>"}}"""


def _build_source_analysis_prompt(case_summary, source, case_type_label):
    md = source.get("markdown") or ""
    return f"""A source document for a Jawafdehi case has been converted to markdown
(via likhit). Summarise it, then analyse how THIS source contributes to the
overall case. Reply with JSON only.

CASE TYPE: {case_type_label or "unknown"}

CASE (for context — what the source should connect to):
{json.dumps(case_summary, ensure_ascii=False, indent=2)[:5000]}

SOURCE: {source.get('title','(untitled)')}  ({source.get('source_type','')})
CONVERTED MARKDOWN:
{md[:9000]}

Analyse ONLY from the source text above; do not invent facts. For each
contribution field, return concrete points grounded in this source (empty list
if the source contributes nothing there). Keep the summary under 80 words and
each contribution list to at most 6 short items. Reply EXACTLY in this JSON shape:
{{"summary": "<2-4 sentence neutral summary of this source>",
  "contributes_to": {{
    "description": ["<how this source supports/expands the case description>"],
    "timeline": ["<dated events this source establishes, e.g. 'YYYY-MM-DD: ...'>"],
    "key_allegations": ["<allegations this source substantiates>"],
    "entities": ["<parties/institutions this source names>"]
  }},
  "relevance": "<high|medium|low>",
  "notes": "<gaps, OCR/quality issues, or why relevance is low; '' if none>"}}"""


def _tier_for_rule(rule):
    """Hard GATE rules (a low score can REJECT the case) get the premium tier;
    routine rules get the cheaper one. Mirrors the narrative, which is low-stakes
    prose and also runs on the cheap tier."""
    return "premium" if rule.get("is_gate") else "cheap"


def analyze_source(case_summary, source, case_type_label, usage=None):
    """Summarise one converted source + analyse its contribution to the case.

    Returns the parsed JSON dict (see prompt), or an {"error": ...} dict on
    failure so a single bad source never aborts the whole analysis stage.
    """
    md = (source.get("markdown") or "").strip()
    if not md:
        return {
            "summary": "",
            "contributes_to": {
                "description": [],
                "timeline": [],
                "key_allegations": [],
                "entities": [],
            },
            "relevance": "low",
            "notes": "No converted text available for this source.",
        }
    try:
        prompt = _build_source_analysis_prompt(case_summary, source, case_type_label)
        return invoke_json(
            REVIEW_SYSTEM, prompt, max_tokens=2000, tier="cheap", usage=usage
        )
    except Exception as e:  # noqa: BLE001
        return {
            "error": str(e),
            "summary": "",
            "relevance": "low",
            "contributes_to": {
                "description": [],
                "timeline": [],
                "key_allegations": [],
                "entities": [],
            },
            "notes": f"Source analysis failed: {e}",
        }


def analyze_sources(case_summary, converted_sources, case_type_label, usage=None):
    """Analyse every converted source in parallel. Returns a list aligned by
    index with `converted_sources` (each item is the analyze_source dict)."""
    if not converted_sources:
        return []
    results = [None] * len(converted_sources)
    workers = min(_effective_max_workers(), len(converted_sources))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {
            ex.submit(analyze_source, case_summary, s, case_type_label, usage): i
            for i, s in enumerate(converted_sources)
        }
        for fut in as_completed(futs):
            i = futs[fut]
            try:
                results[i] = fut.result()
            except Exception as e:  # noqa: BLE001
                results[i] = {
                    "error": str(e),
                    "summary": "",
                    "contributes_to": {},
                    "relevance": "low",
                    "notes": f"analysis crashed: {e}",
                }
    return results


def judge_rules(
    case_summary, source_excerpts, case_type_label, llm_rules, n_samples=3, usage=None
):
    """Grade every LLM rule against the case; return per-rule scores.

    Routing is per tier (gate->premium, routine->cheap):
      * bedrock/proxy: the per-rule x n_samples fan-out (prompt-cache friendly),
        yielding a real mean+variance per rule.
      * CLI harnesses (codex_cli/claude_cli) with RULE_BATCH_SIZE>1: rules are
        graded in BATCHES (one call grades up to RULE_BATCH_SIZE rules) in a
        SINGLE pass, so the case context is sent once per batch instead of once
        per rule (CLI calls can't reuse a prompt cache). Single pass => std 0.

    Returns dict:
      {rule_key: {"mean", "variance", "std", "samples", "rationale", "issues",
                  "suggestions"}, "_narrative": str, "_n_samples": int}
    Raises only if EVERY invocation fails (so the caller can fall back).
    """
    if not llm_rules:
        return {"_narrative": "", "_n_samples": 0}

    from llm.routing import provider_for_tier

    n = max(1, int(n_samples))
    keys = [r["key"] for r in llm_rules]

    # Built once; identical for every rule (cached on bedrock, re-sent on CLI).
    context_block = _rule_context_block(case_summary, source_excerpts, case_type_label)

    # Group rules by tier, then per tier pick batched (CLI) vs per-rule path.
    tier_rules = {"premium": [], "cheap": []}
    for r in llm_rules:
        tier_rules[_tier_for_rule(r)].append(r)

    tasks = []  # each task leads with a kind discriminator
    batched_rules = []  # rules graded via a batch call (retried per-rule if omitted)
    for tier, rules in tier_rules.items():
        if not rules:
            continue
        batch_size = _rule_batch_size()
        batched = batch_size > 1 and provider_for_tier(tier).name in CLI_PROVIDERS
        if batched:
            batched_rules.extend(rules)
            for chunk in _chunk(rules, batch_size):
                tasks.append(("batch", tier, chunk))
        else:
            for r in rules:
                for _ in range(n):
                    tasks.append(("rule", tier, r))
    tasks.append(("narrative", "cheap", None))

    samples = {k: [] for k in keys}
    last_rationale = {k: "" for k in keys}
    last_issues = {k: [] for k in keys}
    last_suggestions = {k: [] for k in keys}
    narrative = ""
    errors = []
    # Rule key -> the transport error that left it unreached on its LATEST
    # attempt. Kept per rule rather than as one shared list because a failure
    # says nothing about a rule that some other call graded: a dead narrative
    # call, or a bug in one batch, must not mark every ungraded rule unreachable.
    unreachable = {}

    def _apply(key, parsed):
        if key not in samples or not isinstance(parsed, dict):
            return
        sc = parsed.get("score")
        if isinstance(sc, str):
            try:
                sc = float(sc)  # LLMs sometimes return "score": "85"
            except ValueError:
                sc = None
        if isinstance(sc, (int, float)):
            samples[key].append(int(round(sc)))
        if parsed.get("rationale"):
            last_rationale[key] = parsed["rationale"]
        if parsed.get("issues"):
            last_issues[key] = parsed["issues"]
        if parsed.get("suggestions"):
            last_suggestions[key] = parsed["suggestions"]

    def _run(task):
        kind = task[0]
        if kind == "narrative":
            parsed = invoke_json(
                REVIEW_SYSTEM,
                _build_narrative_prompt(case_summary, source_excerpts, case_type_label),
                max_tokens=300,
                tier="cheap",
                usage=usage,
            )
            return ("narrative", parsed)
        if kind == "batch":
            _, tier, chunk = task
            content = _build_batch_content(context_block, chunk)
            parsed = invoke_json(
                REVIEW_SYSTEM,
                content,
                max_tokens=400 * len(chunk),
                tier=tier,
                usage=usage,
            )
            return ("batch", parsed)
        _, tier, rule = task
        content = _build_single_rule_content(context_block, rule)
        parsed = _invoke_rule(content, tier, usage)
        return ("rule", rule["key"], parsed)

    def _drain(pool_tasks):
        """Run a round of tasks, then record which rules this round never reached.

        A rule counts as unreached only if EVERY call carrying it in this round
        failed on transport — one landed reply is enough to say the judge was
        there, whatever the reply contained. The verdict is per round, because
        the per-rule retry below is a fresh attempt: it can rescue a rule the
        batch failed to reach, and it can equally be the call that dies when the
        quota runs out part-way through a review.
        """
        nonlocal narrative
        # rule key -> [calls carrying it, calls that failed on transport, first error]
        covered = {}
        for t in pool_tasks:
            for k in _task_rule_keys(t):
                covered.setdefault(k, [0, 0, ""])[0] += 1
        with ThreadPoolExecutor(max_workers=_effective_max_workers()) as pool:
            futures = {pool.submit(_run, t): t for t in pool_tasks}
            for fut in as_completed(futures):
                try:
                    res = fut.result()
                except Exception as e:  # noqa: BLE001 - collect, decide later
                    errors.append(str(e))
                    if _is_transport_error(e):
                        for k in _task_rule_keys(futures[fut]):
                            covered[k][1] += 1
                            covered[k][2] = covered[k][2] or str(e)
                    continue
                kind = res[0]
                if kind == "narrative":
                    narrative = res[1].get("narrative", narrative) or narrative
                elif kind == "batch":
                    rules_map = (res[1] or {}).get("rules") or {}
                    for k, rr in rules_map.items():
                        _apply(k, rr)
                else:  # per-rule
                    _apply(res[1], res[2])
        for k, (n_calls, n_dead, err) in covered.items():
            if n_dead == n_calls:
                unreachable[k] = err
            else:
                unreachable.pop(k, None)

    _drain(tasks)

    # A batch reply is not guaranteed to cover every requested rule: the model
    # can omit keys, and a truncated reply salvages to only the leading rules.
    # Without this retry an omitted rule silently scores a neutral default —
    # for a GATE rule that spuriously REJECTS the case. Re-grade the gaps with
    # isolated per-rule calls (one sample each) before giving up on them.
    omitted = [r for r in batched_rules if not samples[r["key"]]]
    if omitted:
        _drain([("rule", _tier_for_rule(r), r) for r in omitted])

    # Partial reachability is the dangerous case: the quota/credential can die
    # PART-WAY through a review, so the early rules carry real grades and the
    # rest silently take the neutral 50. That review is not a low score, it is
    # an unfinished one -- and it still lands in the 68-71 band that looks like
    # a genuine near-miss. Total failure is just this with every rule in it.
    #
    # Only a rule whose OWN last call never landed counts. An ungraded rule with
    # a landed call behind it is the model omitting a key, which the per-rule
    # retry above already handled and which must stay non-fatal.
    dead = [k for k in keys if not samples[k] and k in unreachable]
    if dead:
        raise JudgeUnavailable(
            f"Judge unreachable for {len(dead)}/{len(keys)} rule(s) "
            f"({', '.join(dead)}) after {len(errors)} failed call(s): "
            f"{unreachable[dead[0]]}"
        )

    # Nothing came back, but the judge was reachable throughout -- unparseable
    # replies, or a bug of ours. Keep the old lenient handling: the scorer
    # catches this and records it as `judge_error` over neutral scores, because
    # dead-lettering a review over a content hiccup is the wrong trade.
    if not any(samples[k] for k in keys) and not narrative:
        raise RuntimeError(
            f"All {len(tasks)} judge calls failed: {errors[0] if errors else 'unknown'}"
        )

    out = {"_narrative": narrative, "_n_samples": n}
    for k in keys:
        # No fabricated sample for ungraded rules: mean stays a neutral 50 for
        # display, but samples=[] lets the scorer tell "the judge said 50" from
        # "the judge never answered" (which must not fail a gate).
        vals = samples[k]
        mean = statistics.fmean(vals) if vals else 50.0
        var = statistics.pvariance(vals) if len(vals) > 1 else 0.0
        out[k] = {
            "mean": round(mean, 1),
            "variance": round(var, 1),
            "std": round(var**0.5, 1),
            "samples": vals,
            "rationale": last_rationale[k],
            "issues": last_issues[k],
            "suggestions": last_suggestions[k],
        }
    return out
