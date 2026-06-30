"""Unit tests for the judge-calibration math (offline, no LLM)."""

from __future__ import annotations

import pytest

from evals import calibrate_judge as cj


def test_kappa_perfect_agreement():
    a = ["PASS", "PASS", "REVISE", "REJECT"]
    assert cj.cohen_kappa(a, a) == pytest.approx(1.0)


def test_kappa_known_value():
    # n=3, observed agreement 2/3, expected 4/9 -> kappa = 0.4
    human = ["PASS", "PASS", "REVISE"]
    machine = ["PASS", "REVISE", "REVISE"]
    assert cj.cohen_kappa(human, machine) == pytest.approx(0.4)


def test_kappa_total_disagreement_is_non_positive():
    human = ["PASS", "REJECT", "PASS", "REJECT"]
    machine = ["REJECT", "PASS", "REJECT", "PASS"]
    assert cj.cohen_kappa(human, machine) <= 0.0


def test_kappa_empty_is_zero():
    assert cj.cohen_kappa([], []) == 0.0


def test_confusion_matrix_counts():
    human = ["PASS", "PASS", "REVISE", "REJECT"]
    machine = ["PASS", "REVISE", "REVISE", "REJECT"]
    m = cj.confusion_matrix(human, machine)
    assert m["PASS"]["PASS"] == 1
    assert m["PASS"]["REVISE"] == 1
    assert m["REVISE"]["REVISE"] == 1
    assert m["REJECT"]["REJECT"] == 1


def test_report_shape():
    rep = cj.report(["PASS", "REVISE"], ["PASS", "REJECT"])
    assert rep["n"] == 2
    assert rep["accuracy"] == pytest.approx(0.5)
    assert set(rep["confusion"]) == set(cj.DISPOSITIONS)


def test_load_real_labels():
    cases = cj.load_labels()
    assert len(cases) >= 5
    for c in cases:
        assert c["human_disposition"] in cj.DISPOSITIONS
        assert c["slug"]


def test_prune_oversized_sources(monkeypatch):
    case = {
        "evidence": [
            {
                "source": {
                    "title": "small RAW only",
                    "urls": [{"role": "RAW", "link": "a.pdf"}],
                }
            },
            {
                "source": {
                    "title": "big RAW only",
                    "urls": [{"role": "RAW", "link": "big.pdf"}],
                }
            },
            {
                "source": {
                    "title": "has markdown",
                    "urls": [
                        {"role": "RAW", "link": "c.pdf"},
                        {"role": "MARKDOWN", "link": "c.md"},
                    ],
                }
            },
        ]
    }
    sizes = {"a.pdf": 1.0, "big.pdf": 20.0, "c.pdf": 50.0}
    monkeypatch.setattr(cj, "_pdf_size_mb", lambda u: sizes.get(u))
    pruned, dropped = cj.prune_oversized_sources(case, 8.0)
    titles = [e["source"]["title"] for e in pruned["evidence"]]
    # The big RAW-only PDF is dropped; the small one is kept; the one WITH markdown is kept
    # even though its PDF is huge (it is never converted live, so it can't OOM).
    assert titles == ["small RAW only", "has markdown"]
    assert dropped == [{"title": "big RAW only", "mb": 20.0}]
