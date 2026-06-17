"""AWS Bedrock LLM judge for rule-centered case-quality review.

Uses Opus 4.8 (global inference profile) per VOL-3 operator request.

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
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import boto3
from botocore.config import Config
from django.conf import settings

_client = None


class UsageAccumulator:
    """Thread-safe tally of Bedrock token usage across parallel calls.

    A single instance is shared by every (rule x sample) and per-source
    invocation of one review, so it must be safe to update from the judge's
    thread pools.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.input_tokens = 0
        self.output_tokens = 0
        self.calls = 0

    def add(self, input_tokens, output_tokens):
        with self._lock:
            self.input_tokens += int(input_tokens or 0)
            self.output_tokens += int(output_tokens or 0)
            self.calls += 1

    def as_dict(self):
        with self._lock:
            in_tok, out_tok, calls = self.input_tokens, self.output_tokens, self.calls
        return {
            "model_id": settings.BEDROCK_MODEL_ID,
            "calls": calls,
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "total_tokens": in_tok + out_tok,
        }


# Bounded parallelism for Bedrock calls. Bedrock throttles aggressively, so we
# keep this modest; with hundreds of rules this is the wall-clock divisor.
MAX_WORKERS = int(getattr(settings, "BEDROCK_MAX_WORKERS", 8))

# Cap on the combined source-excerpt block handed to the judge per rule. The old
# 8000 was far too small: with many sources the relevant document (e.g. the
# press release carrying the bigo amount) was cut off, making the judge wrongly
# report it as "truncated / unverifiable". Opus 4.8's context handles a generous
# budget; raise it so figures deep in a source are actually visible.
_SOURCE_EXCERPTS_CAP = int(getattr(settings, "JUDGE_SOURCE_EXCERPTS_CAP", 120000))


def _bedrock():
    global _client
    if _client is None:
        session = boto3.Session(
            profile_name=settings.AWS_PROFILE or None,
            region_name=settings.AWS_REGION,
        )
        _client = session.client(
            "bedrock-runtime",
            config=Config(
                read_timeout=120,
                connect_timeout=15,
                retries={"max_attempts": 4, "mode": "adaptive"},
                max_pool_connections=MAX_WORKERS + 4,
            ),
        )
    return _client


SYSTEM = (
    "You are a meticulous editorial reviewer for Jawafdehi.org, an open civic "
    "archive of Nepali anti-corruption cases. You grade case quality against a "
    "single explicit rule. You are strict but fair. You understand Nepali and "
    "English. You ALWAYS reply with a single valid JSON object and nothing else."
)


def _build_single_rule_prompt(case_summary, source_excerpts, case_type_label, rule):
    block = f"### Rule `{rule['key']}` — {rule['title']}\n{rule.get('description','')}"
    if rule.get("good_examples"):
        block += f"\nGOOD: {rule['good_examples']}"
    if rule.get("bad_examples"):
        block += f"\nBAD: {rule['bad_examples']}"
    return f"""Grade this Jawafdehi case against the ONE rule below. Reply with JSON only.

CASE TYPE: {case_type_label or "unknown"}

CASE DATA:
{json.dumps(case_summary, ensure_ascii=False, indent=2)[:30000]}

SOURCE DOCUMENT EXCERPTS (converted to markdown):
{source_excerpts[:_SOURCE_EXCERPTS_CAP]}

RULE TO GRADE:
{block}

Score the rule 0-100 with a short rationale, the concrete issues you found, and
a list of concrete, actionable SUGGESTIONS the caseworker can apply to improve
the case against THIS rule (each suggestion an imperative one-liner, e.g.
"Add the special-court verdict date to the timeline"). If the rule is fully
satisfied, return an empty suggestions list. Judge ONLY against this rule.
Reply EXACTLY in this JSON shape:
{{"score": <int 0-100>, "rationale": "<str>", "issues": ["<str>"], "suggestions": ["<str>"]}}"""


def _build_narrative_prompt(case_summary, source_excerpts, case_type_label):
    return f"""Give a 2-3 sentence overall editorial assessment (narrative) of this
Jawafdehi case quality. Reply with JSON only.

CASE TYPE: {case_type_label or "unknown"}

CASE DATA:
{json.dumps(case_summary, ensure_ascii=False, indent=2)[:6000]}

SOURCE DOCUMENT EXCERPTS:
{source_excerpts[:5000]}

Reply EXACTLY: {{"narrative": "<str>"}}"""


def _invoke_text(prompt, max_tokens, usage=None):
    """Invoke the judge once and return its raw text reply (code-fence stripped).

    When a `usage` accumulator is supplied, the response's token counts are
    recorded into it for cost tracking.
    """
    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": max_tokens,
        "system": SYSTEM,
        "messages": [{"role": "user", "content": prompt}],
    }
    resp = _bedrock().invoke_model(
        modelId=settings.BEDROCK_MODEL_ID,
        body=json.dumps(body),
    )
    payload = json.loads(resp["body"].read())
    if usage is not None:
        u = payload.get("usage") or {}
        usage.add(u.get("input_tokens", 0), u.get("output_tokens", 0))
    text = payload["content"][0]["text"].strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    return text


def _invoke_once(prompt, max_tokens=900, usage=None):
    return json.loads(_invoke_text(prompt, max_tokens, usage))


def _invoke_once_salvaged(prompt, max_tokens=900, usage=None):
    """Like _invoke_once but tolerant of truncated/dirty JSON (source analysis)."""
    return _salvage_json(_invoke_text(prompt, max_tokens, usage))


def _salvage_json(text):
    """Best-effort parse of a possibly-truncated/dirty JSON object.

    The judge occasionally returns JSON that is cut off (max_tokens) or carries
    unescaped control chars from quoted source text. Try strict json first, then
    progressively: strip control chars, then close any unterminated string and
    balance braces/brackets so we recover whatever fields completed.
    """
    try:
        return json.loads(text)
    except Exception:  # noqa: BLE001
        pass
    # Drop raw control characters that break JSON string literals.
    cleaned = "".join(ch for ch in text if ch >= " " or ch in "\t")
    try:
        return json.loads(cleaned)
    except Exception:  # noqa: BLE001
        pass
    # Repair truncation: close an open string, then balance brackets.
    s = cleaned
    if s.count('"') % 2 == 1:
        s += '"'
    opens = s.count("{") - s.count("}")
    obrk = s.count("[") - s.count("]")
    s += "]" * max(0, obrk) + "}" * max(0, opens)
    return json.loads(s)  # may still raise; caller catches


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
        try:
            return _invoke_once(prompt, max_tokens=2000, usage=usage)
        except json.JSONDecodeError:
            # Retry parse with salvage for truncated/dirty JSON from large sources.
            return _invoke_once_salvaged(prompt, max_tokens=2000, usage=usage)
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
    workers = min(MAX_WORKERS, len(converted_sources))
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
    """Sample the judge per rule, n_samples times, all in parallel.

    Returns dict:
      {rule_key: {"mean": float, "variance": float, "std": float,
                  "samples": [int...], "rationale": str, "issues": [str]},
       "_narrative": str, "_n_samples": int}
    Raises only if EVERY invocation fails (so the caller can fall back).
    """
    if not llm_rules:
        return {"_narrative": "", "_n_samples": 0}

    n = max(1, int(n_samples))
    keys = [r["key"] for r in llm_rules]
    by_key = {r["key"]: r for r in llm_rules}

    # Build the full task list: one task per (rule, sample) + one narrative task.
    tasks = []  # (kind, key, sample_idx)
    for r in llm_rules:
        for i in range(n):
            tasks.append(("rule", r["key"], i))
    tasks.append(("narrative", None, 0))

    samples = {k: [] for k in keys}
    last_rationale = {k: "" for k in keys}
    last_issues = {k: [] for k in keys}
    last_suggestions = {k: [] for k in keys}
    narrative = ""
    errors = []

    def _invoke_json(prompt, max_tokens=900):
        """Invoke, parsing strictly first, then salvaging dirty/truncated JSON.

        The judge occasionally prefixes the JSON with prose or gets cut off at
        max_tokens; without the salvage fallback such a sample is dropped and the
        rule silently degrades to a neutral default. Mirror analyze_source.
        """
        try:
            return _invoke_once(prompt, max_tokens=max_tokens, usage=usage)
        except json.JSONDecodeError:
            return _invoke_once_salvaged(prompt, max_tokens=max_tokens, usage=usage)

    def _run(task):
        kind, key, _i = task
        if kind == "narrative":
            parsed = _invoke_json(
                _build_narrative_prompt(case_summary, source_excerpts, case_type_label),
                max_tokens=300,
            )
            return ("narrative", None, parsed)
        prompt = _build_single_rule_prompt(
            case_summary, source_excerpts, case_type_label, by_key[key]
        )
        parsed = _invoke_json(prompt)
        return ("rule", key, parsed)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(_run, t): t for t in tasks}
        for fut in as_completed(futures):
            try:
                kind, key, parsed = fut.result()
            except Exception as e:  # noqa: BLE001 - collect, decide later
                errors.append(str(e))
                continue
            if kind == "narrative":
                narrative = parsed.get("narrative", narrative) or narrative
                continue
            sc = parsed.get("score")
            if isinstance(sc, (int, float)):
                samples[key].append(int(round(sc)))
            if parsed.get("rationale"):
                last_rationale[key] = parsed["rationale"]
            if parsed.get("issues"):
                last_issues[key] = parsed["issues"]
            if parsed.get("suggestions"):
                last_suggestions[key] = parsed["suggestions"]

    # If nothing at all came back, surface the failure to the caller.
    if not any(samples[k] for k in keys) and not narrative:
        raise RuntimeError(
            f"All {len(tasks)} Bedrock judge calls failed: {errors[0] if errors else 'unknown'}"
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
