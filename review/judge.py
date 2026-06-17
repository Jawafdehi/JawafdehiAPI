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

from llm.invoke import invoke_json

# Bounded parallelism for LLM calls. Bedrock throttles aggressively, so we
# keep this modest; with hundreds of rules this is the wall-clock divisor.
MAX_WORKERS = int(getattr(settings, "BEDROCK_MAX_WORKERS", 8))


def _effective_max_workers():
    """Get max workers adjusted for provider type.

    CLI providers (codex_cli, claude_cli) have tighter subprocess limits,
    so we use REVIEW_CLI_MAX_WORKERS instead of the larger BEDROCK_MAX_WORKERS.
    """
    from llm.routing import provider_for_tier

    for tier in ("premium", "cheap"):
        if provider_for_tier(tier).name in ("codex_cli", "claude_cli"):
            return int(getattr(settings, "REVIEW_CLI_MAX_WORKERS", 2))
    return MAX_WORKERS


# Cap on the combined source-excerpt block handed to the judge per rule. The old
# 8000 was far too small: with many sources the relevant document (e.g. the
# press release carrying the bigo amount) was cut off, making the judge wrongly
# report it as "truncated / unverifiable". Opus 4.8's context handles a generous
# budget; raise it so figures deep in a source are actually visible.
_SOURCE_EXCERPTS_CAP = int(getattr(settings, "JUDGE_SOURCE_EXCERPTS_CAP", 120000))

# Whether to prompt-cache the shared rule-grading prefix. The same case data +
# source-excerpt block is re-sent for every (rule x sample) call; with hundreds
# of rules that block dominates input cost. Marking it with `cache_control` makes
# Bedrock bill it once per case (cache write) and ~free on every later call
# (cache read) for the cache TTL, which the back-to-back fan-out keeps warm.
PROMPT_CACHE = bool(getattr(settings, "BEDROCK_PROMPT_CACHE", True))

# Provider names whose calls are separate subprocesses with NO cross-call prompt
# cache — each call re-bills (and re-cache-writes) the whole context. For these we
# batch many rules into one call so the expensive case context is sent once per
# batch instead of once per rule. bedrock/proxy keep the per-rule + cache path.
CLI_PROVIDERS = ("codex_cli", "claude_cli")
# Rules per batched CLI call. 1 disables batching (per-rule path; for A/B).
RULE_BATCH_SIZE = int(getattr(settings, "REVIEW_RULE_BATCH_SIZE", 8))

REVIEW_SYSTEM = (
    "You are a meticulous editorial reviewer for Jawafdehi.org, an open civic "
    "archive of Nepali anti-corruption cases. You grade case quality against a "
    "single explicit rule. You are strict but fair. You understand Nepali and "
    "English. You ALWAYS reply with a single valid JSON object and nothing else."
)


def _rule_context_block(case_summary, source_excerpts, case_type_label):
    """The case data + source excerpts shared verbatim by every rule call.

    Kept as a standalone leading block so it can be marked `cache_control` and
    billed once per case instead of once per (rule x sample) invocation.
    """
    return f"""CASE TYPE: {case_type_label or "unknown"}

CASE DATA:
{json.dumps(case_summary, ensure_ascii=False, indent=2)[:30000]}

SOURCE DOCUMENT EXCERPTS (converted to markdown):
{source_excerpts[:_SOURCE_EXCERPTS_CAP]}"""


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
    if PROMPT_CACHE:
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
    if PROMPT_CACHE:
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
    for tier, rules in tier_rules.items():
        if not rules:
            continue
        batched = RULE_BATCH_SIZE > 1 and provider_for_tier(tier).name in CLI_PROVIDERS
        if batched:
            for chunk in _chunk(rules, RULE_BATCH_SIZE):
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

    def _apply(key, parsed):
        if key not in samples or not isinstance(parsed, dict):
            return
        sc = parsed.get("score")
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
        parsed = invoke_json(REVIEW_SYSTEM, content, tier=tier, usage=usage)
        return ("rule", rule["key"], parsed)

    with ThreadPoolExecutor(max_workers=_effective_max_workers()) as pool:
        futures = {pool.submit(_run, t): t for t in tasks}
        for fut in as_completed(futures):
            try:
                res = fut.result()
            except Exception as e:  # noqa: BLE001 - collect, decide later
                errors.append(str(e))
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

    # If nothing at all came back, surface the failure to the caller.
    if not any(samples[k] for k in keys) and not narrative:
        raise RuntimeError(
            f"All {len(tasks)} judge calls failed: {errors[0] if errors else 'unknown'}"
        )

    out = {"_narrative": narrative, "_n_samples": n}
    for k in keys:
        vals = samples[k] or [50]
        mean = statistics.fmean(vals)
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
