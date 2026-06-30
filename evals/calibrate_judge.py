"""Judge calibration: how well does the LLM case-review judge agree with humans?

The Nepali caveat in the RFC is the whole reason this exists: LLM-as-judge reliability
collapses for low-resource languages (cross-lingual judge agreement around kappa 0.3,
optimistic scoring). Before any judge score is allowed to GATE anything, measure its
agreement with real human dispositions on real Nepali cases. If agreement is low, the
judge is advisory, not a gate.

This module is intentionally split:
  * the math (cohen_kappa, confusion_matrix, report) and the offline ``--demo`` run with
    no Django / network / LLM and are unit-tested;
  * ``--run`` exercises the live judge READ-ONLY — it fetches each case over the HTTP API
    and calls ``review.runner.process_case`` (DB-free; never PATCHes), then scores
    agreement against the human labels in ``datasets/judge_calibration/labels.json``.

    poetry run python -m evals.calibrate_judge --demo
    poetry run python -m evals.calibrate_judge --run --limit 5   # live via claude -p; each case = a full review
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path
from typing import Callable, Optional, Sequence

DISPOSITIONS = ["PASS", "REVISE", "REJECT"]

LABELS_PATH = (
    Path(__file__).resolve().parent / "datasets" / "judge_calibration" / "labels.json"
)

# Illustrative machine column for the offline --demo ONLY. Replace with real `--run`
# output. Aligned positionally with the cases in labels.json; here the judge disagrees on
# one published case (calls it REVISE) to produce a non-trivial kappa.
_ILLUSTRATIVE_MACHINE = ["PASS", "PASS", "REVISE", "PASS", "PASS", "REVISE", "REJECT"]


def cohen_kappa(human: Sequence[str], machine: Sequence[str]) -> float:
    """Cohen's kappa for two label sequences (dependency-free).

    1.0 = perfect agreement, 0.0 = chance-level, < 0 = worse than chance.
    """
    n = len(human)
    if n == 0:
        return 0.0
    labels = set(human) | set(machine)
    po = sum(1 for a, b in zip(human, machine) if a == b) / n
    pe = 0.0
    for label in labels:
        pa = sum(1 for a in human if a == label) / n
        pb = sum(1 for b in machine if b == label) / n
        pe += pa * pb
    if pe >= 1.0:
        # Only possible when both columns are a single identical label -> agreement is
        # perfect by construction.
        return 1.0 if po >= 1.0 else 0.0
    return (po - pe) / (1 - pe)


def confusion_matrix(
    human: Sequence[str], machine: Sequence[str], labels: Sequence[str] = DISPOSITIONS
) -> dict:
    """Return a {human_label: {machine_label: count}} confusion matrix."""
    matrix = {h: {m: 0 for m in labels} for h in labels}
    for a, b in zip(human, machine):
        if a in matrix and b in matrix[a]:
            matrix[a][b] += 1
    return matrix


def report(human: Sequence[str], machine: Sequence[str]) -> dict:
    """Agreement report: n, accuracy, kappa, confusion matrix."""
    n = len(human)
    accuracy = (sum(1 for a, b in zip(human, machine) if a == b) / n) if n else 0.0
    return {
        "n": n,
        "accuracy": accuracy,
        "kappa": cohen_kappa(human, machine),
        "confusion": confusion_matrix(human, machine),
    }


def load_labels(path: Path | str = LABELS_PATH) -> list[dict]:
    """Load the real human-labelled calibration cases."""
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)["cases"]


def render_report(rep: dict, title: str) -> str:
    """Format an agreement report as text."""
    lines = [title, "-" * len(title)]
    lines.append(
        f"n = {rep['n']}   accuracy = {rep['accuracy']:.3f}   kappa = {rep['kappa']:.3f}"
    )
    lines.append("")
    lines.append("confusion (rows = human, cols = machine):")
    header = "          " + "".join(f"{m:>9}" for m in DISPOSITIONS)
    lines.append(header)
    for h in DISPOSITIONS:
        row = rep["confusion"][h]
        lines.append(f"  {h:<8}" + "".join(f"{row[m]:>9}" for m in DISPOSITIONS))
    kappa = rep["kappa"]
    verdict = (
        "GATE-WORTHY (kappa >= 0.6)"
        if kappa >= 0.6
        else "ADVISORY ONLY — do not gate on judge score (kappa < 0.6)"
    )
    lines.append("")
    lines.append(f"interpretation: {verdict}")
    return "\n".join(lines)


def _pdf_size_mb(url: str) -> Optional[float]:
    """HEAD a URL and return its size in MB, or None if unknown.

    Uses a curl User-Agent: the default ``Python-urllib`` UA is blocked by the Cloudflare
    WAF in front of the asset host, which would otherwise make every probe silently fail.
    """
    try:
        req = urllib.request.Request(
            url, method="HEAD", headers={"User-Agent": "curl/8.4.0"}
        )
        with urllib.request.urlopen(
            req, timeout=20
        ) as resp:  # noqa: S310 - trusted s3 host
            cl = resp.headers.get("Content-Length")
            return int(cl) / 1048576 if cl else None
    except Exception:  # noqa: BLE001 - best-effort size probe
        return None


def prune_oversized_sources(case: dict, max_mb: float) -> tuple[dict, list]:
    """Drop evidence sources with no pre-converted MARKDOWN whose RAW PDF exceeds max_mb.

    The review converter runs pdfminer live on any source lacking a MARKDOWN link, and a
    large image-heavy PDF (e.g. a procurement bid document) makes pdfminer OOM and SIGKILL
    the whole review — uncatchable in-process. Such a source has no markdown for prod
    either (its conversion already failed), so dropping it keeps the review faithful while
    preventing the kill. Returns (possibly-new case, dropped[{title, mb}]).
    """
    evidence = case.get("evidence") or []
    kept = []
    dropped = []
    for entry in evidence:
        src = entry.get("source") or {}
        urls = src.get("urls") or []
        has_md = any(
            isinstance(u, dict) and u.get("role") == "MARKDOWN" and u.get("link")
            for u in urls
        )
        if has_md:
            kept.append(entry)
            continue
        pdfs = [
            u["link"]
            for u in urls
            if isinstance(u, dict)
            and str(u.get("link", "")).lower().endswith(".pdf")
            and u.get("role") in ("RAW", "ALTERNATE", "SOURCE_PAGE")
        ]
        oversized = next(
            (
                mb
                for link in pdfs
                if (mb := _pdf_size_mb(link)) is not None and mb > max_mb
            ),
            None,
        )
        if oversized is not None:
            dropped.append({"title": src.get("title", ""), "mb": round(oversized, 1)})
        else:
            kept.append(entry)
    if dropped:
        case = {**case, "evidence": kept}
    return case, dropped


def _default_judge(
    provider: str,
    model: str,
    api_base_url: str = "",
    api_token: str = "",
    skip_oversized_mb: float = 8.0,
) -> Callable[[str], Optional[str]]:
    """Build a live judge_fn(slug) -> disposition. READ-ONLY (no DB writes, no PATCH).

    Lazily bootstraps Django + LLM and fetches each case over the API, then runs the
    DB-free review core. Requires LLM credentials and the bigo-enrichment extras (likhit);
    not exercised in CI. ``skip_oversized_mb`` drops un-converted PDFs above that size so a
    pathological source can't OOM-kill the review (0 disables the guard).
    """
    from casework.common import CaseworkApi, bootstrap

    bootstrap(provider, model)
    from review import runner  # imported after bootstrap

    api = CaseworkApi(base_url=api_base_url, token=api_token)
    cfg = {"pass_threshold": 80, "revise_threshold": 60, "llm_samples": 3}

    def judge_fn(slug: str) -> Optional[str]:
        case = api.get_case(slug)
        if skip_oversized_mb:
            case, dropped = prune_oversized_sources(case, skip_oversized_mb)
            for d in dropped:
                # Loud, never silent: report what was excluded and why.
                print(
                    f"    skip oversized source ({d['mb']} MB, no markdown): {d['title']}",
                    flush=True,
                )
        payload = runner.process_case(case, cfg)
        return (payload.get("result") or {}).get("disposition")

    return judge_fn


def run(cases: list[dict], judge_fn: Callable[[str], Optional[str]]) -> dict:
    """Run judge_fn over each case and report agreement vs human_disposition.

    Each case is a slow full review, so progress is printed (flushed) as it goes, and a
    single failing case is recorded and skipped rather than aborting the whole run.
    """
    human: list[str] = []
    machine: list[str] = []
    per_case = []
    n = len(cases)
    for i, case in enumerate(cases, 1):
        slug = case["slug"]
        h = case["human_disposition"]
        try:
            m = judge_fn(slug)
        except Exception as exc:  # noqa: BLE001 - one bad case must not lose the rest
            print(f"  [{i}/{n}] {slug}: ERROR {exc}", flush=True)
            per_case.append(
                {"slug": slug, "human": h, "machine": None, "error": str(exc)}
            )
            continue
        if m is None:
            print(f"  [{i}/{n}] {slug}: human={h} machine=None (skip)", flush=True)
            per_case.append(
                {"slug": slug, "human": h, "machine": None, "skipped": True}
            )
            continue
        print(f"  [{i}/{n}] {slug}: human={h} machine={m}", flush=True)
        human.append(h)
        machine.append(m)
        per_case.append({"slug": slug, "human": h, "machine": m})
    rep = report(human, machine)
    rep["per_case"] = per_case
    return rep


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Calibrate the LLM review judge vs humans."
    )
    ap.add_argument(
        "--demo",
        action="store_true",
        help="offline demo on bundled illustrative predictions",
    )
    ap.add_argument(
        "--run",
        action="store_true",
        help="live, read-only: run the judge over the real cases",
    )
    # Default to the claude -p CLI provider, which is what the review pollers use.
    ap.add_argument("--provider", default="claude_cli")
    ap.add_argument("--model", default="")
    ap.add_argument("--api-base-url", default="")
    ap.add_argument("--api-token", default="")
    ap.add_argument(
        "--limit", type=int, default=None, help="cap live cases (each is a full review)"
    )
    ap.add_argument(
        "--skip-oversized-mb",
        type=float,
        default=8.0,
        help="drop un-converted PDFs above this size so they can't OOM the review (0=off)",
    )
    args = ap.parse_args(argv)

    cases = load_labels()
    if args.limit:
        cases = cases[: args.limit]

    if args.run:
        judge_fn = _default_judge(
            args.provider,
            args.model,
            args.api_base_url,
            args.api_token,
            args.skip_oversized_mb,
        )
        print(
            f"Running {len(cases)} live review(s) via provider={args.provider} ...",
            flush=True,
        )
        rep = run(cases, judge_fn)
        # Persist the result so it survives stdout buffering / long background runs.
        out_path = LABELS_PATH.parent / "last_run.json"
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(rep, fh, ensure_ascii=False, indent=2)
        print()
        print(render_report(rep, "Judge calibration — LIVE"))
        print(f"\nwrote {out_path}")
        return 0

    if args.demo:
        human = [c["human_disposition"] for c in cases]
        machine = _ILLUSTRATIVE_MACHINE[: len(human)]
        rep = report(human, machine)
        print(
            render_report(
                rep, "Judge calibration — OFFLINE DEMO (illustrative machine column)"
            )
        )
        print(
            "\nNOTE: machine column is illustrative. Use --run for real judge output."
        )
        return 0

    print("Loaded real human-labelled calibration cases:")
    for c in cases:
        print(f"  {c['slug']:<40} [{c['state']:<10}] -> {c['human_disposition']}")
    print("\nRun with --demo (offline) or --run (live, read-only).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
