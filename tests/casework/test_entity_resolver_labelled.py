"""Precision and recall against the labelled set, and the zero-false-positive gate.

A false positive here is a wrong bind: a named individual publicly attached to a
corruption case they had nothing to do with. One is a build failure.

The fixtures and the labelling method are described in `fixtures/README.md`. The
labels come from evidence `resolve` cannot read -- the full prod entity document
and the binds already on the source case -- so this measures the resolver rather
than mirroring it.

TWO PIPELINES ARE MEASURED HERE, AND THEY DO NOT SCORE THE SAME. Read which is
which before quoting a number out of this file.

  RESOLVER-ONLY (`_decisions`) is the matcher's ceiling: `resolve` matches names,
  then `apply_document_veto` reads the bound entity's document and refuses
  Election Commission candidate records. This measures how good the name matcher
  can be. It is NOT what ships.

  PRODUCTION (`_production_plans`) runs every row through
  `casework.enrich_related_entities.plan_case_entities`, which is what the
  enricher actually calls. It inserts a step the resolver-only path has no idea
  about: a name is a bind candidate only when its extracted `relationship_type`
  is `related`, and a `location`-typed extraction is sent to review BEFORE any
  search happens. Every municipality in the labelled set is location-typed --
  `SYSTEM_PROMPT` Part 1 tells the LLM to emit them that way -- so production
  refuses a fifth of the rows the matcher binds.

Both figures are legitimate and both are printed with their name attached. The
one to quote when asking "what does the shipped enricher achieve" is the
production one. The earlier version of this file measured only the resolver and
reported its recall unqualified, which read as the pipeline's.
"""
import json
from functools import lru_cache
from pathlib import Path

from casework.enrich_related_entities import plan_case_entities
from casework.entity_resolver import (
    BIND,
    NO_MATCH,
    REVIEW,
    apply_document_veto,
    resolve,
)

FIXTURES = Path(__file__).parent / "fixtures"


# Cached: `entity_candidates.json` is 63k lines and several tests below walk the
# whole labelled set twice each. No test mutates what these return.
@lru_cache(maxsize=1)
def load_labels():
    return [json.loads(line) for line in
            (FIXTURES / "entity_labels.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()]


@lru_cache(maxsize=1)
def load_candidates():
    return json.loads((FIXTURES / "entity_candidates.json").read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
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


class _FrozenApi:
    """`CaseworkApi`'s two read methods, served from the frozen captures.

    `plan_case_entities` only ever calls `search_entities` and `get_entity`, so
    the production path can be measured offline and deterministically against the
    same two files the resolver-only path uses. A missing document raises, which
    is what we want: the gate should fail loudly rather than quietly measure the
    fail-closed branch of the document veto.
    """

    def __init__(self, candidates, documents):
        self._candidates = candidates
        self._documents = documents

    def search_entities(self, name):
        return self._candidates.get(name, [])

    def get_entity(self, nes_id):
        return self._documents[nes_id]


# `plan_case_entities` refuses any case that is not DRAFT and any payload with no
# `entities` key, both before it looks at a single name. One synthetic case per
# row, with no existing binds, so the already-bound skip never interferes and
# each row's plan stands alone.
_SYNTHETIC_CASE = {"slug": "labelled-set", "state": "DRAFT", "entities": []}


def extracted_relationship_type(row):
    """The `relationship_type` the extractor emits for this row, in production.

    Derived from the labelled target, not guessed: `SYSTEM_PROMPT` Part 1 tells
    the LLM to emit districts, municipalities and provinces as
    `relationship_type="location"`, and Part 2 covers everything else as
    `"related"`. So a row whose labelled bind target is a `location/*` entity is
    a location-typed extraction in production.

    Rows with no target (REVIEW / NO_MATCH labels) are typed `related`, which is
    the CONSERVATIVE direction for a precision gate: `related` is the permissive
    path, so those rows get the full chance to produce a wrong bind rather than
    being refused before the search.
    """
    target = row.get("expected_nes_id") or ""
    return "location" if "/entity/location/" in target else "related"


def _production_plans():
    """Every labelled row through `plan_case_entities` -- the shipped pipeline.

    One row per plan, so `plan_case_entities`' already-bound dedupe cannot drop
    the second of two rows that resolve to the same IRI (`सोरु गाउँपालिका` and
    `सोरु गाउँपालिका, मुगु` both target one municipality).
    """
    api = _FrozenApi(load_candidates(), load_documents())
    for row in load_labels():
        item = {"entity_name": row["extracted"],
                "relationship_type": extracted_relationship_type(row),
                "notes": "measured by the labelled gate"}
        yield row, item, plan_case_entities(api, _SYNTHETIC_CASE, "W/\"labelled\"", [item])


# The labelled set's exact size. Asserted, not inferred, because
# `entity_labels.jsonl` is regenerated by a gitignored script: a partial write --
# or someone deleting the six ECN rows to clear a red build -- would otherwise
# leave both gate tests passing and printing "precision 1.000" over a set that
# proves nothing. Change this only alongside a deliberate change to the fixture.
EXPECTED_LABEL_COUNT = 142
# The six rows the document veto exists for. Named individually so removing one
# is a test failure and not a silent improvement in the numbers. Each is the only
# NES entity with that name, and each is an Election Commission 2079 candidate
# record for a ward the case has nothing to do with.
ECN_VETO_ROWS = (
    "टोपेन्द्र खनाल",
    "नन्दलाल दास",
    "याङजी शेर्पा",
    "राज बहादुर बम",
    # nec-candidate-id, an ELECTED ward head rather than a candidate. The veto
    # keyed on ecn-candidate-id alone until 2026-08-03 and let this through.
    "तेजनाथ पौडेल",
)
# `मिङमा ल्हमु शेर्पा` used to sit in ECN_VETO_ROWS above, and it is deliberately
# no longer there. It no longer reaches BIND at all: the extraction spells it
# ल्हमु where NES stores ल्हामु, and an inserted ा stopped folding when
# romanisation was restricted to cross-script matching (see `tokens_equal`).
# It is pinned below rather than deleted, so that the day someone widens the fold
# again this gate says so instead of quietly regaining a bind.
#
# Honest cost: the document veto now has FIVE rows exercising it, not six.
NO_LONGER_REACHES_THE_DOCUMENT_VETO = (
    "मिङमा ल्हमु शेर्पा",
)
# Rows the PROVINCE veto holds: the candidate IRI asserts a province the extracted
# name never mentions. Separate from ECN_VETO_ROWS because a different veto fires,
# and pure -- no document is consulted.
PROVINCE_VETO_ROWS = (
    "वन तथा वातावरण मन्त्रालय",
)
# Rows the UNQUALIFIED-INSTITUTION veto holds: an institution-type name that NES
# also holds with a locality appended, so the record it matches is the
# district-less bucket. Structural, and pure -- no document is consulted.
#
# This row is LABELLED BIND and is deliberately no longer bound. The label rests
# on a human's bind for one specific case; the resolver would produce the same
# bind for a case in any other district, and Nepal has one land-revenue office
# per district. Costing this row is why resolver-only recall is 0.846 and not
# 0.872.
BUCKET_VETO_ROWS = (
    "मालपोत कार्यालय",
)
# Rows production refuses BEFORE searching, because the extractor types them
# `location` and `plan_case_entities` only ever binds `related`. Named so the
# gate breaks if someone widens that allow-list without re-reading the product
# decision behind it -- whether to bind locations is open and out of scope here.
# All nine are `location/*` bind targets; the resolver binds seven of them.
LOCATION_TYPED_ROWS = (
    "अदानचुली गाउँपालिका",
    "छायाँनाथ रारा नगरपालिका",
    "तामाकोशी गाउँपालिका",
    "नवराजपुर गाउँपालिका",
    "नागार्जुन नगरपालिका - काठमाडौं",
    "रामधुनी नगरपालिका",
    "शारदा नगरपालिका",
    "सोरु गाउँपालिका",
    "सोरु गाउँपालिका, मुगु",
)


def test_the_labelled_set_is_whole():
    """Guards every other test in this file. A gate that cannot fail is not a gate."""
    rows = load_labels()
    assert len(rows) == EXPECTED_LABEL_COUNT, (
        f"expected {EXPECTED_LABEL_COUNT} labelled rows, found {len(rows)} -- the "
        "fixture is truncated, and the precision below would be measured over a "
        "subset")

    extracted = {row["extracted"] for row in rows}
    assert len(extracted) == EXPECTED_LABEL_COUNT, "duplicate rows in the labelled set"

    missing = [name for name in
               ECN_VETO_ROWS + PROVINCE_VETO_ROWS + BUCKET_VETO_ROWS + LOCATION_TYPED_ROWS
               if name not in extracted]
    assert not missing, (
        f"veto regression rows are gone from the labelled set: {missing}. These are "
        "the namesake election records, province-scoped bodies and unqualified "
        "institution buckets the vetoes exist to refuse, plus the location-typed "
        "rows production refuses before searching; without them the gate no longer "
        "tests them")

    # LOCATION_TYPED_ROWS is derived, not hand-maintained: it must be exactly the
    # rows `extracted_relationship_type` types `location`, or the production
    # measurement below is scoring a different population than it names.
    typed = {row["extracted"] for row in rows
             if extracted_relationship_type(row) == "location"}
    assert typed == set(LOCATION_TYPED_ROWS), (
        "LOCATION_TYPED_ROWS no longer matches the labelled targets: "
        f"only in the list {set(LOCATION_TYPED_ROWS) - typed}, only in the "
        f"fixture {typed - set(LOCATION_TYPED_ROWS)}")

    # Every row carries the four fields the gate reads, and an audit trail.
    for row in rows:
        assert row.get("provenance"), f"{row['extracted']!r} has no provenance"
        assert row["expected_verdict"] in (BIND, "REVIEW", "NO_MATCH"), row
        if row["expected_verdict"] == BIND:
            assert row["expected_nes_id"], f"{row['extracted']!r} binds to nothing"
        else:
            assert row["expected_nes_id"] is None, (
                f"{row['extracted']!r} is not a BIND but carries an id")


def test_the_gate_actually_exercises_binding():
    """The set must contain rows that reach BIND, and rows the veto downgrades.

    Without this, a labelled set of nothing but NO_MATCH rows scores a perfect
    precision of 1.000 and the gate is decoration.
    """
    rows = list(_decisions())
    assert len(rows) == EXPECTED_LABEL_COUNT

    bound = [r for r, d in rows if d.verdict == BIND]
    assert len(bound) >= 30, f"only {len(bound)} rows reached BIND; the gate is vacuous"

    should_bind = [r for r, _ in rows if r["expected_verdict"] == BIND]
    assert len(should_bind) >= 30, f"only {len(should_bind)} rows are labelled BIND"

    # Every ECN row must be present AND actually downgraded by the veto — proof
    # the veto is load-bearing rather than a no-op that happens to look right.
    by_name = {r["extracted"]: d for r, d in rows}
    for name in ECN_VETO_ROWS:
        assert name in by_name, f"{name!r} missing from the measured set"
        assert by_name[name].verdict == REVIEW, (
            f"{name!r} was not downgraded — the document veto is not firing")
        assert by_name[name].nes_id is None

    # Pinned so a widened fold cannot silently turn this back into a bind.
    for name in NO_LONGER_REACHES_THE_DOCUMENT_VETO:
        assert name in by_name, f"{name!r} missing from the measured set"
        assert by_name[name].verdict == NO_MATCH, (
            f"{name!r} reached {by_name[name].verdict} -- it should not score at "
            "all. If the matra fold was widened, re-check that कमल थापा still "
            "cannot bind a कमला थापा entity before accepting this.")

    # The province veto is pure, so it must fire inside resolve() itself and name
    # the province, otherwise a caseworker cannot tell what to check.
    for name in PROVINCE_VETO_ROWS:
        assert name in by_name, f"{name!r} missing from the measured set"
        decision = by_name[name]
        assert decision.verdict == REVIEW, (
            f"{name!r} was not held — the province veto is not firing")
        assert decision.nes_id is None
        assert "province" in decision.reason, decision.reason

    # The unqualified-institution veto is pure and structural, so it must fire
    # inside resolve() and name the qualified sibling the caseworker should add.
    for name in BUCKET_VETO_ROWS:
        assert name in by_name, f"{name!r} missing from the measured set"
        decision = by_name[name]
        assert decision.verdict == REVIEW, (
            f"{name!r} was not held — the unqualified-institution veto is not firing")
        assert decision.nes_id is None
        assert "unqualified institution name" in decision.reason, decision.reason


def test_zero_false_positives_across_the_labelled_set():
    rows = list(_decisions())
    assert len(rows) == EXPECTED_LABEL_COUNT, "labelled set truncated"

    false_positives = []
    for row, decision in rows:
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


def _score(label, should_bind, bound_pairs):
    """Print one named precision/recall pair and return it.

    `label` is not decoration. Two pipelines are measured in this file and they
    score differently, so every printed figure carries the name of the pipeline
    it describes -- see the module docstring.
    """
    correct = [1 for expected, got in bound_pairs if expected == got]
    # No `or 1.0` fallbacks: an empty numerator or denominator here means the
    # fixture is broken, and a division error is a better outcome than a
    # confident 1.000 over nothing.
    precision = len(correct) / len(bound_pairs)
    recall = len(correct) / should_bind
    print(f"\n{label}")
    print(f"  {should_bind} labelled BIND rows; bound {len(bound_pairs)}, "
          f"correct {len(correct)}")
    print(f"  precision {precision:.3f}  recall {recall:.3f}")
    return precision, recall


def test_resolver_only_precision_and_recall_are_reported():
    """The MATCHER'S CEILING, not the shipped pipeline.

    `resolve` + `apply_document_veto` over all 142 rows. This figure ignores the
    `related`-only allow-list in `plan_case_entities`, so it counts binds
    production refuses before it searches. Quote
    `test_production_precision_and_recall_are_reported` for what ships.
    """
    rows = list(_decisions())
    assert len(rows) == EXPECTED_LABEL_COUNT, "labelled set truncated"
    should_bind = sum(1 for r, _ in rows if r["expected_verdict"] == BIND)
    bound_pairs = [(r["expected_nes_id"] if r["expected_verdict"] == BIND else None,
                    d.nes_id)
                   for r, d in rows if d.verdict == BIND]
    precision, _recall = _score(
        "RESOLVER-ONLY (matcher ceiling; NOT what production runs)",
        should_bind, bound_pairs)
    # Precision is the gate. Recall is reported, never asserted upward — the
    # threshold is not tuned to raise it.
    assert precision == 1.0


def test_production_precision_and_recall_are_reported():
    """WHAT SHIPS: every row through `plan_case_entities`.

    Lower recall than the resolver-only figure, by design and not by accident:
    the `related`-only allow-list refuses every location-typed extraction before
    searching, and the truncation guard downgrades a bind whose candidate list hit
    the search page cap. Both are precision-protecting refusals, so precision is
    still the gate here.
    """
    plans = list(_production_plans())
    assert len(plans) == EXPECTED_LABEL_COUNT, "labelled set truncated"
    should_bind = sum(1 for r, _i, _p in plans if r["expected_verdict"] == BIND)
    bound_pairs = [
        (row["expected_nes_id"] if row["expected_verdict"] == BIND else None,
         decision.nes_id)
        for row, _item, plan in plans
        for _name, decision, _notes in plan.bound
    ]
    precision, _recall = _score(
        "PRODUCTION (plan_case_entities -- what the enricher actually runs)",
        should_bind, bound_pairs)
    assert precision == 1.0


def test_a_location_typed_extraction_never_reaches_a_bind():
    """Pins the refusal the production figure rests on.

    `plan_case_entities` binds only a `related` extraction, and sends a `location`
    one to review before spending a search request. Nothing else in this suite
    exercises that allow-list, so without this test the gate could not fail the
    day someone widened it -- and the production recall printed above would
    quietly become wrong.

    Whether to bind locations is an open product decision. This test does not
    settle it; it makes changing the answer a deliberate act.
    """
    refused = 0
    for row, item, plan in _production_plans():
        if item["relationship_type"] != "location":
            continue
        refused += 1
        assert not plan.bound, (
            f"{row['extracted']!r} is a location-typed extraction and it REACHED A "
            f"BIND ({plan.bound[0][1].nes_id}). The `related`-only allow-list in "
            "plan_case_entities was widened. That is a product decision about "
            "binding places to corruption cases, not a bug fix -- if it was "
            "deliberate, re-measure the production figures and update this test.")
        assert plan.patch_items == [], (
            f"{row['extracted']!r} produced a write payload despite being refused")
        assert plan.action == "NOOP"
        reasons = [decision.reason for _name, decision in plan.review]
        assert any("out of bind scope" in reason for reason in reasons), (
            f"{row['extracted']!r} was not reported for review with the "
            f"out-of-scope reason: {reasons}")
    assert refused == len(LOCATION_TYPED_ROWS), (
        f"expected {len(LOCATION_TYPED_ROWS)} location-typed rows, exercised {refused}")


def test_the_production_path_refuses_a_strict_subset_of_resolver_binds():
    """The two figures differ only by refusals, never by a bind production adds.

    If production ever bound something the resolver-only path did not, the
    resolver-only zero-false-positive gate above would stop covering production
    and this file would need a second one.
    """
    resolver_bound = {row["extracted"] for row, decision in _decisions()
                      if decision.verdict == BIND}
    production_bound = {row["extracted"] for row, _item, plan in _production_plans()
                        if plan.bound}
    assert production_bound <= resolver_bound, (
        "production bound names the resolver-only path did not: "
        f"{production_bound - resolver_bound}")
