"""Precision and recall against the labelled set, and the zero-false-positive gate.

A false positive here is a wrong bind: a named individual publicly attached to a
corruption case they had nothing to do with. One is a build failure.

The fixtures and the labelling method are described in `fixtures/README.md`. The
labels come from evidence `resolve` cannot read -- the full prod entity document
and the binds already on the source case -- so this measures the resolver rather
than mirroring it.

The pipeline under test is the two-step one a caller uses, not `resolve` alone:
`resolve` matches names, then `apply_document_veto` reads the bound entity's
document and refuses Election Commission candidate records. Measuring `resolve`
on its own scores 0.872, because the name matcher cannot tell a namesake ward
candidate from the official a case names -- `/api/search/` does not return the
field that would say.
"""
import json
from pathlib import Path

from casework.entity_resolver import BIND, apply_document_veto, resolve

FIXTURES = Path(__file__).parent / "fixtures"


def load_labels():
    return [json.loads(line) for line in
            (FIXTURES / "entity_labels.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()]


def load_candidates():
    return json.loads((FIXTURES / "entity_candidates.json").read_text(encoding="utf-8"))


def load_documents():
    return json.loads((FIXTURES / "entity_documents.json").read_text(encoding="utf-8"))


def _decisions():
    """Every labelled row through the full two-step decision, offline.

    The frozen documents stand in for `CaseworkApi.get_entity`; the veto is only
    consulted for a BIND, exactly as the caller does it, so no test depends on a
    document for a row that never reaches that step.
    """
    candidates, documents = load_candidates(), load_documents()
    for row in load_labels():
        decision = resolve(row["extracted"], candidates.get(row["extracted"], []))
        if decision.is_bind:
            assert decision.nes_id in documents, (
                f"no frozen document for {decision.nes_id} -- re-run "
                "scripts/capture_entity_candidates.py")
            decision = apply_document_veto(decision, documents[decision.nes_id])
        yield row, decision


def test_zero_false_positives_across_the_labelled_set():
    false_positives = []
    for row, decision in _decisions():
        if decision.verdict != BIND:
            continue
        if row["expected_verdict"] != BIND or decision.nes_id != row["expected_nes_id"]:
            false_positives.append(
                f"{row['extracted']!r} bound to {decision.nes_id} "
                f"(expected {row['expected_verdict']} {row['expected_nes_id']}) "
                f"-- {row['provenance']}")
    assert not false_positives, (
        "WRONG BINDS — each one attaches a named individual to a case they may have "
        "nothing to do with:\n" + "\n".join(false_positives))


def test_precision_and_recall_are_reported(capsys):
    rows = list(_decisions())
    should_bind = [r for r, _ in rows if r["expected_verdict"] == BIND]
    did_bind = [(r, d) for r, d in rows if d.verdict == BIND]
    correct = [1 for r, d in did_bind
               if r["expected_verdict"] == BIND and d.nes_id == r["expected_nes_id"]]
    precision = len(correct) / len(did_bind) if did_bind else 1.0
    recall = len(correct) / len(should_bind) if should_bind else 1.0
    print(f"\nlabelled set: {len(rows)} names, {len(should_bind)} should bind")
    print(f"bound {len(did_bind)}, correct {len(correct)}")
    print(f"precision {precision:.3f}  recall {recall:.3f}")
    # Precision is the gate. Recall is reported, never asserted upward — the
    # threshold is not tuned to raise it.
    assert precision == 1.0
