"""Live, end-to-end BIGO eval + model-migration comparison (READ-ONLY).

Runs the real extraction pipeline over the golden ``extraction`` cases and scores
field-level accuracy against the human-approved bigo. Because it can run any tier/model,
this is the model-migration tool: it answers "is the cheap tier (or a new model) safe to
swap in for this enricher?" with a per-case delta, not a guess.

It reuses the enricher's OWN input assembly (``_get_source_content``) and post-processing
(``_parse_bigo_response``) so the only thing that varies is the model — the eval is
faithful to production. It only reads (fetch case + call model); it never PATCHes.

    poetry run python -m evals.run_eval --tier premium        # via claude -p (default provider)
    poetry run python -m evals.run_eval --compare             # premium vs cheap migration verdict

Needs the bigo-enrichment extras (likhit) + a read API token (JAWAFDEHI_API_TOKEN); not run in CI.
"""

from __future__ import annotations

import argparse
import sys
from typing import Optional

from evals.metrics import deterministic as det


def evaluate(
    tier: str,
    provider: str,
    model: str,
    api_base_url: str = "",
    api_token: str = "",
    limit: Optional[int] = None,
) -> dict:
    """Run the BIGO pipeline at a given tier over the golden extraction cases."""
    from casework.common import CaseworkApi, bootstrap, clamp, env_int

    bootstrap(provider, model)
    from casework.enrich_missing_bigo import _get_source_content, _parse_bigo_response
    from llm.invoke import invoke_text
    from llm.prompts import get
    from llm.usage import UsageAccumulator

    spec = get("enrich.missing_bigo")
    api = CaseworkApi(base_url=api_base_url, token=api_token)
    cases = det.load_golden()["extraction"]
    if limit:
        cases = cases[:limit]

    usage = UsageAccumulator()
    rows = []
    correct = 0
    scored = 0
    for entry in cases:
        slug = entry["slug"]
        expected = entry["expected_bigo"]
        detail = api.get_case(slug)
        source_text, source_context = _get_source_content(detail)
        if not source_text:
            rows.append({"court_case": entry["court_case"], "skipped": "no source"})
            continue
        content = spec.render_user(
            case_id=entry["court_case"],
            case_title=entry["title"],
            source_context=source_context,
            markdown=clamp(
                source_text, env_int("CASEWORK_BIGO_FEED_CHARS", 100000), "bigo"
            ),
        )
        resp = invoke_text(
            system=spec.system,
            content=content,
            max_tokens=spec.max_tokens,
            tier=tier,
            usage=usage,
        )
        predicted = _parse_bigo_response(resp)
        match = det.bigo_field_match(predicted, expected)
        scored += 1
        correct += int(match)
        rows.append(
            {
                "court_case": entry["court_case"],
                "expected": expected,
                "predicted": predicted,
                "match": match,
            }
        )

    return {
        "tier": tier,
        "field_accuracy": (correct / scored) if scored else 0.0,
        "correct": correct,
        "scored": scored,
        "rows": rows,
        "usage": usage.as_dict(),
    }


def _print_report(rep: dict) -> None:
    from llm.usage import render_usage_table

    print(f"\nBIGO extraction eval — tier={rep['tier']}")
    print("-" * 72)
    for r in rep["rows"]:
        if r.get("skipped"):
            print(f"  [skip] {r['court_case']}: {r['skipped']}")
            continue
        flag = "OK  " if r["match"] else "FAIL"
        print(
            f"  [{flag}] {r['court_case']}: predicted={r['predicted']} expected={r['expected']}"
        )
    print("-" * 72)
    print(
        f"field_accuracy = {rep['field_accuracy']:.3f} ({rep['correct']}/{rep['scored']})"
    )
    print(render_usage_table(rep["usage"]["by_provider"], title="eval usage"))


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Live BIGO eval / model-migration comparison."
    )
    ap.add_argument("--tier", default="premium", choices=["premium", "cheap"])
    ap.add_argument(
        "--compare", action="store_true", help="run premium AND cheap, show delta"
    )
    # Default to the claude -p CLI provider, which is what the enrichers/pollers use.
    ap.add_argument("--provider", default="claude_cli")
    ap.add_argument("--model", default="")
    ap.add_argument("--api-base-url", default="")
    ap.add_argument("--api-token", default="")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args(argv)

    if args.compare:
        prem = evaluate(
            "premium",
            args.provider,
            args.model,
            args.api_base_url,
            args.api_token,
            args.limit,
        )
        cheap = evaluate(
            "cheap",
            args.provider,
            args.model,
            args.api_base_url,
            args.api_token,
            args.limit,
        )
        _print_report(prem)
        _print_report(cheap)
        delta = cheap["field_accuracy"] - prem["field_accuracy"]
        print("\n=== MODEL-MIGRATION VERDICT ===")
        print(f"premium accuracy = {prem['field_accuracy']:.3f}")
        print(f"cheap   accuracy = {cheap['field_accuracy']:.3f}")
        print(f"delta (cheap - premium) = {delta:+.3f}")
        print(
            "SAFE to swap to cheap"
            if delta >= 0
            else "NOT safe — cheap regresses on real cases"
        )
        return 0

    rep = evaluate(
        args.tier,
        args.provider,
        args.model,
        args.api_base_url,
        args.api_token,
        args.limit,
    )
    _print_report(rep)
    return 0


if __name__ == "__main__":
    sys.exit(main())
