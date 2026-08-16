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

from . import casetype, judge, ngm_client, rules_engine


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
        # Canonical @id IRIs (parseable refs only — the judge and the NGM
        # cross-check both key off these).
        "court_cases": ngm_client.court_refs_for_case(case),
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


def _build_excerpts(converted_sources, analysis_by_idx):
    """One markdown blob for the judge: each source's header, URLs, analysis, body.

    Each excerpt carries the raw markdown PLUS the source's contribution
    analysis, so rule scoring is informed by what each source establishes.
    """
    return "\n\n---\n\n".join(
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


def _judge_summary_for(case, llm_rules):
    """The case summary handed to the judge, with the NGM court record when needed.

    When an LLM rule needs to compare the accused against the official court
    record, pull the NGM defendant list for this case's court refs and hand it to
    the judge inside the case summary. Done once and reused across samples.
    """
    base_summary = build_case_summary(case)
    if not any(r.key == "accused_list_matches_court_record" for r in llm_rules):
        return base_summary
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
    return {"ngm_court_record": ngm_block, **base_summary}


def _run_judge(judge_summary, excerpts, ctype, llm_rules, config, usage):
    """Grade every applicable LLM rule. Returns ``(judged, judge_err)``.

    A judge failure is not fatal: ``judged`` falls back to the neutral shape and
    the error string is threaded through so each rule can explain itself.

    The exception is ``JudgeUnavailable`` -- the judge was unreachable for at
    least one rule (dead credential, 429 session cap, network) -- which is
    deliberately NOT caught. Scoring a review whose LLM rules never ran produces
    a confident-looking ~70 that is indistinguishable from a real near-miss, so
    it must fail the job instead and let ``on_failure`` mark the review failed.
    """
    judged = {"_narrative": "", "_n_samples": 0}
    if not llm_rules:
        return judged, None
    rule_specs = [
        {
            "key": r.key,
            "title": r.title,
            "description": r.description,
            "good_examples": r.good_examples,
            "bad_examples": r.bad_examples,
            # Routes the judge model tier: gate rules -> premium model.
            "is_gate": r.is_gate,
        }
        for r in llm_rules
    ]
    try:
        return (
            judge.judge_rules(
                judge_summary,
                excerpts,
                ctype["label"],
                rule_specs,
                n_samples=config.llm_samples,
                usage=usage,
            ),
            None,
        )
    except judge.JudgeUnavailable:
        raise
    except Exception as e:  # noqa: BLE001 - judge failure degrades to neutral scores
        return judged, str(e)


def _grade_llm_rule(rule, judged, judge_err):
    """Score one LLM rule off the judge's reply.

    Returns ``(fields, graded)``, where ``graded`` is False when the judge never
    actually returned a grade for this rule -- either it failed outright or its
    batch reply omitted the key even after the per-rule retry. In both cases the
    50 is a placeholder, not a verdict, and must not fail a gate.
    """
    jd = judged.get(rule.key)
    if not jd:
        # judge failed -> neutral default, explicitly low confidence
        return (
            {
                "score": 50,
                "variance": 0.0,
                "std": 0.0,
                "samples": [],
                "issues": [],
                "suggestions": [],
                "rationale": (
                    f"Judge unavailable: {judge_err}" if judge_err else "Judge not run."
                ),
                "confidence": "low",
            },
            False,
        )

    samples = jd.get("samples", [])
    rationale = jd.get("rationale", "")
    graded = True
    # Confidence from THIS rule's actual sample count, not the global
    # llm_samples: batched CLI grading is single-pass (1 sample) and must not be
    # labelled high-confidence off a zero std.
    confidence = _confidence_label(jd["std"], len(samples))
    if not samples:
        graded = False
        confidence = "low"
        rationale = rationale or (
            "The judge did not return a grade for this rule "
            "(incomplete reply); score is a neutral default."
        )
    return (
        {
            "score": _clamp(jd["mean"]),
            "variance": jd["variance"],
            "std": jd["std"],
            "samples": samples,
            "issues": jd.get("issues", []),
            "suggestions": jd.get("suggestions", []),
            "rationale": rationale,
            "confidence": confidence,
        },
        graded,
    )


def _grade_deterministic_rule(rule, case):
    """Score one code rule via its detector. ``None`` when the detector is unknown."""
    fn = rules_engine.DETECTORS.get(rule.detector)
    if fn is None:
        return None
    score, issues = fn(case)
    return {
        "score": score,
        "variance": 0.0,
        "std": 0.0,
        "samples": [],
        "issues": issues,
        "suggestions": [],
        "rationale": "",
        "confidence": "high",  # deterministic = exact
    }


def _evaluate_rules(applicable, case, judged, judge_err):
    """Score every applicable rule into one uniform shape.

    Returns ``(rule_results, gate_failures, ungraded_llm_keys)``. A rule whose
    detector is unknown is skipped entirely rather than scored.
    """
    rule_results = []
    gate_failures = []
    ungraded_llm_keys = []
    for r in applicable:
        if r.kind == r.KIND_LLM:
            fields, graded = _grade_llm_rule(r, judged, judge_err)
            # An LLM rule the judge ran but never graded (batch reply omitted its
            # key even after the per-rule retry) is reported in the narrative.
            if not graded and judged.get(r.key):
                ungraded_llm_keys.append(r.key)
        else:
            fields = _grade_deterministic_rule(r, case)
            if fields is None:  # unknown detector -> rule is not scored at all
                continue
            graded = True

        # A gate can only fail on a REAL grade. An ungraded rule's neutral 50
        # placeholder must not REJECT the case — except on total judge failure
        # (judge_err), where rejecting-with-explanation is the long-standing
        # loud behavior (gates are unverified, and the narrative says so).
        gate_failed = bool(
            r.is_gate and fields["score"] < r.gate_min and (graded or judge_err)
        )
        if gate_failed:
            gate_failures.append(
                {
                    "key": r.key,
                    "title": r.title,
                    "score": fields["score"],
                    "gate_min": r.gate_min,
                }
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
                "description": r.description,
                "good_examples": r.good_examples,
                "bad_examples": r.bad_examples,
                **fields,
            }
        )
    return rule_results, gate_failures, ungraded_llm_keys


def _build_narrative(judged, judge_err, ungraded_llm_keys):
    """The reviewer-facing narrative, including any placeholder-score warning.

    A total judge failure replaces the narrative outright. Otherwise ungraded
    rules must never be silent: they are appended so a reviewer can see which
    scores are placeholders, not verdicts.
    """
    if judge_err:
        return (
            "LLM judge could not be reached; LLM-rule scores are neutral "
            f"defaults. ({judge_err})"
        )
    narrative = judged.get("_narrative", "")
    if ungraded_llm_keys:
        narrative = (
            f"{narrative} NOTE: {len(ungraded_llm_keys)} rule(s) received no "
            f"grade from the judge ({', '.join(ungraded_llm_keys)}); their "
            "scores are neutral defaults and did not gate the case."
        ).strip()
    return narrative


def _source_summary(converted_sources, analysis_by_idx):
    """Per-source conversion status + full markdown for the review UI."""
    return [
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
    ]


def _weighted_overall(rule_results):
    """Weighted mean score over every scored rule."""
    total_w = sum(rr["weight"] for rr in rule_results) or 1.0
    return _clamp(sum(rr["score"] * rr["weight"] for rr in rule_results) / total_w)


def _gaps_from(rule_results):
    """Flatten every rule's issues into ``{rule, issue}`` rows for the UI."""
    return [
        {"rule": rr["title"], "issue": issue}
        for rr in rule_results
        for issue in rr["issues"]
    ]


def _disposition_for(overall, gates_pass, config):
    """PASS / REVISE / REJECT. A failed gate rejects regardless of the score."""
    if not gates_pass:
        return "REJECT"
    if overall >= config.pass_threshold:
        return "PASS"
    if overall >= config.revise_threshold:
        return "REVISE"
    return "REJECT"


def _category_rollup(rule_results):
    """Weighted mean per category, for the UI."""
    cats = {}
    for rr in rule_results:
        c = cats.setdefault(rr["category"], {"w": 0.0, "ws": 0.0, "rules": 0})
        c["w"] += rr["weight"]
        c["ws"] += rr["score"] * rr["weight"]
        c["rules"] += 1
    return [
        {
            "category": name,
            "score": _clamp(v["ws"] / (v["w"] or 1.0)),
            "rules": v["rules"],
        }
        for name, v in cats.items()
    ]


def score_case(
    case, converted_sources, rules, config, source_analyses=None, usage=None
):
    """Score a case against a list of Rule model instances.

    `rules` is an iterable of active (enabled) code rules, in display order.
    `config` is a ReviewConfig (thresholds + llm_samples).
    `source_analyses` is an optional list (aligned with converted_sources) of
    per-source summary+contribution dicts from judge.analyze_sources;
    when omitted it is computed here. Every review summarises each source and
    analyses its contribution before rule scoring.
    `usage` is an optional llm.usage.UsageAccumulator that LLM calls record
    their token usage into for cost tracking.
    Returns a structured, rule-centered result dict.
    """
    ctype = casetype.detect(case)
    ctype_key = ctype["type"]

    # 0. Per-source analysis: summarise each converted source and determine how
    #    it contributes to the case (description / timeline / allegations).
    if source_analyses is None:
        source_analyses = judge.analyze_sources(
            build_case_summary(case), converted_sources, ctype["label"], usage=usage
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
    excerpts = _build_excerpts(converted_sources, analysis_by_idx)
    judged, judge_err = _run_judge(
        _judge_summary_for(case, llm_rules), excerpts, ctype, llm_rules, config, usage
    )

    n_samples = judged.get("_n_samples", 0)
    # 3. Evaluate every applicable rule into a uniform shape.
    rule_results, gate_failures, ungraded_llm_keys = _evaluate_rules(
        applicable, case, judged, judge_err
    )
    narrative = _build_narrative(judged, judge_err, ungraded_llm_keys)

    # 4. Weighted overall over applicable rules.
    overall = _weighted_overall(rule_results)

    # 5. Disposition.
    gates_pass = not gate_failures
    pass_t, revise_t = config.pass_threshold, config.revise_threshold
    disposition = _disposition_for(overall, gates_pass, config)

    # 6. Category roll-up (weighted mean per category, for the UI).
    categories = _category_rollup(rule_results)
    gaps = _gaps_from(rule_results)

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
        "source_summary": _source_summary(converted_sources, analysis_by_idx),
    }
