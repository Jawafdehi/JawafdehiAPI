"""Rule-centered case-quality scorer.

The system is a set of code-defined Rules (review.code_rules). For a given case we:
  1. detect the case type (CIAA_BASIC / CIAA_EXTENDED / CIAA_HAS_VERDICT / NON_CIAA),
  2. select the rules whose condition (applies_to) matches that type,
  3. score each one:
       - deterministic rules via rules_engine.DETECTORS (exact -> variance 0),
       - LLM rules via the Bedrock judge, sampled N times -> mean + variance,
  4. report per-rule score + confidence (mean + variance/std),
  5. roll up a weighted overall, apply hard-gate rules, and pick a disposition.

This replaces the old fixed 8-dimension rubric.
"""

from . import bedrock_judge, casetype, ngm_client, rules_engine


def _clamp(n):
    return max(0, min(100, int(round(n))))


def _confidence_label(std, n_samples):
    """Map an LLM rule's sample std into a coarse confidence label."""
    if n_samples <= 1:
        return "low"
    if std <= 4:
        return "high"
    if std <= 12:
        return "medium"
    return "low"


# Max chars of the case description handed to the judge. Detailed CIAA verdict
# cases routinely run long, so keep this generous.
_DESCRIPTION_CAP = 20000

# Max chars of EACH source's converted markdown included in the judge excerpts.
# Must be generous enough that a verifiable figure (e.g. the bigo amount in a
# press release / charge sheet, which can sit well past the first page) is
# actually visible to the judge — a too-tight cap makes the judge report a
# source as "truncated / cannot verify" when the data is in fact present.
_PER_SOURCE_EXCERPT_CAP = 12000


def _capped(text, cap):
    """Cap long text, marking the cut explicitly so the judge does NOT mistake
    our truncation for the case text ending mid-sentence (which would unfairly
    penalise the description-quality rule)."""
    text = text or ""
    if len(text) <= cap:
        return text
    return (
        text[:cap]
        + "\n\n[…description truncated here for length by the review system; "
        "the original case text continues beyond this point and is NOT cut off.]"
    )


def build_case_summary(case):
    """Compact case dict handed to the LLM (judge + per-source analysis)."""
    return {
        "slug": case.get("slug"),
        "title": case.get("title"),
        "short_description": case.get("short_description"),
        "description": _capped(case.get("description"), _DESCRIPTION_CAP),
        "bigo": case.get("bigo"),
        "key_allegations": case.get("key_allegations"),
        "timeline_titles": [t.get("title") for t in (case.get("timeline") or [])][:15],
        "entities": [
            {"name": e.get("display_name"), "type": e.get("type")}
            for e in (case.get("entities") or [])
        ],
        "court_cases": case.get("court_cases"),
        "missing_details": case.get("missing_details"),
        "notes": case.get("notes"),
    }


def _source_urls(source):
    """Comma-joined URL list for a source (so the judge can tell same-doc /
    different-URL apart from genuinely duplicated source entries)."""
    u = source.get("url")
    if isinstance(u, (list, tuple)):
        return ", ".join(str(x) for x in u if x)
    return str(u) if u else ""


def _source_analysis_block(source, analysis):
    """Render one source's summary + contribution analysis for the judge prompt."""
    if not analysis:
        return ""
    parts = []
    if analysis.get("summary"):
        parts.append(f"SUMMARY: {analysis['summary']}")
    contrib = analysis.get("contributes_to") or {}
    for field in ("description", "timeline", "key_allegations", "entities"):
        items = contrib.get(field) or []
        if items:
            parts.append(f"{field}: " + "; ".join(str(x) for x in items[:6]))
    if analysis.get("relevance"):
        parts.append(f"relevance: {analysis['relevance']}")
    return "\n".join(parts)


def score_case(case, converted_sources, rules, config, source_analyses=None):
    """Score a case against a list of Rule model instances.

    `rules` is an iterable of active (enabled) code rules, in display order.
    `config` is a ReviewConfig (thresholds + llm_samples).
    `source_analyses` is an optional list (aligned with converted_sources) of
    per-source summary+contribution dicts from bedrock_judge.analyze_sources;
    when omitted it is computed here. Every review summarises each source and
    analyses its contribution before rule scoring.
    Returns a structured, rule-centered result dict.
    """
    ctype = casetype.detect(case)
    ctype_key = ctype["type"]

    # 0. Per-source analysis: summarise each converted source and determine how
    #    it contributes to the case (description / timeline / allegations).
    if source_analyses is None:
        source_analyses = bedrock_judge.analyze_sources(
            build_case_summary(case), converted_sources, ctype["label"]
        )
    analysis_by_idx = {i: a for i, a in enumerate(source_analyses or [])}

    # 1. Split applicable rules by kind.
    applicable = [
        r
        for r in rules
        if r.enabled and rules_engine.is_applicable(r.applies_to, ctype_key)
    ]
    llm_rules = [r for r in applicable if r.kind == r.KIND_LLM]

    # 2. Bedrock judge for all applicable LLM rules (sampled for variance).
    #    Each excerpt carries the raw markdown PLUS the source's contribution
    #    analysis, so rule scoring is informed by what each source establishes.
    excerpts = "\n\n---\n\n".join(
        f"## [source {i + 1}] {s.get('title','source')} ({s.get('source_type','')})\n"
        + f"URLs: {_source_urls(s) or '(none)'}\n"
        + (
            f"[analysis]\n{_source_analysis_block(s, analysis_by_idx.get(i))}\n[/analysis]\n"
            if analysis_by_idx.get(i)
            else ""
        )
        + (s.get("markdown") or "")[:_PER_SOURCE_EXCERPT_CAP]
        for i, s in enumerate(converted_sources)
    )
    # When an LLM rule needs to compare the accused against the official court
    # record, pull the NGM defendant list for this case's court refs and hand it
    # to the judge inside the case summary. Done once and reused across samples.
    base_summary = build_case_summary(case)
    judge_summary = base_summary
    if any(r.key == "accused_list_matches_court_record" for r in llm_rules):
        try:
            ngm = ngm_client.defendants_for_case(case)
            ngm_block = {
                "court_refs": ngm["refs"],
                "defendants": ngm["defendants"],
                "lookup_errors": ngm["errors"],
                "note": (
                    "Defendants pulled from the NGM judicial database for this "
                    "case's court_cases references. Empty court_refs means the "
                    "case has no court case number to verify against."
                ),
            }
        except Exception as e:  # noqa: BLE001 - NGM is best-effort context
            ngm_block = {"error": str(e)}
        # Put the NGM record FIRST: the judge prompt truncates the case-summary
        # JSON, and the entities list can push a trailing key past the cutoff.
        judge_summary = {"ngm_court_record": ngm_block, **base_summary}

    judge_err = None
    judged = {"_narrative": "", "_n_samples": 0}
    if llm_rules:
        rule_specs = [
            {
                "key": r.key,
                "title": r.title,
                "description": r.description,
                "good_examples": r.good_examples,
                "bad_examples": r.bad_examples,
            }
            for r in llm_rules
        ]
        try:
            judged = bedrock_judge.judge_rules(
                judge_summary,
                excerpts,
                ctype["label"],
                rule_specs,
                n_samples=config.llm_samples,
            )
        except Exception as e:  # noqa: BLE001
            judge_err = str(e)

    n_samples = judged.get("_n_samples", 0)
    narrative = judged.get("_narrative", "")
    if judge_err:
        narrative = (
            "LLM judge could not be reached; LLM-rule scores are neutral "
            f"defaults. ({judge_err})"
        )

    # 3. Evaluate every applicable rule into a uniform shape.
    rule_results = []
    gate_failures = []
    for r in applicable:
        if r.kind == r.KIND_LLM:
            jd = judged.get(r.key)
            if jd:
                score = _clamp(jd["mean"])
                variance = jd["variance"]
                std = jd["std"]
                issues = jd.get("issues", [])
                suggestions = jd.get("suggestions", [])
                rationale = jd.get("rationale", "")
                samples = jd.get("samples", [])
                confidence = _confidence_label(std, n_samples)
            else:
                # judge failed -> neutral default, explicitly low confidence
                score, variance, std, samples = 50, 0.0, 0.0, []
                issues = []
                suggestions = []
                rationale = (
                    f"Judge unavailable: {judge_err}" if judge_err else "Judge not run."
                )
                confidence = "low"
        else:
            fn = rules_engine.DETECTORS.get(r.detector)
            if fn is None:
                continue
            score, issues = fn(case)
            suggestions = []
            variance, std, samples = 0.0, 0.0, []
            rationale = ""
            confidence = "high"  # deterministic = exact

        gate_failed = bool(r.is_gate and score < r.gate_min)
        if gate_failed:
            gate_failures.append(
                {"key": r.key, "title": r.title, "score": score, "gate_min": r.gate_min}
            )

        rule_results.append(
            {
                "key": r.key,
                "title": r.title,
                "category": r.category,
                "kind": r.kind,
                "condition_text": r.condition_text,
                "applies_to": r.applies_to,
                "weight": r.weight,
                "is_gate": r.is_gate,
                "gate_min": r.gate_min,
                "gate_failed": gate_failed,
                "score": score,
                "confidence": confidence,
                "variance": variance,
                "std": std,
                "samples": samples,
                "issues": issues,
                "suggestions": suggestions,
                "rationale": rationale,
                "description": r.description,
                "good_examples": r.good_examples,
                "bad_examples": r.bad_examples,
            }
        )

    # 4. Weighted overall over applicable rules.
    total_w = sum(rr["weight"] for rr in rule_results) or 1.0
    overall = _clamp(sum(rr["score"] * rr["weight"] for rr in rule_results) / total_w)

    # 5. Disposition.
    gates_pass = not gate_failures
    pass_t, revise_t = config.pass_threshold, config.revise_threshold
    if not gates_pass:
        disposition = "REJECT"
    elif overall >= pass_t:
        disposition = "PASS"
    elif overall >= revise_t:
        disposition = "REVISE"
    else:
        disposition = "REJECT"

    # 6. Category roll-up (weighted mean per category, for the UI).
    cats = {}
    for rr in rule_results:
        c = cats.setdefault(rr["category"], {"w": 0.0, "ws": 0.0, "rules": 0})
        c["w"] += rr["weight"]
        c["ws"] += rr["score"] * rr["weight"]
        c["rules"] += 1
    categories = [
        {
            "category": name,
            "score": _clamp(v["ws"] / (v["w"] or 1.0)),
            "rules": v["rules"],
        }
        for name, v in cats.items()
    ]

    gaps = []
    for rr in rule_results:
        for issue in rr["issues"]:
            gaps.append({"rule": rr["title"], "issue": issue})

    return {
        "slug": case.get("slug"),
        "title": case.get("title"),
        "state": case.get("state"),
        "case_type": ctype,
        "overall_score": overall,
        "disposition": disposition,
        "rules": rule_results,
        "categories": categories,
        "gate_failures": gate_failures,
        "gates_pass": gates_pass,
        "narrative": narrative,
        "gaps": gaps,
        "judge_error": judge_err,
        "llm_samples": n_samples,
        "thresholds": {"pass": pass_t, "revise": revise_t},
        "model_id_used": None,  # filled by pipeline
        "source_summary": [
            {
                "title": s.get("title"),
                "source_type": s.get("source_type"),
                "conversion_status": s.get("conversion_status"),
                "conversion_note": s.get("conversion_note"),
                "markdown_chars": len(s.get("markdown") or ""),
                # Full converted markdown so the UI can render it in a modal
                # (with a raw-text toggle). Capped to keep the result JSON sane.
                "markdown": (s.get("markdown") or "")[:200000],
                "url": s.get("url") or [],
                # Per-source summary + how it contributes to the case.
                "analysis": analysis_by_idx.get(i),
            }
            for i, s in enumerate(converted_sources)
        ],
    }
