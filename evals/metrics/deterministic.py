"""Deterministic, language-agnostic eval metrics for the BIGO enricher.

These are the cheap, free, model-independent checks the RFC leans on (especially for
Nepali, where LLM-judging is unreliable): exact field-level accuracy on the amount, and
JSON-schema conformance of the model's output. They reuse the *production* normalisation
(`_coerce_bigo_int`) and schema validator directly — there is no second copy of that logic.

Runnable offline with no Django / network / LLM:

    poetry run python evals/metrics/deterministic.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

# Production logic, imported directly (offline-safe: the enricher's top-level imports are
# stdlib + requests, and the Django bootstrap is a function, not run on import).
from casework.enrich_missing_bigo import _coerce_bigo_int
from llm.prompts.spec import validate_output

GOLDEN_PATH = (
    Path(__file__).resolve().parents[1]
    / "datasets"
    / "enrich_missing_bigo"
    / "golden.json"
)


def load_golden(path: Path | str = GOLDEN_PATH) -> dict:
    """Load the BIGO golden dataset."""
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def coerce_amount(raw: str) -> Optional[int]:
    """Normalise a raw CIAA amount string to an NPR integer (production logic)."""
    return _coerce_bigo_int(raw)


def bigo_field_match(predicted: Optional[int], expected: Optional[int]) -> bool:
    """Exact field-level match for the bigo amount (None == None counts as a match)."""
    return predicted == expected


def score_amount_coercion(entries: list[dict]) -> dict:
    """Score the deterministic amount-coercion layer over golden entries.

    Returns field_accuracy (= document_accuracy here, one field per entry), the count, and
    a per-entry breakdown so a CI run can show exactly which string regressed.
    """
    results = []
    correct = 0
    for entry in entries:
        raw = entry["raw_amount"]
        expected = entry["expected_bigo"]
        got = coerce_amount(raw)
        ok = bigo_field_match(got, expected)
        correct += int(ok)
        results.append(
            {
                "raw_amount": raw,
                "expected": expected,
                "got": got,
                "ok": ok,
                "court_case": entry.get("court_case", ""),
                "edge": entry.get("edge", ""),
            }
        )
    total = len(entries)
    return {
        "field_accuracy": (correct / total) if total else 1.0,
        "correct": correct,
        "total": total,
        "results": results,
    }


def score_model_output(
    predicted_obj: dict, expected_bigo: Optional[int], schema: Optional[dict]
) -> dict:
    """Score one raw model output: schema conformance + bigo field match.

    `predicted_obj` is the parsed JSON the model returned for an extraction case; the bigo
    is read from it and coerced with the production logic before comparison.
    """
    schema_errors = validate_output(predicted_obj, schema)
    raw_bigo = predicted_obj.get("bigo") if isinstance(predicted_obj, dict) else None
    if isinstance(raw_bigo, str):
        predicted_bigo = coerce_amount(raw_bigo)
    else:
        predicted_bigo = raw_bigo
    return {
        "schema_ok": not schema_errors,
        "schema_errors": schema_errors,
        "predicted_bigo": predicted_bigo,
        "expected_bigo": expected_bigo,
        "bigo_match": bigo_field_match(predicted_bigo, expected_bigo),
    }


def _selftest() -> int:
    """Offline self-test: score the real golden coercion set; 0 = all pass."""
    golden = load_golden()
    report = score_amount_coercion(golden["amount_coercion"])

    print("BIGO amount-coercion eval (real CIAA strings)\n" + "-" * 64)
    for r in report["results"]:
        flag = "OK  " if r["ok"] else "FAIL"
        print(
            f"  [{flag}] {r['raw_amount']!r} -> {r['got']} (expected {r['expected']})"
        )
    print("-" * 64)
    print(
        f"field_accuracy = {report['field_accuracy']:.3f} "
        f"({report['correct']}/{report['total']})"
    )

    # Schema conformance: a well-formed output passes; a malformed one is rejected.
    from llm.prompts import get

    schema = get("enrich.missing_bigo").output_schema
    good = {
        "bigo": 11001199,
        "confidence": "high",
        "evidence_quote": "बिगो रु. १,१०,०१,१९९।७५ कायम",
        "press_release_type": "charge_filing",
    }
    bad = {"bigo": "lots", "confidence": "definitely", "evidence_quote": 5}
    good_errs = validate_output(good, schema)
    bad_errs = validate_output(bad, schema)
    print(
        f"schema: good_output_errors={good_errs} "
        f"bad_output_caught={len(bad_errs)} issue(s)"
    )

    ok = report["correct"] == report["total"] and not good_errs and bad_errs
    print("\nRESULT:", "ALL PASS" if ok else "FAILURES PRESENT")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(_selftest())
