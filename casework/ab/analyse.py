#!/usr/bin/env python
"""Turn a raw A/B run into the report tables.

Reads `ab_raw.json` (written by `run_ab.py`), scores every arm's output with
the case reviewer, and emits the markdown tables that go into
`casework/ab/RESULTS.md`.

Every table here reports unmet/skipped/error counts alongside the agreement
figures. A stage where both arms produced nothing is reported as BOTH
PRODUCED NOTHING -- `diff.no_output` keeps such rows out of the agreement
denominator entirely, so an empty run can never surface as "100% agreement".
"""

import argparse
import collections
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from casework.ab.diff import three_way_report  # noqa: E402
from casework.ab.run_ab import build_rows  # noqa: E402


def pct(x):
    return "n/a" if x is None else f"{100 * x:.1f}%"


def fmt(v, width=48):
    if v is None:
        return "—"
    if isinstance(v, list):
        if not v:
            return "(empty)"
        return f"{len(v)} item(s)"
    s = str(v)
    return s if len(s) <= width else s[:width] + "…"


def stage_table(rows):
    """Per-stage agreement, with no_output broken out as its own column."""
    by_stage = collections.defaultdict(list)
    for r in rows:
        by_stage[r["stage"]].append(r)
    out = ["| stage | cases | comparable | A==B | A==B rate | all three agree | "
           "neither produced | readback error |",
           "|---|---|---|---|---|---|---|---|"]
    for stage in ("bigo", "tags", "timeline", "allegations", "entities"):
        rs = by_stage.get(stage) or []
        if not rs:
            continue
        rep = three_way_report(rs)
        ab = sum(1 for r in rs
                 if r["verdict"] in ("all_agree", "both_diverge_from_golden"))
        out.append(
            f"| `{stage}` | {rep['total']} | {rep['comparable']} | {ab} | "
            f"{pct(rep['ab_agreement_rate'])} | "
            f"{rep['counts'].get('all_agree', 0)} | {rep['no_output']} | "
            f"{rep['counts'].get('readback_error', 0)} |")
    out.append("")
    out.append("`comparable` excludes rows where neither arm produced output and "
               "rows we failed to read back; the rate is over `comparable` only, "
               "so a stage where nothing happened reports `n/a`, never 100%.")
    return "\n".join(out)


def outcome_table(outcomes):
    """Per-arm, per-stage outcome counts straight from each arm's own output."""
    stages = sorted({s for arm in outcomes.values() for s in arm})
    kinds = sorted({v for arm in outcomes.values() for st in arm.values()
                    for v in st.values()})
    out = ["| arm | stage | " + " | ".join(f"`{k}`" for k in kinds) + " |",
           "|---|---|" + "---|" * len(kinds)]
    for arm in sorted(outcomes):
        for stage in stages:
            per = outcomes[arm].get(stage) or {}
            c = collections.Counter(per.values())
            out.append(f"| {arm} | `{stage}` | "
                       + " | ".join(str(c.get(k, 0)) for k in kinds) + " |")
    return "\n".join(out)


def presence_table(rows):
    """Who produced output at all -- the guard against reading parity into
    a run where one or both arms were silent."""
    by_stage = collections.defaultdict(lambda: collections.Counter())
    for r in rows:
        c = by_stage[r["stage"]]
        c["A"] += bool(r["a_present"])
        c["B"] += bool(r["b_present"])
        c["G"] += bool(r["g_present"])
        c["n"] += 1
        if r["a_present"] and not r["b_present"]:
            c["A_only"] += 1
        if r["b_present"] and not r["a_present"]:
            c["B_only"] += 1
    out = ["| stage | cases | A produced | B produced | golden had | "
           "A only | B only |", "|---|---|---|---|---|---|---|"]
    for stage in ("bigo", "tags", "timeline", "allegations", "entities"):
        c = by_stage.get(stage)
        if not c:
            continue
        out.append(f"| `{stage}` | {c['n']} | {c['A']} | {c['B']} | {c['G']} | "
                   f"{c['A_only']} | {c['B_only']} |")
    return "\n".join(out)


def tags_metrics_table(rows):
    rs = [r for r in rows if r["stage"] == "tags" and r["verdict"] != "no_output"]
    if not rs:
        return "_No comparable tag rows._"
    js = [r["metrics"]["jaccard"] for r in rs if r["metrics"]["jaccard"] is not None]
    ps = [r["metrics"]["precision"] for r in rs
          if r["metrics"]["precision"] is not None]
    rc = [r["metrics"]["recall"] for r in rs if r["metrics"]["recall"] is not None]
    mean = lambda xs: (sum(xs) / len(xs)) if xs else None  # noqa: E731
    return "\n".join([
        f"- comparable cases: **{len(rs)}**",
        f"- mean Jaccard(A,B): **{pct(mean(js))}**",
        f"- mean precision (B's tags that A also produced): **{pct(mean(ps))}**",
        f"- mean recall (A's tags that B also produced): **{pct(mean(rc))}**",
        f"- exact set matches: **{sum(1 for r in rs if r['metrics']['jaccard'] == 1.0)}/{len(rs)}**",
    ])


def timeline_metrics_table(rows):
    rs = [r for r in rows if r["stage"] == "timeline" and r["verdict"] != "no_output"]
    if not rs:
        return "_No comparable timeline rows._"
    out = ["| case | A entries | B entries | date Jaccard | same dates, in order |",
           "|---|---|---|---|---|"]
    for r in sorted(rs, key=lambda r: r["slug"]):
        m = r["metrics"]
        out.append(f"| `{r['slug'][:38]}` | {m['n_a']} | {m['n_b']} | "
                   f"{pct(m['date_jaccard'])} | "
                   f"{'yes' if m['dates_equal_ordered'] else 'no'} |")
    djs = [r["metrics"]["date_jaccard"] for r in rs
           if r["metrics"]["date_jaccard"] is not None]
    if djs:
        out.append("")
        out.append(f"Mean date Jaccard: **{pct(sum(djs) / len(djs))}**; "
                   f"exact ordered date match: "
                   f"**{sum(1 for r in rs if r['metrics']['dates_equal_ordered'])}"
                   f"/{len(rs)}**")
    return "\n".join(out)


def bigo_table(rows):
    rs = [r for r in rows if r["stage"] == "bigo"]
    out = ["| case | Arm A | Arm B | golden | verdict |", "|---|---|---|---|---|"]
    for r in sorted(rs, key=lambda r: r["slug"]):
        out.append(f"| `{r['slug'][:38]}` | {fmt(r['a'])} | {fmt(r['b'])} | "
                   f"{fmt(r['g'])} | {r['verdict']} |")
    return "\n".join(out)


def reviewer_table(scores):
    """Reviewer scores per arm. Deterministic rules only -- see reviewer.py
    for what the reviewer does and does not actually discriminate on."""
    if not scores:
        return "_Reviewer scoring not available._"
    out = ["| case | A overall | B overall | A bigo rule | B bigo rule | "
           "A timeline rule | B timeline rule | A structural | B structural |",
           "|---|---|---|---|---|---|---|---|---|"]
    agg = collections.defaultdict(list)
    for slug in sorted(scores):
        s = scores[slug]
        a, b = s.get("A") or {}, s.get("B") or {}

        def rule(x, key):
            return (x.get("rules", {}).get(key) or {}).get("score")

        out.append(
            f"| `{slug[:34]}` | {a.get('overall_score')} | {b.get('overall_score')} | "
            f"{rule(a, 'bigo_amount_present')} | {rule(b, 'bigo_amount_present')} | "
            f"{rule(a, 'timeline_completeness')} | {rule(b, 'timeline_completeness')} | "
            f"{rule(a, 'structural_completeness')} | {rule(b, 'structural_completeness')} |")
        for arm, x in (("A", a), ("B", b)):
            if x.get("overall_score") is not None:
                agg[arm].append(x["overall_score"])
    out.append("")
    for arm in sorted(agg):
        vals = agg[arm]
        out.append(f"- Arm {arm} mean overall reviewer score: "
                   f"**{sum(vals) / len(vals):.1f}** (n={len(vals)})")
    return "\n".join(out)


def score_all(raw, base_cases):
    """Reviewer-score both arms for every sampled case."""
    from casework.ab.reviewer import score_arms

    out = {}
    for slug in raw["sample"]["slugs"]:
        case = base_cases.get(slug)
        if not case:
            continue
        arm_values = {}
        for arm in ("A", "B"):
            vals = (raw["results"].get(arm) or {}).get(slug) or {}
            if "_error" in vals:
                continue
            arm_values[arm] = {k: vals.get(k) for k in
                               ("bigo", "tags", "timeline", "key_allegations")}
        if len(arm_values) < 2:
            continue
        try:
            out[slug] = score_arms(case, arm_values)
        except Exception as exc:  # noqa: BLE001
            out[slug] = {"error": str(exc)}
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", required=True)
    ap.add_argument("--cases", required=True,
                    help="dir of golden case JSON (snapshot/cases)")
    ap.add_argument("--golden", default="")
    ap.add_argument("--blocked", default="",
                    help="comma-separated slugs the port could not fetch (UA/WAF 403)")
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)

    raw = json.load(open(args.raw))
    # Re-derive the rows from the raw readback rather than trusting the ones
    # stored by the run: build_rows gained a readback-error guard after the
    # run started, and the stored rows predate it.
    golden = json.load(open(args.golden)) if args.golden else {}
    rows = build_rows(
        raw["sample"]["slugs"], raw["results"].get("A") or {},
        raw["results"].get("B") or {}, golden,
        (raw.get("entities") or {}).get("A") or {},
        (raw.get("entities") or {}).get("B") or {})

    base_cases = {}
    for slug in raw["sample"]["slugs"]:
        path = os.path.join(args.cases, f"{slug}.json")
        if os.path.exists(path):
            base_cases[slug] = json.load(open(path))
    scores = score_all(raw, base_cases)

    # Cases the port physically could not read because of the missing
    # User-Agent header (WAF 403 on s3-hosted MARKDOWN). Separating these
    # keeps an INFRASTRUCTURE gap from being read as an extraction-quality
    # gap -- Arm A read the identical document fine.
    blocked = set(filter(None, (args.blocked or "").split(",")))
    unblocked = [r for r in rows if r["slug"] not in blocked]

    parts = [
        "## Per-stage agreement (all sampled cases)\n", stage_table(rows), "",
    ]
    if blocked:
        parts += [
            f"### Excluding the {len(blocked)} case(s) blocked by the "
            "User-Agent defect\n",
            "These cases are ones the PORT could not fetch source text for at "
            "all (WAF 403), while the donor read the identical document. Their "
            "divergence measures a missing request header, not extraction "
            "quality, so the table below is the fairer read of enricher "
            "behaviour. `tags` is unaffected either way -- it reads no "
            "evidence.\n",
            stage_table(unblocked), "",
        ]
    parts += [
        "## Who produced output at all\n", presence_table(rows), "",
        "## Per-arm outcomes (from each arm's own run output)\n",
        outcome_table(raw.get("outcomes") or {}), "",
        "## bigo (exact)\n", bigo_table(rows), "",
        "## tags (set metrics)\n", tags_metrics_table(rows), "",
        "## timeline (structural)\n", timeline_metrics_table(rows), "",
        "## Case-reviewer scores\n", reviewer_table(scores), "",
    ]
    text = "\n".join(parts)
    with open(args.out, "w") as fh:
        fh.write(text)
    json.dump(scores, open(args.out + ".scores.json", "w"),
              indent=1, ensure_ascii=False)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
