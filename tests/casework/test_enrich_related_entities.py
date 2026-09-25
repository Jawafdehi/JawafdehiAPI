"""Tests for the DB-free related-entities enricher
(casework/enrich_related_entities.py): LLM extraction, deterministic NES
resolution (`casework/entity_resolver.py`), and the conditional bind write.

ARCHITECTURE FINDING (see module docstring for the full writeup, escalated and
confirmed with the dispatcher before any code was written): the donor
(0321a85) writes entities via `api.create_entity(display_name=name, nes_id="")`
-- a method that does not exist on this branch's `CaseworkApi` and never has.
The CURRENT schema (`cases/caseworker_serializers.py::EntityPatchItemSerializer`)
requires `{"nes_id": <canonical NES @id IRI>, "relationship_type", "outcome"?,
"notes"}` and explicitly has "no display-name fallback". This port's resolver
turns an LLM-extracted name into a confirmed `nes_id` deterministically (no
fuzzy matching, no LLM call, `MIN_BIND_SCORE = 0.85`) and only ever binds an
entity NES already has -- an unmatched name is reported, never minted.

BRIEF-VS-DONOR DIFFERENCE: the brief's suggested `validate_entity_item`
function (canonical `nes_id` + accused-only `outcome` validation) does not
exist anywhere in the donor -- it matches the CURRENT serializer, not any
donor behavior, so it is NOT implemented here (`test_donor_never_defines_
validate_entity_item`). Same phantom-function shape as `normalise_missing_
details` (14b) and `validate_timeline_items` (14c).

`TestDonorFidelity` re-derives the `tier`/`max_tokens` LLM-call arguments
directly from the donor at commit `0321a85` (via `git show` + `ast`, never by
trusting this file's own transcription).
"""
import ast
import inspect
import json
import logging
import subprocess
import sys
import types
import urllib.error
from pathlib import Path

import pytest

from casework import enrich_related_entities as ere
from casework.common.api import CandidateList, ENTITY_SEARCH_MAX_PAGES, ENTITY_SEARCH_PAGE_SIZE
from casework.common.api import EntityAlreadyExists
from casework.common.order_windows import MAX_ENTITY_WINDOWS, start_windows
from casework.enrich_related_entities import (
    PROMOTED_PREFIX,
    Extraction,
    Source,
    accused_missing_notes,
    current_entity_binds,
    extract_from_source,
    is_promoted,
    merge_entity_binds,
    parse_window_response,
    pick_source,
    plan_case_entities,
    validate_bind_item,
)
from casework.location_gazetteer import Gazetteer, LocationBind
from tests.casework.fakes import FakeUsage
from tests.casework.test_location_gazetteer import DISTRICTS, UNITS

REPO_ROOT = Path(__file__).resolve().parents[2]
DONOR_COMMIT = "0321a85"


def _donor_source() -> str:
    proc = subprocess.run(
        ["git", "show", f"{DONOR_COMMIT}:casework/enrich_related_entities.py"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        pytest.skip(
            f"donor commit {DONOR_COMMIT} not in local history "
            "(shallow clone?); fidelity check needs full git history")
    return proc.stdout


def _donor_invoke_text_kwargs() -> dict:
    """Find the donor's `invoke_text(...)` call and extract its literal
    `tier`/`max_tokens` keyword arguments via AST (donor line ~416-423)."""
    tree = ast.parse(_donor_source())
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "invoke_text"
        ):
            return {
                kw.arg: ast.literal_eval(kw.value)
                for kw in node.keywords
                if kw.arg in ("tier", "max_tokens")
            }
    raise AssertionError("donor never calls invoke_text(...)")


class TestDonorFidelity:
    """Pins against the donor at commit 0321a85 -- NOT this module's own transcription."""

    def test_invoke_text_tier_matches_donor_and_max_tokens_deliberately_does_not(self):
        # Pins donor line ~421 `tier="premium"` and line ~420 `max_tokens=2000`.
        donor_kwargs = _donor_invoke_text_kwargs()
        assert donor_kwargs["tier"] == "premium"
        assert donor_kwargs["max_tokens"] == 2000
        # The port deliberately does NOT keep the donor's 2000. At that cap the
        # claude CLI aborts the call with "response exceeded the 2000 output token
        # maximum" on a multi-defendant case -- reproduced on 078-CR-0001 with
        # sonnet. 2000 stopped being a budget and became a failure, once the
        # extraction asked for five sections of Devanagari names and notes instead
        # of two. Pinned here, in the class that exists to catch silent drift, so
        # the divergence stays a decision with a reason attached.
        assert ere.EXTRACTION_MAX_TOKENS == 8000
        assert ere.EXTRACTION_MAX_TOKENS > donor_kwargs["max_tokens"]
        # And that this port's tier_for("entities") resolves to the same
        # value (casework/common/llm.py's own pin, cross-checked here).
        from casework.common.llm import tier_for

        assert tier_for("entities") == donor_kwargs["tier"]

    def test_donor_never_defines_validate_entity_item(self):
        # Pins the brief-vs-donor finding: the donor never defines this
        # function (or the string) anywhere in its source.
        assert "validate_entity_item" not in _donor_source()

    def test_donor_write_path_uses_create_entity_with_blank_nes_id(self):
        # Documents exactly why the donor's write path cannot be reused
        # as-is: it mints a brand new entity with a blank nes_id, not a
        # resolved canonical NES @id IRI.
        source = _donor_source()
        assert 'api.create_entity(display_name=name, nes_id="")' in source

    def test_the_donor_create_entity_call_is_still_impossible_to_make(self):
        # `CaseworkApi` DOES have `create_entity` now -- the --create-entities
        # step needs it. The donor's CALL remains impossible, which is what this
        # guard was always about: it minted an entity from a `display_name` and a
        # blank `nes_id`, with no prefix, no type and therefore no IRI. Ours takes
        # the API's authoring payload and the IRI comes from prefix+slug, so the
        # donor's signature raises instead of quietly creating a nameless entity.
        import inspect

        params = inspect.signature(ere.CaseworkApi.create_entity).parameters
        assert "display_name" not in params
        assert "nes_id" not in params
        assert "payload" in params


# --------------------------------------------------------------------------
# main() -- integration over a stubbed API + LLM. NEVER writes.
# --------------------------------------------------------------------------

PRESS_ONLY_CASE = {
    "slug": "case-press-only",
    "title": "प्रेस विज्ञप्ति मात्र भएको मुद्दा",
    "state": "DRAFT",
    "evidence": [
        {"material_iri": "https://jawafdehi.org/material/ciaa/press_releases/1",
         "material": {"material_type": "press_release", "urls": [
             {"link": "https://x/press.md", "role": "MARKDOWN"}]}},
    ],
}

COURT_ONLY_CASE = {
    "slug": "case-court-only",
    "title": "अदालतको आदेश मात्र भएको मुद्दा",
    "state": "DRAFT",
    "evidence": [
        {"material_iri": "https://jawafdehi.org/material/ngm/court_orders/1",
         "material": {"material_type": "court_order", "urls": [
             {"link": "https://x/court.md", "role": "MARKDOWN"}]}},
    ],
}

BOTH_CASE = {
    "slug": "case-both",
    "title": "प्रेस विज्ञप्ति र अदालतको आदेश दुबै भएको मुद्दा",
    "state": "DRAFT",
    "evidence": [
        {"material_iri": "https://jawafdehi.org/material/ciaa/press_releases/2",
         "material": {"material_type": "press_release", "urls": [
             {"link": "https://x/press2.md", "role": "MARKDOWN"}]}},
        {"material_iri": "https://jawafdehi.org/material/ngm/court_orders/2",
         "material": {"material_type": "court_order", "urls": [
             {"link": "https://x/court2.md", "role": "MARKDOWN"}]}},
    ],
}

NEITHER_CASE_UNCONVERTED = {
    "slug": "case-neither",
    "title": "कुनै रूपान्तरित सामग्री नभएको मुद्दा",
    "state": "DRAFT",
    "evidence": [
        {"material_iri": "https://jawafdehi.org/material/ciaa/press_releases/3",
         "material": {"material_type": "press_release", "urls": [
             {"link": "https://x/raw.pdf", "role": "RAW"}]}},
    ],
}

#: A coded district bind: with a `related` bind it makes a case `already_enriched`.
ALREADY_BOUND_DISTRICT = {"nes_id": "https://jawafdehi.org/entity/location/district/surkhet-np0605",
                          "type": "location", "notes": ""}

PRESS_CASE_ALREADY_POPULATED = {
    "slug": "case-populated",
    "title": "पहिल्यै entities भरिएको मुद्दा",
    "state": "DRAFT",
    # "type", not "relationship_type" -- the production read shape
    # (cases/services/nes_resolver.py via CaseSerializer.get_entities) sends
    # the relationship type back under "type"; "relationship_type" is a
    # write-only key that never appears on a read.
    "entities": [
        {"nes_id": "https://nes.jawafdehi.org/entity/1",
         "type": "related", "notes": "पहिल्यै बाँधिएको"},
        ALREADY_BOUND_DISTRICT,
    ],
    "evidence": [
        {"material_iri": "https://jawafdehi.org/material/ciaa/press_releases/5",
         "material": {"material_type": "press_release", "urls": [
             {"link": "https://x/press5.md", "role": "MARKDOWN"}]}},
    ],
}

PRESS_ONLY_EMPTY_MARKDOWN_CASE = {
    "slug": "case-empty-markdown",
    "title": "खाली मार्कडाउन भएको मुद्दा",
    "state": "DRAFT",
    "evidence": [
        {"material_iri": "https://jawafdehi.org/material/ciaa/press_releases/4",
         "material": {"material_type": "press_release", "urls": [
             {"link": "https://x/empty.md", "role": "MARKDOWN"}]}},
    ],
}


class _StubApi:
    """Call-tracking (never raising) fake -- proves patch_field/replace_list
    are genuinely never invoked, rather than merely never raising."""

    def __init__(self, cases):
        self._cases = {c["slug"]: dict(c) for c in cases}
        self.patch_calls = []
        self.replace_list_calls = []

    def iter_cases(self, params=None, timeout=60):
        yield from self._cases.values()

    def get_case(self, slug, timeout=60):
        return self._cases[slug]

    def patch_field(self, slug, field, value, timeout=60):
        self.patch_calls.append((slug, field, value))
        return {}

    def replace_list(self, slug, path, items, timeout=60):
        self.replace_list_calls.append((slug, path, items))
        return {}


#: The sentence every `patched_fetch_markdown` text carries, so `with_evidence` can
#: ground a canned reply against whichever source `pick_source` chooses.
FIXTURE_EVIDENCE = "गोपाल बहादुर श्रेष्ठविरुद्ध मुद्दा दर्ता भएको छ।"
#: Pads a press release past `is_teaser`'s 400-char floor.
PRESS_PADDING = " आयोगले यस विषयमा विस्तृत अनुसन्धान गरेको थियो।" * 10


@pytest.fixture
def patched_fetch_markdown(monkeypatch):
    import casework.common.materials as m

    def fake_fetch(link, timeout=60):
        return {
            "https://x/press.md": "साझा भण्डार सहकारीमा अनियमितता भएको छ। "
                                   + FIXTURE_EVIDENCE + PRESS_PADDING,
            "https://x/court.md": "अदालतको आदेशमा ठहर खण्ड उल्लेख छ। " + FIXTURE_EVIDENCE,
            "https://x/press2.md": "प्रेस विज्ञप्तिको सामग्री। " + FIXTURE_EVIDENCE + PRESS_PADDING,
            "https://x/court2.md": "अदालतको आदेशको सामग्री। " + FIXTURE_EVIDENCE,
            "https://x/empty.md": "",
            "https://x/press5.md": ("पहिल्यै भरिएको मुद्दाको प्रेस विज्ञप्ति। "
                                    + FIXTURE_EVIDENCE + PRESS_PADDING),
        }.get(link, "")

    monkeypatch.setattr(m, "fetch_markdown", fake_fetch)


def _stub_gazetteer_loader():
    """A `load_gazetteer` stand-in over the ten test districts, counting its calls."""
    calls = []

    def load(api):
        calls.append(api)
        return Gazetteer(DISTRICTS, UNITS)

    load.calls = calls
    return load


def with_evidence(reply: str, evidence: str) -> str:
    """Add `evidence` to every entities/accused_notes item of a canned extraction reply."""
    data = json.loads(reply)
    for key in ("entities", "accused_notes"):
        for item in data.get(key) or []:
            item.setdefault("evidence", evidence)
    return json.dumps(data, ensure_ascii=False)


def _run_main(monkeypatch, api, invoke_text_stub, argv, gazetteer_loader=None,
              real_gazetteer=False):
    """Drive `main()` end to end with a stubbed API and a stubbed LLM call."""
    monkeypatch.setattr(ere, "build_api", lambda args: api)
    monkeypatch.setattr(ere, "bootstrap", lambda *a, **k: None)
    if not real_gazetteer:
        monkeypatch.setattr(ere, "load_gazetteer", gazetteer_loader or _stub_gazetteer_loader())

    fake_llm_invoke = types.ModuleType("llm.invoke")
    fake_llm_invoke.invoke_text = invoke_text_stub

    fake_llm_usage = types.ModuleType("llm.usage")
    fake_llm_usage.UsageAccumulator = FakeUsage
    fake_llm_usage.render_usage_table = lambda by_provider, title=None: ""

    monkeypatch.setitem(sys.modules, "llm.invoke", fake_llm_invoke)
    monkeypatch.setitem(sys.modules, "llm.usage", fake_llm_usage)

    report = ere.main(argv)
    return report


def _call_tracking_stub(response=None):
    """A stub that records invocations instead of raising -- an "LLM must not
    be called" assertion must check `stub.calls == []` explicitly rather than
    relying on a raise, per this project's own documented trap: a raise from
    a case that legitimately reaches the LLM call would be swallowed by the
    per-case `except Exception` and counted as an "error" status instead of
    failing the test loudly."""
    if response is None:
        response = json.dumps({"entities": [], "accused_notes": []})
    calls = []

    def stub(**kw):
        calls.append(kw)
        return response

    stub.calls = calls
    return stub


ENTITY_RESPONSE = with_evidence(json.dumps({
    "entities": [
        {"entity_name": "साझा भण्डार सहकारी", "relationship_type": "related",
         "notes": "ठेक्का प्राप्त गर्ने संस्था"},
        {"entity_name": "सुर्खेत जिल्ला", "relationship_type": "location", "notes": ""},
    ],
    "accused_notes": [
        {"name": "गोपाल बहादुर श्रेष्ठ", "notes": "तत्कालीन अध्यक्ष"},
    ],
}), FIXTURE_EVIDENCE)


def test_unmet_prerequisite_is_recorded_not_silently_skipped(
    monkeypatch, patched_fetch_markdown
):
    api = _StubApi([NEITHER_CASE_UNCONVERTED])
    stub = _call_tracking_stub()
    report = _run_main(monkeypatch, api, invoke_text_stub=stub, argv=["--dry-run"])
    assert report.rows[0]["status"] == "unmet"
    assert report.rows[0]["reason"]
    assert stub.calls == []


def test_empty_markdown_after_satisfied_prerequisite_is_recorded_unmet(
    monkeypatch, patched_fetch_markdown
):
    # The MARKDOWN link exists (unmet_prerequisites is satisfied) but the
    # fetched content is blank -- must still be reported, never silently
    # treated as "no work to do".
    api = _StubApi([PRESS_ONLY_EMPTY_MARKDOWN_CASE])
    stub = _call_tracking_stub()
    report = _run_main(monkeypatch, api, invoke_text_stub=stub, argv=["--dry-run"])
    assert report.rows[0]["status"] == "unmet"
    assert stub.calls == []


def test_already_populated_case_is_skipped_without_calling_llm(
    monkeypatch, patched_fetch_markdown
):
    # Finding 2: of the five ported enrichers, this was the only one missing
    # the already-populated skip -- every run re-spent a premium-tier LLM
    # call on cases whose `entities` were already set. Assert on a
    # call-count spy (`stub.calls`), never on a raise: main()'s per-case
    # `except Exception` around the LLM call would otherwise swallow a stub
    # that incorrectly DID get invoked and raised, making a "must not call
    # the LLM" assertion pass vacuously.
    api = _StubApi([PRESS_CASE_ALREADY_POPULATED])
    stub = _call_tracking_stub()
    report = _run_main(monkeypatch, api, invoke_text_stub=stub, argv=["--dry-run"])
    assert report.rows[0]["status"] == "already"
    assert stub.calls == []


def test_force_reruns_an_already_populated_case_and_calls_the_llm(
    monkeypatch, patched_fetch_markdown
):
    # The other half of Finding 2: --force must actually override the skip,
    # not be a silent no-op. Assert the LLM WAS called (call-count spy), and
    # that the case proceeds all the way to resolution. No search result is
    # configured for the 'related' name, so it is a no-match; the 'location'
    # one goes through the gazetteer, fails grounding (its quote does not name
    # the place) and lands in `*.dropped.jsonl` without ever being searched.
    # Neither is a new write, hence a NOOP, but a NOOP reached AFTER the LLM ran
    # rather than by the pre-LLM skip.
    api = _SearchStubApi([PRESS_CASE_ALREADY_POPULATED])
    stub = _call_tracking_stub(ENTITY_RESPONSE)
    report = _run_main(
        monkeypatch, api, invoke_text_stub=stub, argv=["--force", "--dry-run"])
    assert len(stub.calls) == 1
    assert report.rows[0]["status"] == "already"
    assert report.rows[0]["reason"] == "0 for review, 1 no match"
    assert api.search_calls == ["साझा भण्डार सहकारी"]
    assert [r["place"] for r in _jsonl("dropped")] == ["सुर्खेत जिल्ला"]


def test_pre_llm_skip_keys_on_a_related_bind_not_any_bind(
    monkeypatch, patched_fetch_markdown
):
    # Measured on production: 162 of 3,003 cases carry at least one bind but
    # not one of them 'related' -- a bare `case.get("entities")` test skips
    # every one of those forever. A case bound solely to a 'location' must
    # still reach the LLM; a case that already carries a 'related' bind must
    # not (that would re-spend a premium-tier call on every run).
    #
    # "type", not "relationship_type": that is the PRODUCTION read shape
    # (cases/services/nes_resolver.py via CaseSerializer.get_entities) --
    # `relationship_type` is a write-only key `validate_bind_item` builds and
    # never appears coming back from a read. A version of this test that used
    # `relationship_type` for both fixtures passed against a skip filtered on
    # that same wrong key, hiding a real regression (every case would have
    # burned a premium LLM call, worse than the shape-agnostic check this
    # amendment replaced) -- see `test_pre_llm_skip_also_tolerates_the_write_
    # shape_key` below for the `relationship_type` case.
    location_only = dict(PRESS_ONLY_CASE)
    location_only["slug"] = "case-location-only-bind"
    location_only["entities"] = [
        {"nes_id": "https://jawafdehi.org/entity/place/surkhet-district-abc123",
         "type": "location", "notes": ""}]

    related_bound = dict(COURT_ONLY_CASE)
    related_bound["slug"] = "case-related-bind"
    related_bound["entities"] = [
        {"nes_id": "https://jawafdehi.org/entity/organization/"
                   "sajha-bhandara-sahakari-9f9f9f",
         "type": "related", "notes": "ठेक्का प्राप्त गर्ने संस्था"},
        ALREADY_BOUND_DISTRICT]

    api = _SearchStubApi([location_only, related_bound])  # nothing resolves
    stub = _call_tracking_stub(ENTITY_RESPONSE)
    report = _run_main(monkeypatch, api, invoke_text_stub=stub, argv=["--dry-run"])

    # The whole point of the fix: the premium call now happens for the
    # location-only case. Assert on the mock LLM's call count, not only the
    # report status -- with no search results configured, the location-only
    # case's plan also ends as a no-op (nothing resolves), so "already" alone
    # cannot tell the two cases apart; the pre-LLM skip's own reason text can.
    assert len(stub.calls) == 1

    rows_by_slug = {r["slug"]: r for r in report.rows}
    assert "already present" not in rows_by_slug["case-location-only-bind"]["reason"]
    assert "already present" in rows_by_slug["case-related-bind"]["reason"]


def test_pre_llm_skip_also_tolerates_the_write_shape_key(
    monkeypatch, patched_fetch_markdown
):
    # A hand-built or legacy payload using "relationship_type" instead of the
    # real read shape's "type" must still be recognised -- same tolerance
    # `current_entity_binds` already applies.
    related_bound = dict(COURT_ONLY_CASE)
    related_bound["slug"] = "case-related-bind-write-shape"
    related_bound["entities"] = [
        {"nes_id": "https://jawafdehi.org/entity/organization/"
                   "sajha-bhandara-sahakari-9f9f9f",
         "relationship_type": "related", "notes": "ठेक्का प्राप्त गर्ने संस्था"},
        ALREADY_BOUND_DISTRICT]
    api = _SearchStubApi([related_bound])
    stub = _call_tracking_stub(ENTITY_RESPONSE)
    _run_main(monkeypatch, api, invoke_text_stub=stub, argv=["--dry-run"])
    assert stub.calls == []


def test_press_only_case_reaches_the_llm(monkeypatch, patched_fetch_markdown, capsys):
    stub = _call_tracking_stub(ENTITY_RESPONSE)
    api = _StubApi([PRESS_ONLY_CASE])
    _run_main(monkeypatch, api, invoke_text_stub=stub, argv=["--dry-run"])
    assert len(stub.calls) == 1
    # PRESS_ONLY_CASE carries no "entities" key at all, so plan_case_entities
    # refuses to plan a write for it (absent is not empty) and the case's
    # report status is "already" regardless of what was extracted -- the
    # extraction itself is what this test pins, via the run summary.
    out = capsys.readouterr().out
    assert "TOTAL entities extracted across all cases: 2" in out


def test_court_only_case_reaches_the_llm(monkeypatch, patched_fetch_markdown, capsys):
    stub = _call_tracking_stub(ENTITY_RESPONSE)
    api = _StubApi([COURT_ONLY_CASE])
    _run_main(monkeypatch, api, invoke_text_stub=stub, argv=["--dry-run"])
    assert len(stub.calls) == 1
    out = capsys.readouterr().out
    assert "TOTAL entities extracted across all cases: 2" in out


def test_llm_invoked_with_premium_tier_end_to_end(monkeypatch, patched_fetch_markdown):
    """Pins the donor's `tier="premium"` argument (enrich_related_entities.py:421)."""
    seen_tiers = []

    def stub(**kw):
        seen_tiers.append(kw.get("tier"))
        return ENTITY_RESPONSE

    api = _StubApi([PRESS_ONLY_CASE])
    _run_main(monkeypatch, api, invoke_text_stub=stub, argv=["--apply"])
    assert seen_tiers == ["premium"]


def test_llm_extraction_failure_is_recorded_as_error(monkeypatch, patched_fetch_markdown):
    def stub(**kw):
        raise RuntimeError("LLM provider unavailable")

    api = _StubApi([PRESS_ONLY_CASE])
    report = _run_main(monkeypatch, api, invoke_text_stub=stub, argv=["--apply"])
    assert report.rows[0]["status"] == "error"


def test_llm_returning_nothing_is_recorded_as_skipped(monkeypatch, patched_fetch_markdown):
    response = json.dumps({"other": "no entities or accused_notes key"})
    api = _StubApi([PRESS_ONLY_CASE])
    report = _run_main(monkeypatch, api, invoke_text_stub=lambda **kw: response,
                        argv=["--apply"])
    assert report.rows[0]["status"] == "skipped"


def test_dry_run_writes_nothing_but_prints_what_it_would_bind(
    monkeypatch, patched_fetch_markdown, capsys
):
    # PRESS_ONLY_CASE itself carries no "entities" key (an intentionally-
    # incomplete payload used elsewhere in this file to pin the "absent is
    # not empty" refusal) -- a real case detail always carries the key, so
    # this end-to-end write test needs a copy that does.
    case = dict(PRESS_ONLY_CASE, entities=[])
    api = _SearchStubApi(
        [case],
        {"साझा भण्डार सहकारी": [{"id": "https://jawafdehi.org/entity/organization/"
                                  "sajha-bhandara-sahakari-9f9f9f",
                                 "title": {"ne": "साझा भण्डार सहकारी"}, "score": 200.0}]})
    _run_main(monkeypatch, api, invoke_text_stub=lambda **kw: ENTITY_RESPONSE,
              argv=["--dry-run"])
    out = capsys.readouterr().out
    assert api.patch_calls == []
    assert api.replace_list_calls == []
    assert "साझा भण्डार सहकारी" in out
    assert "WOULD BIND" in out


def test_apply_writes_the_merged_list_with_if_match(
    monkeypatch, patched_fetch_markdown
):
    case = dict(PRESS_ONLY_CASE, entities=[])
    api = _SearchStubApi(
        [case],
        {"साझा भण्डार सहकारी": [{"id": "https://jawafdehi.org/entity/organization/"
                                  "sajha-bhandara-sahakari-9f9f9f",
                                 "title": {"ne": "साझा भण्डार सहकारी"}, "score": 200.0}]})
    _run_main(monkeypatch, api, invoke_text_stub=lambda **kw: ENTITY_RESPONSE,
              argv=["--apply"])
    assert len(api.replace_list_calls) == 1
    slug, path, items, if_match = api.replace_list_calls[0]
    assert (slug, path) == ("case-press-only", "entities")
    assert if_match == api.etag


def test_summary_reports_the_three_counts_separately(
    monkeypatch, patched_fetch_markdown, capsys
):
    api = _SearchStubApi([PRESS_ONLY_CASE], {})   # nothing resolves
    _run_main(monkeypatch, api, invoke_text_stub=lambda **kw: ENTITY_RESPONSE,
              argv=["--dry-run"])
    out = capsys.readouterr().out
    assert "TOTAL entities extracted across all cases: 2" in out
    assert "TOTAL that WOULD bind to an EXISTING NES entity (dry run, nothing written): 0" in out
    assert "TOTAL reported for human review:" in out
    assert "TOTAL with no NES match:" in out
    assert "bound zero" in out.lower()


def test_rerunning_on_an_already_bound_case_is_a_noop(
    monkeypatch, patched_fetch_markdown
):
    bound_case = dict(PRESS_ONLY_CASE)
    bound_case["slug"] = "case-already-bound"
    bound_case["entities"] = [
        {"nes_id": "https://jawafdehi.org/entity/organization/"
                   "sajha-bhandara-sahakari-9f9f9f",
         "type": "related", "notes": "ठेक्का प्राप्त गर्ने संस्था"}]
    api = _SearchStubApi(
        [bound_case],
        {"साझा भण्डार सहकारी": [{"id": "https://jawafdehi.org/entity/organization/"
                                  "sajha-bhandara-sahakari-9f9f9f",
                                 "title": {"ne": "साझा भण्डार सहकारी"}, "score": 200.0}]})
    _run_main(monkeypatch, api, invoke_text_stub=lambda **kw: ENTITY_RESPONSE,
              argv=["--apply", "--force"])
    assert api.replace_list_calls == []


def test_summary_uses_plan_summary_so_already_bound_names_do_not_vanish(
    monkeypatch, patched_fetch_markdown, capsys
):
    # `plan_case_entities` drops a resolved BIND whose nes_id is already on
    # the case from bound/review/nomatch alike (correct for a re-run -- there
    # is nothing new to write), so summing those three lists directly
    # silently undercounts the extracted names on a re-run. This is exactly
    # why `plan_summary` exists (Task 7); `main()` must call it rather than
    # summing `len(plan.bound)` etc. directly, or it re-ships the bug.
    sajha_iri = ("https://jawafdehi.org/entity/organization/"
                 "sajha-bhandara-sahakari-9f9f9f")
    case = dict(PRESS_ONLY_CASE)
    case["entities"] = [
        {"nes_id": sajha_iri, "relationship_type": "related",
         "notes": "ठेक्का प्राप्त गर्ने संस्था"}]
    response = with_evidence(json.dumps({
        "entities": [
            {"entity_name": "साझा भण्डार सहकारी", "relationship_type": "related",
             "notes": "ठेक्का प्राप्त गर्ने संस्था"},
            {"entity_name": "अंकुर खत्री", "relationship_type": "related",
             "notes": "घुस लेनदेनमा सहयोग"},
        ],
        "accused_notes": [],
    }), FIXTURE_EVIDENCE)
    api = _SearchStubApi(
        [case],
        {"साझा भण्डार सहकारी": [{"id": sajha_iri,
                                 "title": {"ne": "साझा भण्डार सहकारी"}, "score": 200.0}],
         "अंकुर खत्री": [{"id": "https://jawafdehi.org/entity/person/"
                          "amkura-khatri-2de9b3",
                         "title": {"ne": "अंकुर खत्री"}, "score": 190.0}]})
    # --force: the case already carries a 'related' bind, which would
    # otherwise trip the pre-LLM skip before extraction ever runs.
    _run_main(monkeypatch, api, invoke_text_stub=lambda **kw: response,
              argv=["--apply", "--force"])

    out = capsys.readouterr().out
    assert "TOTAL entities extracted across all cases: 2" in out
    assert "TOTAL bound to an EXISTING NES entity: 1" in out
    assert "TOTAL already bound (nothing to write): 1" in out
    assert "TOTAL reported for human review: 0" in out
    assert "TOTAL with no NES match: 0" in out


def test_invalid_relationship_type_is_excluded_from_extracted_count(
    monkeypatch, patched_fetch_markdown, capsys
):
    # The donor dropped anything that was not "location" or "related" here, and so
    # did this port until the binder learned every section. Both of these now
    # count: "accused" is a real section, and an UNKNOWN one is counted too, because
    # it reaches the planner and is recorded there as a review row. What must still
    # be dropped is an item with no name -- the planner skips those without
    # recording them anywhere, so counting one would corrupt `plan_summary`'s
    # already-bound subtraction.
    response = with_evidence(json.dumps({
        "entities": [
            {"entity_name": "गोपाल बहादुर", "relationship_type": "accused", "notes": "x"},
            {"entity_name": "सुर्खेत जिल्ला", "relationship_type": "location", "notes": ""},
            {"entity_name": "बुद्धि प्रसाद", "relationship_type": "organization",
             "notes": "unknown section, still counted"},
            {"entity_name": "   ", "relationship_type": "related", "notes": "no name"},
        ],
        "accused_notes": [],
    }), FIXTURE_EVIDENCE)
    api = _SearchStubApi([PRESS_ONLY_CASE])
    _run_main(monkeypatch, api, invoke_text_stub=lambda **kw: response,
              argv=["--dry-run"])
    out = capsys.readouterr().out
    assert "TOTAL entities extracted across all cases: 3" in out


def test_accused_notes_only_response_yields_zero_valid_entities_end_to_end(
    monkeypatch, patched_fetch_markdown, capsys
):
    # Exercises the shared-parser leak (see TestParseExtractionResponse) end
    # to end: with only accused_notes in the response, entities_data leaks
    # the same array, but main()'s valid_items filter (entity_name +
    # relationship_type required) rejects the leaked accused-note dicts, so
    # extraction still correctly reports 0 entities.
    response = with_evidence(json.dumps({"accused_notes": [{"name": "गोपाल", "notes": "अध्यक्ष"}]}),
                             FIXTURE_EVIDENCE)
    api = _StubApi([PRESS_ONLY_CASE])
    _run_main(monkeypatch, api, invoke_text_stub=lambda **kw: response,
              argv=["--dry-run"])
    out = capsys.readouterr().out
    assert "TOTAL entities extracted across all cases: 0" in out
    assert "TOTAL accused notes extracted: 1" in out


# --------------------------------------------------------------------------
# Task PP2 -- run-logging events file (see test_enrich_missing_bigo.py's
# identical block for the rationale; `conftest.py`'s autouse
# `_isolate_casework_run_logs` fixture keeps these out of the real repo
# `work/enricher-runs/`).
# --------------------------------------------------------------------------


def _events_path():
    logger = logging.getLogger("casework.entities")
    return logger._casework_run_paths["events"]


def _read_events(path):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines()]


def test_events_file_covers_start_and_extract_happy_path(
    monkeypatch, patched_fetch_markdown, tmp_path
):
    # A genuine happy path, not merely a stub that survives: `_StubApi` has
    # no `get_case_with_etag`, so `plan_case_entities` was always refused
    # (case payload has no "entities" key -> SKIP_STATE-adjacent early
    # return) and the resolution loop, the search, and the write never ran --
    # `("resolve", "ok")` used to pass here only because that event fired
    # unconditionally, resolution or not. `_SearchStubApi` on a DRAFT case
    # carrying an "entities" key and a real ETag is what actually exercises
    # extraction -> resolution -> a genuine `replace_list` write end to end.
    case = dict(PRESS_ONLY_CASE, entities=[])
    api = _SearchStubApi(
        [case],
        {"साझा भण्डार सहकारी": [{"id": "https://jawafdehi.org/entity/organization/"
                                  "sajha-bhandara-sahakari-9f9f9f",
                                 "title": {"ne": "साझा भण्डार सहकारी"}, "score": 200.0}]})
    _run_main(monkeypatch, api, invoke_text_stub=lambda **kw: ENTITY_RESPONSE,
              argv=["--apply"])

    rows = _read_events(_events_path())
    assert rows

    required_keys = {"ts", "run_id", "stage", "slug", "step", "status", "detail", "elapsed_ms"}
    for row in rows:
        assert required_keys <= set(row.keys())
        assert row["stage"] == "entities"

    steps_and_statuses = {(r["step"], r["status"]) for r in rows}
    assert ("start", "start") in steps_and_statuses
    assert ("extract", "ok") in steps_and_statuses
    assert ("resolve", "ok") in steps_and_statuses
    assert ("write", "ok") in steps_and_statuses
    assert len(api.replace_list_calls) == 1


# --------------------------------------------------------------------------
# Task 6 -- bind planning and the merge.
#
# Two corrections to the original brief, both load-bearing (see task-6-report.md
# for the full writeup):
#
# 1. `plan_case_entities` must apply Task 5's document veto
#    (`casework.entity_resolver.apply_document_veto`) before a BIND is trusted:
#    `resolve()` alone will bind Election Commission candidate/ward-head
#    records that share a name with the real case subject. The document read
#    fails closed -- an unreadable document (None/empty/non-dict, or an
#    exception from `get_entity`) downgrades the BIND to REVIEW, never lets it
#    survive.
# 2. Binding is SECTION-SCOPED, not `related`-only. An extracted item binds into
#    whatever section its own `relationship_type` names, for any of the nine the
#    case API accepts -- so a `location`-typed item binds into `location`. Only an
#    unrecognised section goes straight to `plan.review` before a search is spent,
#    because there is no section to file it under. (This reverses the original
#    brief, which refused every section but `related`; the scope was widened on
#    request because the refusal cost recall.)
# --------------------------------------------------------------------------

ANKUR_IRI = "https://jawafdehi.org/entity/person/amkura-khatri-2de9b3"
EXISTING_IRI = "https://jawafdehi.org/entity/person/gopal-bahadur-shrestha-1a2b3c"


def test_current_binds_convert_read_shape_and_drop_outcome():
    # The read snapshot uses `type`; the patch shape uses `relationship_type`.
    # `outcome` is deliberately omitted so the server PRESERVES an accused
    # bind's existing verdict instead of resetting it to 'charged'.
    case = {"entities": [
        {"nes_id": EXISTING_IRI, "display_name": "गोपाल बहादुर श्रेष्ठ",
         "entity_type": "Person", "type": "accused", "outcome": "convicted",
         "notes": "तत्कालीन अध्यक्ष"},
    ]}
    assert current_entity_binds(case) == [
        {"nes_id": EXISTING_IRI, "relationship_type": "accused",
         "notes": "तत्कालीन अध्यक्ष"},
    ]


def test_merge_preserves_existing_binds_and_their_order():
    current = [{"nes_id": EXISTING_IRI, "relationship_type": "accused", "notes": "क"}]
    added = {"nes_id": ANKUR_IRI, "relationship_type": "related", "notes": "ख"}
    merged = merge_entity_binds(current, [added])
    # A merge that also appended a duplicate would still satisfy the two
    # assertions below, so pin the length too.
    assert len(merged) == 2
    assert merged[0] == current[0]
    assert merged[1] == added


def test_merge_never_overwrites_an_existing_bind_for_the_same_id():
    # The existing bind and its human-written notes survive untouched. A second
    # section for the same entity is APPENDED rather than replacing it -- the
    # thing this test exists to prevent is losing "मूल", not gaining a row.
    current = [{"nes_id": ANKUR_IRI, "relationship_type": "accused", "notes": "मूल"}]
    merged = merge_entity_binds(
        current, [{"nes_id": ANKUR_IRI, "relationship_type": "related", "notes": "नयाँ"}])
    assert merged[0] == current[0]


def test_merge_keeps_two_sections_for_one_entity():
    # Bind identity is the PAIR, matching the DB's
    # `unique_case_entity_relationship_type` constraint over
    # ("case", "nes_id", "relationship_type"). An organisation can legitimately be
    # both where the events happened and a related party; keying the merge on
    # `nes_id` alone silently dropped the second bind.
    current = [{"nes_id": SURKHET_IRI, "relationship_type": "location", "notes": "क"}]
    added = {"nes_id": SURKHET_IRI, "relationship_type": "related", "notes": "ख"}
    merged = merge_entity_binds(current, [added])
    assert merged == [current[0], added]


def test_merge_is_still_idempotent_on_an_identical_pair():
    # The pair key must not turn a re-run into a duplicate write.
    current = [{"nes_id": ANKUR_IRI, "relationship_type": "related", "notes": "क"}]
    merged = merge_entity_binds(
        current, [{"nes_id": ANKUR_IRI, "relationship_type": "related", "notes": "ख"}])
    assert merged == current


def test_merge_treats_a_section_as_the_same_bind_regardless_of_case():
    # The read path and a hand-built dict can disagree on casing; `bind_key`
    # lowercases so 'Related' and 'related' are one bind, not two.
    current = [{"nes_id": ANKUR_IRI, "relationship_type": "Related", "notes": "क"}]
    merged = merge_entity_binds(
        current, [{"nes_id": ANKUR_IRI, "relationship_type": "related", "notes": "ख"}])
    assert merged == current


def test_validate_bind_item_rejects_a_non_canonical_iri():
    with pytest.raises(ValueError, match="canonical"):
        validate_bind_item({"nes_id": "https://nes.jawafdehi.org/entity/1",
                            "relationship_type": "related", "notes": ""})


def test_validate_bind_item_rejects_outcome_on_a_non_accused_role():
    with pytest.raises(ValueError, match="accused"):
        validate_bind_item({"nes_id": ANKUR_IRI, "relationship_type": "related",
                            "notes": "", "outcome": "convicted"})


def test_validate_bind_item_relationship_types_match_the_django_enum():
    from cases.models import RelationshipType

    from casework.enrich_related_entities import RELATIONSHIP_TYPES
    assert set(RELATIONSHIP_TYPES) == set(RelationshipType.values)


class _SearchStubApi(_StubApi):
    """_StubApi plus entity search, ETag reads, and entity document reads.

    `documents` maps a `nes_id` to either the document `get_entity` should
    return, or an `Exception` instance it should raise instead -- so a test
    can pin the document veto's fail-closed behaviour on a transient read
    failure without a real network. A `nes_id` absent from `documents` gets a
    default document that is a normal (non-election) CIAA portal entity: a
    non-empty dict with no election-record `identifier`, so the veto is a
    no-op unless a test deliberately configures otherwise -- 25 of the 40
    frozen fixture documents look exactly like this and 24 of the 25 still
    BIND. The exception is the `मालपोत कार्यालय` bucket, which the structural
    unqualified-institution veto (`92dc4db`) now holds at REVIEW before this
    document is ever fetched.
    """

    # No `get_court_case_entities` stub, deliberately. This enricher no longer
    # reads accused from the NGM court record, and a stub for a call the code
    # cannot make would let that path be wired back in without a single test
    # failing. `casework/court_record.py` keeps its own 15 tests.
    def __init__(self, cases, search_results=None, documents=None):
        super().__init__(cases)
        self._search = search_results or {}
        self._documents = documents or {}
        self.etag = 'W/"abc123"'
        self.search_calls = []
        self.get_entity_calls = []
        # Entity creation. `create_entity_calls` records the payload as sent;
        # `create_conflicts` holds `<prefix>/<slug>` values that answer as though
        # the entity already exists, and `create_errors` maps one to the
        # exception the POST should raise instead.
        self.create_entity_calls = []
        self.create_conflicts = set()
        self.create_errors = {}
        self.live_prefixes = [
            "person",
            "location", "location/district",
            "organization", "organization/contractor",
            "organization/government", "organization/government/department",
            "organization/government/district/dfo",
        ]

    def entity_prefixes(self, timeout=60):
        return list(self.live_prefixes)

    def create_entity(self, payload, timeout=60):
        self.create_entity_calls.append(dict(payload))
        ref = f"{payload['prefix']}/{payload['slug']}"
        if ref in self.create_errors:
            raise self.create_errors[ref]
        if ref in self.create_conflicts:
            raise EntityAlreadyExists(f"Entity {ref} already exists")
        return {"@id": f"https://jawafdehi.org/entity/{ref}",
                "@type": payload.get("type"), "name": payload.get("name")}

    def search_entities(self, query, **kw):
        self.search_calls.append(query)
        return self._search.get(query, [])

    def get_entity(self, ref, timeout=60):
        self.get_entity_calls.append(ref)
        if ref in self._documents:
            configured = self._documents[ref]
            if isinstance(configured, BaseException):
                raise configured
            return configured
        return {"identifier": None}

    def get_case_with_etag(self, slug, timeout=60):
        return self._cases[slug], self.etag

    def patch_field(self, slug, field, value, timeout=60, if_match=None):
        self.patch_calls.append((slug, field, value, if_match))
        return {}

    def replace_list(self, slug, path, items, timeout=60, if_match=None):
        self.replace_list_calls.append((slug, path, items, if_match))
        return {}


ANKUR_CANDIDATE = {"id": ANKUR_IRI, "title": {"ne": "अंकुर खत्री", "en": "Ankur Khatri"},
                   "score": 194.0}


def test_plan_binds_a_confident_name_and_keeps_the_existing_bind():
    case = {"slug": "case-x", "state": "DRAFT", "entities": [
        {"nes_id": EXISTING_IRI, "type": "accused", "outcome": "charged", "notes": "क"}]}
    api = _SearchStubApi([case], {"अंकुर खत्री": [ANKUR_CANDIDATE]})
    plan = plan_case_entities(api, case, 'W/"e"', [
        {"entity_name": "अंकुर खत्री", "relationship_type": "related",
         "notes": "घुस लेनदेनमा सहयोग"}])

    assert plan.action == "WOULD_PATCH"
    assert [i["nes_id"] for i in plan.patch_items] == [EXISTING_IRI, ANKUR_IRI]
    assert plan.patch_items[1]["notes"] == "घुस लेनदेनमा सहयोग"
    assert "outcome" not in plan.patch_items[1]
    assert len(plan.bound) == 1


def test_plan_is_a_noop_when_the_name_is_already_bound():
    case = {"slug": "case-x", "state": "DRAFT", "entities": [
        {"nes_id": ANKUR_IRI, "type": "related", "notes": "क"}]}
    api = _SearchStubApi([case], {"अंकुर खत्री": [ANKUR_CANDIDATE]})
    plan = plan_case_entities(api, case, 'W/"e"', [
        {"entity_name": "अंकुर खत्री", "relationship_type": "related", "notes": "ख"}])
    assert plan.action == "NOOP"
    # Fix round 1, item 3: a name that resolves to an already-bound entity
    # must not be counted in `plan.bound` -- it produced no new write, so
    # a summary counting len(plan.bound) as "binds made" must not overstate
    # on a re-run.
    assert plan.bound == []


def test_plan_refuses_a_non_draft_case():
    case = {"slug": "case-y", "state": "IN_REVIEW", "entities": []}
    api = _SearchStubApi([case], {"अंकुर खत्री": [ANKUR_CANDIDATE]})
    plan = plan_case_entities(api, case, 'W/"e"', [
        {"entity_name": "अंकुर खत्री", "relationship_type": "related", "notes": "ख"}])
    assert plan.action == "SKIP_STATE"
    assert plan.patch_items == []


def test_strict_buckets_review_and_nomatch_separately():
    # Two different failures must not land in one bucket: अनिष श्रेष्ठ matched
    # two same-name entities (a decision to make -> review) while खगेन्द्र
    # पराजुली matched nothing at all (nothing to decide -> nomatch). Pinned
    # under strict=True, which is the mode that still refuses an ambiguity.
    anish_a = {"id": "https://jawafdehi.org/entity/person/anish-shrestha-219986",
               "title": {"ne": "अनिष श्रेष्‍ठ"}, "score": 182.17}
    anish_b = {"id": "https://jawafdehi.org/entity/person/anish-shrestha-285096",
               "title": {"ne": "अनिष श्रेष्ठ"}, "score": 182.17}
    case = {"slug": "case-z", "state": "DRAFT", "entities": []}
    api = _SearchStubApi([case], {"अनिष श्रेष्ठ": [anish_a, anish_b],
                                  "खगेन्द्र पराजुली": []})
    plan = plan_case_entities(api, case, 'W/"e"', [
        {"entity_name": "अनिष श्रेष्ठ", "relationship_type": "related", "notes": "क"},
        {"entity_name": "खगेन्द्र पराजुली", "relationship_type": "related", "notes": "ख"}],
        strict=True)
    assert plan.action == "NOOP"
    assert len(plan.review) == 1
    assert len(plan.nomatch) == 1


def test_permissive_binds_the_ambiguity_and_still_cannot_bind_a_nomatch():
    # The default mode's whole point, and its limit. An ambiguity between two
    # same-name entities binds BOTH of them (2026-08-05: no review queue, the
    # later filtering pass decides which is real); a name with NO candidate stays
    # in nomatch, because there is nothing to bind. Creating one is the create
    # step's job and requires `--create-entities`.
    anish_a = {"id": "https://jawafdehi.org/entity/person/anish-shrestha-219986",
               "title": {"ne": "अनिष श्रेष्‍ठ"}, "score": 182.17}
    anish_b = {"id": "https://jawafdehi.org/entity/person/anish-shrestha-285096",
               "title": {"ne": "अनिष श्रेष्ठ"}, "score": 182.17}
    case = {"slug": "case-z", "state": "DRAFT", "entities": []}
    api = _SearchStubApi([case], {"अनिष श्रेष्ठ": [anish_a, anish_b],
                                  "खगेन्द्र पराजुली": []},
                         documents={anish_a["id"]: {"identifier": None},
                                    anish_b["id"]: {"identifier": None}})
    plan = plan_case_entities(api, case, 'W/"e"', [
        {"entity_name": "अनिष श्रेष्ठ", "relationship_type": "related", "notes": "क"},
        {"entity_name": "खगेन्द्र पराजुली", "relationship_type": "related", "notes": "ख"}])

    assert plan.action == "WOULD_PATCH"
    assert len(plan.bound) == 2
    assert plan.review == []
    assert len(plan.nomatch) == 1
    # Both namesakes bound, in the deterministic `(-score, nes_id)` order so a
    # re-run produces the same list rather than a reshuffled one.
    assert [d.nes_id for _n, d, _notes, _s in plan.bound] == [
        anish_a["id"], anish_b["id"]]
    _name, decision, _notes, _section = plan.bound[0]
    assert is_promoted(decision)
    assert "ambiguous" in decision.reason


def test_promotion_is_deterministic_across_candidate_orderings():
    # A re-run must never bind a DIFFERENT namesake than the run before it. The
    # promoted winner comes from `resolve`'s `(-score, nes_id)` sort, so shuffling
    # the search payload cannot change it.
    anish_a = {"id": "https://jawafdehi.org/entity/person/anish-shrestha-219986",
               "title": {"ne": "अनिष श्रेष्ठ"}, "score": 182.17}
    anish_b = {"id": "https://jawafdehi.org/entity/person/anish-shrestha-285096",
               "title": {"ne": "अनिष श्रेष्ठ"}, "score": 182.17}
    docs = {anish_a["id"]: {"identifier": None}, anish_b["id"]: {"identifier": None}}
    bound = []
    for payload in ([anish_a, anish_b], [anish_b, anish_a]):
        case = {"slug": "case-z", "state": "DRAFT", "entities": []}
        api = _SearchStubApi([case], {"अनिष श्रेष्ठ": payload}, documents=docs)
        plan = plan_case_entities(api, case, 'W/"e"', [
            {"entity_name": "अनिष श्रेष्ठ", "relationship_type": "related",
             "notes": "क"}])
        bound.append(plan.bound[0][1].nes_id)

    assert bound[0] == bound[1] == anish_a["id"]


# A candidate that exists in NES with an English title only, so the only available
# comparison is Devanagari-against-Latin. That comparison goes through
# `to_roman_colloquial`, which folds कमल (masculine) into कमला (feminine): the pair
# scores 0.96 across scripts and 0.00 within Devanagari.
KAMALA_IRI = "https://jawafdehi.org/entity/person/kamala-thapa-4f21ac"
KAMALA_LATIN_ONLY = {"id": KAMALA_IRI, "title": {"en": "Kamala Thapa"}, "score": 190.0}


def test_permissive_mode_now_binds_a_cross_script_only_match():
    # WAS THE ONE VETO PERMISSIVE MODE LEFT STANDING, and it is gone by decision
    # (2026-08-05): this stage produces no review queue.
    #
    # Keeping the original reasoning on the record, because the test no longer
    # states it: every other promotion binds a name that matched on grounds
    # outside the name, while this one binds a name that did not match at all.
    # कमला थापा is a woman, कमल थापा is a man, and only romanisation makes them
    # equal -- 0.96 across scripts, 0.00 within Devanagari. So this bind can name
    # a different person than the case charges, and the later filtering pass is
    # what catches it.
    case = {"slug": "case-kamal", "state": "DRAFT", "entities": []}
    api = _SearchStubApi([case], {"कमल थापा": [KAMALA_LATIN_ONLY]})
    plan = plan_case_entities(api, case, 'W/"e"', [
        {"entity_name": "कमल थापा", "relationship_type": "related", "notes": "क"}])

    assert plan.action == "WOULD_PATCH"
    assert plan.review == []
    assert [item["nes_id"] for item in plan.patch_items] == [KAMALA_IRI]


def test_a_cross_script_match_is_bound_even_when_another_veto_reports_first():
    # Two Latin-only namesakes, both above threshold. `resolve` reports only the
    # first veto that fires and ambiguity is checked before the cross-script
    # guard, so the reason reads "ambiguous". Both now bind.
    second = {"id": "https://jawafdehi.org/entity/person/kamala-thapa-9b7e10",
              "title": {"en": "Kamala Thapa"}, "score": 188.0}
    case = {"slug": "case-kamal-2", "state": "DRAFT", "entities": []}
    api = _SearchStubApi([case], {"कमल थापा": [KAMALA_LATIN_ONLY, second]})
    plan = plan_case_entities(api, case, 'W/"e"', [
        {"entity_name": "कमल थापा", "relationship_type": "related", "notes": "क"}])

    assert plan.review == []
    assert sorted(item["nes_id"] for item in plan.patch_items) == sorted(
        [KAMALA_IRI, second["id"]])


def test_a_same_script_candidate_is_still_promoted_over_its_veto():
    # The other side of the guard: refusing cross-script must not quietly turn off
    # promotion generally. Same names, same ambiguity, but the candidates carry
    # Devanagari titles -- so the comparison was fair and the bind goes ahead.
    thapa_a = {"id": "https://jawafdehi.org/entity/person/kamal-thapa-111111",
               "title": {"ne": "कमल थापा"}, "score": 190.0}
    thapa_b = {"id": "https://jawafdehi.org/entity/person/kamal-thapa-222222",
               "title": {"ne": "कमल थापा"}, "score": 189.0}
    case = {"slug": "case-kamal-3", "state": "DRAFT", "entities": []}
    api = _SearchStubApi([case], {"कमल थापा": [thapa_a, thapa_b]})
    plan = plan_case_entities(api, case, 'W/"e"', [
        {"entity_name": "कमल थापा", "relationship_type": "related", "notes": "क"}])

    assert plan.review == []
    _name, decision, _notes, _section = plan.bound[0]
    assert decision.nes_id == thapa_a["id"]
    assert is_promoted(decision)
    assert "ambiguous" in decision.reason


def test_strict_mode_also_refuses_a_cross_script_only_match():
    # Strict mode never promoted anything, so this is unchanged behaviour --
    # asserted so the two modes cannot diverge on the one case where they must
    # agree.
    case = {"slug": "case-kamal-strict", "state": "DRAFT", "entities": []}
    api = _SearchStubApi([case], {"कमल थापा": [KAMALA_LATIN_ONLY]})
    plan = plan_case_entities(api, case, 'W/"e"', [
        {"entity_name": "कमल थापा", "relationship_type": "related", "notes": "क"}],
        strict=True)

    assert plan.bound == []
    assert plan.review[0][1].nes_id is None


def test_one_entity_binds_into_two_sections_in_a_single_plan():
    # The planner's `have` set is keyed on the pair too, so a pre-resolved
    # location and an extraction naming the SAME entity under a different
    # section plan both writes. Keyed on `nes_id` alone the second was dropped
    # without appearing in any report.
    case = {"slug": "case-two-sections", "state": "DRAFT", "entities": []}
    api = _SearchStubApi([case], {"सुर्खेत जिल्ला": [SURKHET_CANDIDATE]})
    lb = LocationBind(SURKHET_IRI, "क", "सुर्खेत जिल्ला", "एभिडेन्स", "gazetteer")
    plan = plan_case_entities(api, case, 'W/"e"', [
        {"entity_name": "सुर्खेत जिल्ला", "relationship_type": "related", "notes": "ख"}],
        locations=[lb])

    assert plan.action == "WOULD_PATCH"
    assert [(i["nes_id"], i["relationship_type"]) for i in plan.patch_items] == [
        (SURKHET_IRI, "location"), (SURKHET_IRI, "related")]
    assert len(plan.bound) == 2
    # Each row carries its OWN section. Looked up by `nes_id` instead, both rows
    # reported whichever section was written last, so `.binds.jsonl` and the
    # console would have labelled the location bind 'related'.
    assert [section for _n, _d, _notes, section in plan.bound] == ["location", "related"]


def test_the_same_location_bind_twice_is_planned_once():
    # A `LocationBind` can in principle repeat across the same run --
    # `resolve_locations` already dedups on `nes_id`, but the planner's own
    # `have`/`bind_key` idempotency must hold regardless, the same as it does
    # for a duplicate resolved from two extracted spellings in any other
    # section.
    case = {"slug": "case-dupe", "state": "DRAFT", "entities": []}
    api = _SearchStubApi([case], {})
    lb = LocationBind(SURKHET_IRI, "क", "सुर्खेत जिल्ला", "एभिडेन्स", "gazetteer")
    plan = plan_case_entities(api, case, 'W/"e"', [], locations=[lb, lb])

    assert [i["nes_id"] for i in plan.patch_items] == [SURKHET_IRI]
    assert len(plan.bound) == 1


def test_an_accused_extraction_never_touches_an_existing_bind():
    # This used to be the escalation guard: an `accused` bind on an entity the
    # case already binds as `related` would assert they are the subject of the
    # case AND set outcome=charged, so it went to review. The guard is now moot
    # -- accused never reaches the binder at all (2026-08-06, confirmed with
    # Gaurav's supervisor: defendants come from the NGM court record).
    case = {"slug": "case-escalate", "state": "DRAFT", "entities": [
        {"nes_id": ANKUR_IRI, "type": "related", "notes": "मानव-लिखित टिप्पणी"}]}
    api = _SearchStubApi([case], {"अंकुर खत्री": [ANKUR_CANDIDATE]})
    plan = plan_case_entities(api, case, 'W/"e"', [
        {"entity_name": "अंकुर खत्री", "relationship_type": "accused", "notes": "क"}])

    assert plan.action == "NOOP"
    assert plan.bound == []
    assert plan.review == []          # no review either -- there is nothing to decide
    assert plan.court_record_only == [("अंकुर खत्री", "accused")]
    # The existing bind and its human note are untouched.
    assert plan.patch_items == []


def test_a_non_accused_section_does_join_an_already_characterised_entity():
    # The other side of that guard: only `accused` is held back. A pre-resolved
    # `location` bind alongside an existing `related` one is additive, not an
    # accusation.
    case = {"slug": "case-additive", "state": "DRAFT", "entities": [
        {"nes_id": SURKHET_IRI, "type": "related", "notes": "क"}]}
    api = _SearchStubApi([case], {})
    lb = LocationBind(SURKHET_IRI, "ख", "सुर्खेत जिल्ला", "एभिडेन्स", "gazetteer")
    plan = plan_case_entities(api, case, 'W/"e"', [], locations=[lb])

    assert plan.action == "WOULD_PATCH"
    assert [(i["nes_id"], i["relationship_type"]) for i in plan.patch_items] == [
        (SURKHET_IRI, "related"), (SURKHET_IRI, "location")]


def test_a_first_time_accused_is_not_written_either():
    # Not a narrowing of the old escalation guard -- a removal of the path. Even
    # with a clean case, an unambiguous name and a perfect match, nothing is
    # written, because the court record already states who the defendants are.
    case = {"slug": "case-first-accused", "state": "DRAFT", "entities": []}
    api = _SearchStubApi([case], {"अंकुर खत्री": [ANKUR_CANDIDATE]})
    plan = plan_case_entities(api, case, 'W/"e"', [
        {"entity_name": "अंकुर खत्री", "relationship_type": "accused", "notes": "क"}])

    assert plan.action == "NOOP"
    assert plan.patch_items == []
    assert plan.court_record_only == [("अंकुर खत्री", "accused")]


def test_each_review_row_reports_its_own_section():
    # One person, named in two sections by the same extraction -- a witness in a
    # case who is also alleged, which the prompt allows. The section is recorded on
    # the review row itself for exactly this reason: derived from a name-keyed dict
    # instead, both rows reported the LAST section seen, so a caseworker triaging
    # `*.review.jsonl` would see two `witness` rows and no `alleged` one.
    case = {"slug": "case-two-roles", "state": "DRAFT", "entities": []}
    api = _SearchStubApi([case], {"अनिष श्रेष्ठ": [ANISH_A, ANISH_B]})
    plan = plan_case_entities(api, case, 'W/"e"', [
        {"entity_name": "अनिष श्रेष्ठ", "relationship_type": "alleged", "notes": "क"},
        {"entity_name": "अनिष श्रेष्ठ", "relationship_type": "witness", "notes": "ख"}],
        strict=True)

    assert [section for _n, _d, section in plan.review] == ["alleged", "witness"]


def test_an_unrecognised_section_is_recorded_verbatim_when_it_is_coerced():
    # WAS a review row carrying the raw value. The row is gone, but the raw value
    # still has to be recoverable: a relabelled section is a claim nobody made,
    # so `plan.coerced` keeps what the model actually said, lowercased.
    #
    # The name is now searched, where before the bad section short-circuited it.
    # That is a real added cost -- one search request per coerced name -- and it
    # is the price of not failing the whole case's PATCH on one bad label.
    case = {"slug": "case-bad-section", "state": "DRAFT", "entities": []}
    api = _SearchStubApi([case], {})
    plan = plan_case_entities(api, case, 'W/"e"', [
        {"entity_name": "अनिष श्रेष्ठ", "relationship_type": "Suspect", "notes": "क"}])

    assert plan.review == []
    assert plan.coerced == [("अनिष श्रेष्ठ", "suspect", "related")]
    assert api.search_calls != []


# --- Correction 1: the document veto (Task 5's `apply_document_veto`) -----


def test_strict_downgrades_a_bind_to_review_when_the_document_is_an_election_record():
    # A real person, correctly named, with a search-payload score above
    # MIN_BIND_SCORE -- but the second read (the document) shows it is an
    # Election Commission candidate/ward-head record, not confirmed as the
    # case subject. `resolve()` alone would BIND this; strict mode must not.
    case = {"slug": "case-elect", "state": "DRAFT", "entities": []}
    election_doc = {"identifier": [{"propertyID": "ecn-candidate-id", "value": "12345"}]}
    api = _SearchStubApi(
        [case], {"अंकुर खत्री": [ANKUR_CANDIDATE]}, documents={ANKUR_IRI: election_doc})
    plan = plan_case_entities(api, case, 'W/"e"', [
        {"entity_name": "अंकुर खत्री", "relationship_type": "related", "notes": "क"}],
        strict=True)

    assert plan.action == "NOOP"
    assert plan.patch_items == []
    assert plan.bound == []
    assert len(plan.review) == 1
    name, decision, _section = plan.review[0]
    assert name == "अंकुर खत्री"
    assert decision.nes_id is None
    assert "Election Commission" in decision.reason
    assert api.get_entity_calls == [ANKUR_IRI]


def test_permissive_does_not_bind_an_election_record():
    # NOT promotable, even in permissive mode. NES holds the bulk ECN candidate
    # rolls, so a name match against one carries no information: 5 of 5 wrong in
    # the 2026-08-13 review, 12 of 12 on the FY078/079 batch. Permissive mode
    # accepts uncertainty ABOUT a match; this is a match with nothing behind it.
    case = {"slug": "case-elect", "state": "DRAFT", "entities": []}
    election_doc = {"identifier": [{"propertyID": "ecn-candidate-id", "value": "12345"}]}
    api = _SearchStubApi(
        [case], {"अंकुर खत्री": [ANKUR_CANDIDATE]}, documents={ANKUR_IRI: election_doc})
    plan = plan_case_entities(api, case, 'W/"e"', [
        {"entity_name": "अंकुर खत्री", "relationship_type": "related", "notes": "क"}])

    assert plan.action == "NOOP"
    assert plan.patch_items == []
    assert plan.bound == []
    _name, decision, _section = plan.review[0]
    assert decision.nes_id is None
    assert "Election Commission" in decision.reason


def test_a_doubly_vetoed_name_records_both_reasons():
    # `apply_document_veto` REPLACES the reason, so a name that was ambiguous AND
    # turned out to be an election record used to end up recorded as only the
    # second. The carry-forward still has to hold now that the election veto
    # refuses rather than promotes -- the review row is what a caseworker reads,
    # and it must not under-report how uncertain the name was.
    election_doc = {"identifier": [{"propertyID": "ecn-candidate-id", "value": "12345"}]}
    case = {"slug": "case-both", "state": "DRAFT", "entities": []}
    api = _SearchStubApi([case], {"अनिष श्रेष्ठ": [ANISH_A, ANISH_B]},
                         documents={ANISH_A["id"]: election_doc})
    plan = plan_case_entities(api, case, 'W/"e"', [
        {"entity_name": "अनिष श्रेष्ठ", "relationship_type": "related", "notes": "क"}])

    assert plan.bound == []
    _name, decision, _section = plan.review[0]
    assert "Election Commission" in decision.reason   # the second veto
    assert "ambiguous" in decision.reason             # the first, no longer lost


def test_a_single_overridden_veto_is_not_recorded_twice():
    # The carry-forward must fire only when the reason was actually replaced. A
    # promoted ambiguity whose document comes back clean passes through
    # `apply_document_veto` untouched, so it keeps exactly one reason.
    case = {"slug": "case-once", "state": "DRAFT", "entities": []}
    api = _SearchStubApi([case], {"अनिष श्रेष्ठ": [ANISH_A, ANISH_B]})
    plan = plan_case_entities(api, case, 'W/"e"', [
        {"entity_name": "अनिष श्रेष्ठ", "relationship_type": "related", "notes": "क"}])

    _name, decision, _notes, _section = plan.bound[0]
    assert decision.reason.count("ambiguous") == 1
    assert "; also" not in decision.reason


def test_plan_downgrades_a_bind_to_review_when_get_entity_raises():
    # Fail closed: one transient 403/502 on the document read must not let a
    # BIND survive. This is the whole point of the try/except around
    # `api.get_entity` -- a raised exception must map to REVIEW, not bubble
    # up and abort the run, and never leave nes_id set.
    case = {"slug": "case-unreadable", "state": "DRAFT", "entities": []}
    api = _SearchStubApi(
        [case], {"अंकुर खत्री": [ANKUR_CANDIDATE]},
        documents={ANKUR_IRI: RuntimeError("502 Bad Gateway")})
    plan = plan_case_entities(api, case, 'W/"e"', [
        {"entity_name": "अंकुर खत्री", "relationship_type": "related", "notes": "क"}])

    assert plan.action == "NOOP"
    assert plan.patch_items == []
    assert plan.bound == []
    assert len(plan.review) == 1
    name, decision, _section = plan.review[0]
    assert decision.nes_id is None
    assert api.get_entity_calls == [ANKUR_IRI]


def test_plan_still_binds_when_the_document_has_a_null_identifier():
    # The boundary, precisely: a document that is a dict with `identifier:
    # null` is a NORMAL CIAA portal entity (25 of the 40 frozen fixture
    # documents look exactly like this) and must still BIND. Only an
    # UNREADABLE document fails closed -- this is not that.
    case = {"slug": "case-normal", "state": "DRAFT", "entities": []}
    api = _SearchStubApi(
        [case], {"अंकुर खत्री": [ANKUR_CANDIDATE]}, documents={ANKUR_IRI: {"identifier": None}})
    plan = plan_case_entities(api, case, 'W/"e"', [
        {"entity_name": "अंकुर खत्री", "relationship_type": "related", "notes": "क"}])

    assert plan.action == "WOULD_PATCH"
    assert [i["nes_id"] for i in plan.patch_items] == [ANKUR_IRI]
    assert len(plan.bound) == 1
    assert plan.review == []


# --- Correction 2, superseded by Task 5: a location-typed EXTRACTED item is
# refused, never searched. `test_a_location_item_binds_into_the_location_
# section` and the location half of `test_a_location_and_a_related_item_each_
# bind_into_their_own_section` pinned the search-based bind this reverses;
# see `TestExtractedLocationItemsAreRefused` below for the replacement and
# `test_one_entity_binds_into_two_sections_in_a_single_plan` above for the
# `locations=` replacement of the two-section shape.


SURKHET_IRI = "https://jawafdehi.org/entity/location/district/surkhet"
SURKHET_CANDIDATE = {"id": SURKHET_IRI, "title": {"ne": "सुर्खेत जिल्ला"},
                     "score": 190.0}


def test_a_location_item_is_refused_while_a_related_item_in_the_same_extraction_still_binds():
    # A mixed extraction lands in two different sections from one pass. The
    # `location` item is refused to review without ever being searched; the
    # `related` item resolves and binds exactly as any other section would.
    case = {"slug": "case-mixed", "state": "DRAFT", "entities": []}
    api = _SearchStubApi([case], {"अंकुर खत्री": [ANKUR_CANDIDATE],
                                  "सुर्खेत जिल्ला": [SURKHET_CANDIDATE]},
                         documents={ANKUR_IRI: {"identifier": None},
                                    SURKHET_IRI: {"identifier": None}})
    plan = plan_case_entities(api, case, 'W/"e"', [
        {"entity_name": "सुर्खेत जिल्ला", "relationship_type": "location", "notes": ""},
        {"entity_name": "अंकुर खत्री", "relationship_type": "related", "notes": "ख"}])

    assert plan.action == "WOULD_PATCH"
    assert {i["nes_id"]: i["relationship_type"] for i in plan.patch_items} == {
        ANKUR_IRI: "related"}
    assert len(plan.bound) == 1
    assert [(name, section) for name, _decision, section in plan.review] == [
        ("सुर्खेत जिल्ला", "location")]
    assert "सुर्खेत जिल्ला" not in api.search_calls


KTM_IRI = "https://jawafdehi.org/entity/location/district/kathmandu-np0327"


class TestLocationsBindOnlyThroughTheGazetteer:
    """Task 5: `plan_case_entities` binds a `LocationBind` straight through
    `_bind_one`, and refuses an extracted `location` item outright rather than
    name-searching it."""

    @staticmethod
    def _ktm_bind():
        return LocationBind(KTM_IRI, "घटना भएको जिल्ला", "काठमाडौं",
                            "काठमाडौं जिल्लाभित्र भएको", "gazetteer")

    def test_a_location_bind_puts_a_patch_item_into_the_plan(self):
        case = {"slug": "case-ktm", "state": "DRAFT", "entities": []}
        api = _SearchStubApi([case], {})
        plan = plan_case_entities(api, case, 'W/"e"', [], locations=[self._ktm_bind()])

        assert plan.action == "WOULD_PATCH"
        assert {"nes_id": KTM_IRI, "relationship_type": "location",
                "notes": "घटना भएको जिल्ला"} in plan.patch_items
        assert api.search_calls == []

    def test_a_rerun_with_the_location_already_bound_is_a_noop(self):
        case = {"slug": "case-ktm-rerun", "state": "DRAFT", "entities": [
            {"nes_id": KTM_IRI, "type": "location", "notes": "घटना भएको जिल्ला"}]}
        api = _SearchStubApi([case], {})
        plan = plan_case_entities(api, case, 'W/"e"', [], locations=[self._ktm_bind()])

        assert plan.action == "NOOP"
        assert plan.patch_items == []

    def test_an_extracted_location_item_lands_in_review_without_a_search(self):
        case = {"slug": "case-ktm-extracted", "state": "DRAFT", "entities": []}
        api = _SearchStubApi([case], {})
        plan = plan_case_entities(api, case, 'W/"e"', [
            {"entity_name": "काठमाडौं", "relationship_type": "location", "notes": ""}])

        assert plan.patch_items == []
        name, decision, section = plan.review[0]
        assert name == "काठमाडौं"
        assert section == "location"
        assert decision.reason == (
            "a location must come through the gazetteer (resolve_locations)")
        assert api.search_calls == []


def test_an_accused_extraction_writes_nothing():
    # `accused` is the one section with an extra requirement: the DB's
    # `outcome_only_on_accused` CHECK makes `outcome` legal here and nowhere
    # else, and every case in this corpus is a Special Court `-CR-` case, so
    # 'charged' is true by construction.
    case = {"slug": "case-acc", "state": "DRAFT", "entities": []}
    api = _SearchStubApi([case], {"अंकुर खत्री": [ANKUR_CANDIDATE]},
                         documents={ANKUR_IRI: {"identifier": None}})
    plan = plan_case_entities(api, case, 'W/"e"', [
        {"entity_name": "अंकुर खत्री", "relationship_type": "accused", "notes": "क"}])

    # No bind, and therefore no `outcome` -- which is the point. `outcome` is
    # legal only on an accused bind, so with the section gone this module can
    # never send one.
    assert plan.action == "NOOP"
    assert plan.patch_items == []


# --------------------------------------------------------------------------
# Fix round 1 -- review response. Seven items; this section covers the six
# that land in this module (the ETag question's write-side enforcement is
# Task 7's, per the ruling; this module only surfaces its absence).
# --------------------------------------------------------------------------


# --- Item 1 (critical): the location gate must be an allow-list ----------


@pytest.mark.parametrize("relationship_type,expected", [
    ("ALLEGED", "alleged"),
    ("witness", "witness"),
])
def test_a_valid_section_binds_however_the_llm_cased_it(relationship_type, expected):
    # The field is trimmed and lowercased before it is checked, so an LLM that
    # capitalises or pads it still lands in the right section. This used to be an
    # allow-list of exactly "related"; the normalisation is what survived from it,
    # because a casing mismatch here once let "Location" through a deny-list and
    # bind as related.
    case = {"slug": "case-scope", "state": "DRAFT", "entities": []}
    api = _SearchStubApi([case], {"अंकुर खत्री": [ANKUR_CANDIDATE]},
                         documents={ANKUR_IRI: {"identifier": None}})
    plan = plan_case_entities(api, case, 'W/"e"', [
        {"entity_name": "अंकुर खत्री", "relationship_type": relationship_type,
         "notes": "क"}])

    assert plan.action == "WOULD_PATCH"
    assert plan.patch_items[0]["relationship_type"] == expected


@pytest.mark.parametrize("relationship_type", ["Location", " location ", "LOCATION"])
def test_a_location_section_is_refused_however_the_llm_cased_it(relationship_type):
    # The refusal keys on the SAME normalised value the bind path does -- a
    # casing or padding mismatch here would let a location slip through and be
    # name-searched, exactly the hole the casing test above exists to close on
    # the bind side.
    case = {"slug": "case-scope-loc", "state": "DRAFT", "entities": []}
    api = _SearchStubApi([case], {"अंकुर खत्री": [ANKUR_CANDIDATE]})
    plan = plan_case_entities(api, case, 'W/"e"', [
        {"entity_name": "अंकुर खत्री", "relationship_type": relationship_type,
         "notes": "क"}])

    assert plan.patch_items == []
    assert [(name, section) for name, _decision, section in plan.review] == [
        ("अंकुर खत्री", "location")]
    assert api.search_calls == []


@pytest.mark.parametrize("relationship_type", ["", "organization", "Related party", None])
def test_a_section_the_api_does_not_accept_is_coerced_and_still_binds(relationship_type):
    # WAS refused before any search, so a malformed extraction cost no request.
    # Now coerced to `related` and bound: one unaccepted section fails the whole
    # case's PATCH, so the name that would have been held is the cheap loss and
    # every other bind on the case is the expensive one.
    case = {"slug": "case-scope", "state": "DRAFT", "entities": []}
    api = _SearchStubApi([case], {"अंकुर खत्री": [ANKUR_CANDIDATE]})
    plan = plan_case_entities(api, case, 'W/"e"', [
        {"entity_name": "अंकुर खत्री", "relationship_type": relationship_type,
         "notes": "क"}])

    assert plan.action == "WOULD_PATCH"
    assert plan.review == []
    assert [item["relationship_type"] for item in plan.patch_items] == ["related"]
    assert [c[1] for c in plan.coerced] == [(relationship_type or "").strip().lower()]


def test_plan_coerces_a_missing_relationship_type_rather_than_holding_it():
    # The allow-list's other half: a missing key coerces the same way an
    # unrecognised string does, so the planner never depends on `main()` having
    # filtered such items out first.
    case = {"slug": "case-missing-type", "state": "DRAFT", "entities": []}
    api = _SearchStubApi([case], {"अंकुर खत्री": [ANKUR_CANDIDATE]})
    plan = plan_case_entities(api, case, 'W/"e"', [
        {"entity_name": "अंकुर खत्री", "notes": "क"}])  # no relationship_type at all

    assert plan.action == "WOULD_PATCH"
    assert plan.review == []
    assert plan.coerced == [("अंकुर खत्री", "", "related")]


# --- Item 2 (important): a case payload with no "entities" key -----------


def test_plan_refuses_to_write_when_the_entities_key_is_absent_from_the_case_payload():
    # `case.get("entities") or []` cannot tell "this case has no binds" from
    # "this payload does not carry binds" -- and sent to `replace_list`, the
    # latter deletes every existing bind. Absent is not empty.
    case = {"slug": "case-noentities", "state": "DRAFT"}  # no "entities" key
    api = _SearchStubApi([case], {"अंकुर खत्री": [ANKUR_CANDIDATE]})
    plan = plan_case_entities(api, case, 'W/"e"', [
        {"entity_name": "अंकुर खत्री", "relationship_type": "related", "notes": "क"}])

    assert plan.action == "NOOP"
    assert plan.patch_items == []
    assert "entities" in plan.reason
    # Never even reached the resolver -- refused before any search.
    assert api.search_calls == []


# --- Item 3 (important): bound-count accuracy + exception text -----------


def test_plan_folds_the_get_entity_exception_text_into_the_review_reason():
    # A misconfigured base URL or a changed `get_entity` signature would
    # downgrade EVERY bind to REVIEW with only "entity document unavailable" to
    # go on -- indistinguishable from a real transient failure. The actual
    # exception text must be diagnosable from the plan.
    case = {"slug": "case-diag", "state": "DRAFT", "entities": []}
    api = _SearchStubApi(
        [case], {"अंकुर खत्री": [ANKUR_CANDIDATE]},
        documents={ANKUR_IRI: RuntimeError("502 Bad Gateway from api.example")})
    plan = plan_case_entities(api, case, 'W/"e"', [
        {"entity_name": "अंकुर खत्री", "relationship_type": "related", "notes": "क"}])

    assert len(plan.review) == 1
    _, decision, _section = plan.review[0]
    assert "502 Bad Gateway from api.example" in decision.reason


# --- Item 5 (important, new): a truncated candidate list must not bind ---


def test_plan_downgrades_a_bind_when_search_reports_an_incomplete_window():
    # `search_entities` reports whether it ran out of results or stopped early,
    # via `CandidateList.complete`. When it stopped early AND the lowest-ranked
    # row fetched still scores as high as the match, an equally-relevant same-name
    # entity can sit just past the edge, so the ambiguity veto's premise -- every
    # tied candidate was seen -- does not hold and the bind must not survive.
    #
    # Note the scores: the filler ties the match rather than sitting far below it.
    # That is the whole condition. A row COUNT cannot express it, which is why the
    # earlier version of this test passed a 200-long list of score-1.0 filler and
    # proved nothing about the real hazard.
    candidates = CandidateList([ANKUR_CANDIDATE] + [
        {"id": f"https://jawafdehi.org/entity/person/filler-{i:03d}-aaaaaa",
         "title": {"ne": "फरक नाम"}, "score": ANKUR_CANDIDATE["score"]}
        for i in range(49)])
    candidates.complete = False

    case = {"slug": "case-truncated", "state": "DRAFT", "entities": []}
    api = _SearchStubApi([case], {"अंकुर खत्री": candidates})
    plan = plan_case_entities(api, case, 'W/"e"', [
        {"entity_name": "अंकुर खत्री", "relationship_type": "related", "notes": "क"}],
        strict=True)

    assert plan.action == "NOOP"
    assert plan.patch_items == []
    assert plan.bound == []
    assert len(plan.review) == 1
    _, decision, _section = plan.review[0]
    assert decision.nes_id is None
    assert "truncated mid-block" in decision.reason


def test_permissive_binds_through_a_truncated_window_and_says_so():
    # The riskiest promotion of the five, pinned deliberately: at the page cap a
    # same-name duplicate can sit just outside the window, so permissive mode is
    # binding on a candidate set it KNOWS is incomplete. That is the accepted
    # cost of the mode -- what must not happen is it binding silently, so the
    # reason has to survive onto the decision.
    candidates = CandidateList([ANKUR_CANDIDATE] + [
        {"id": f"https://jawafdehi.org/entity/person/filler-{i:03d}-aaaaaa",
         "title": {"ne": "फरक नाम"}, "score": ANKUR_CANDIDATE["score"]}
        for i in range(49)])
    candidates.complete = False

    case = {"slug": "case-truncated", "state": "DRAFT", "entities": []}
    api = _SearchStubApi([case], {"अंकुर खत्री": candidates},
                         documents={ANKUR_IRI: {"identifier": None}})
    plan = plan_case_entities(api, case, 'W/"e"', [
        {"entity_name": "अंकुर खत्री", "relationship_type": "related", "notes": "क"}])

    assert plan.action == "WOULD_PATCH"
    _name, decision, _notes, _section = plan.bound[0]
    assert decision.nes_id == ANKUR_IRI
    assert is_promoted(decision)
    assert "truncated mid-block" in decision.reason


def test_a_complete_window_binds_however_long_the_list_is():
    # The inverse, and the reason the count-based test had to go: a full-length
    # result set that search EXHAUSTED carries no truncation risk, and every bind
    # on one was previously thrown away.
    candidates = CandidateList([ANKUR_CANDIDATE] + [
        {"id": f"https://jawafdehi.org/entity/person/filler-{i:03d}-aaaaaa",
         "title": {"ne": "फरक नाम"}, "score": 1.0}
        for i in range(199)])
    candidates.complete = True
    assert len(candidates) == ENTITY_SEARCH_PAGE_SIZE * ENTITY_SEARCH_MAX_PAGES

    case = {"slug": "case-complete-window", "state": "DRAFT", "entities": []}
    api = _SearchStubApi([case], {"अंकुर खत्री": candidates})
    plan = plan_case_entities(api, case, 'W/"e"', [
        {"entity_name": "अंकुर खत्री", "relationship_type": "related", "notes": "क"}])

    assert plan.action == "WOULD_PATCH"
    assert [n for n, _, _, _ in plan.bound] == ["अंकुर खत्री"]


def test_plan_still_binds_when_the_candidate_list_is_one_short_of_the_cap():
    # The boundary: one candidate short of the cap must still bind normally --
    # proves the guard is keyed on the actual constant, not an off-by-one that
    # would also catch a merely-large-but-complete result.
    cap = ENTITY_SEARCH_PAGE_SIZE * ENTITY_SEARCH_MAX_PAGES
    filler = [
        {"id": f"https://jawafdehi.org/entity/person/filler-{i:03d}-aaaaaa",
         "title": {"ne": "फरक नाम"}, "score": 1.0}
        for i in range(cap - 2)
    ]
    candidates = [*filler, ANKUR_CANDIDATE]
    assert len(candidates) == cap - 1

    case = {"slug": "case-not-truncated", "state": "DRAFT", "entities": []}
    api = _SearchStubApi([case], {"अंकुर खत्री": candidates})
    plan = plan_case_entities(api, case, 'W/"e"', [
        {"entity_name": "अंकुर खत्री", "relationship_type": "related", "notes": "क"}])

    assert plan.action == "WOULD_PATCH"
    assert [i["nes_id"] for i in plan.patch_items] == [ANKUR_IRI]


# --- Item 4: no `required_state` keyword exists any more ------------------


def test_plan_case_entities_has_no_required_state_parameter():
    # The brief's own signature offered `required_state`, and its only
    # possible use (`required_state="IN_REVIEW"`) is exactly what this
    # module's REQUIRED_WRITE_STATE comment forbids -- IN_REVIEW's `notes`
    # come back blanked for a non-casework read, so merging it would wipe
    # every existing note. Zero callers ever passed it; removed rather than
    # left as a footgun with no user.
    import inspect

    params = inspect.signature(plan_case_entities).parameters
    assert "required_state" not in params


# --- The ETag visibility note (not a fix -- Task 7 enforces it) -----------


def test_plan_reason_names_a_missing_etag_for_visibility():
    case = {"slug": "case-noetag", "state": "DRAFT", "entities": []}
    api = _SearchStubApi([case])
    plan = plan_case_entities(api, case, None, [])
    assert "etag" in plan.reason.lower()


# --------------------------------------------------------------------------
# Task 7 -- conditional apply, and the three report files.
# --------------------------------------------------------------------------

from casework.enrich_related_entities import (  # noqa: E402
    apply_entity_plan,
    plan_summary,
    report_paths,
    write_jsonl,
    write_nomatch_report,
)


def test_apply_sends_the_captured_etag_as_if_match():
    case = {"slug": "case-x", "state": "DRAFT", "entities": []}
    api = _SearchStubApi([case], {"अंकुर खत्री": [ANKUR_CANDIDATE]})
    plan = plan_case_entities(api, case, 'W/"abc123"', [
        {"entity_name": "अंकुर खत्री", "relationship_type": "related", "notes": "क"}])
    apply_entity_plan(api, plan)

    slug, path, items, if_match = api.replace_list_calls[0]
    assert (slug, path, if_match) == ("case-x", "entities", 'W/"abc123"')
    assert [i["nes_id"] for i in items] == [ANKUR_IRI]


def test_apply_refuses_an_unconditional_write_when_no_etag_was_captured():
    case = {"slug": "case-x", "state": "DRAFT", "entities": []}
    api = _SearchStubApi([case], {"अंकुर खत्री": [ANKUR_CANDIDATE]})
    plan = plan_case_entities(api, case, None, [
        {"entity_name": "अंकुर खत्री", "relationship_type": "related", "notes": "क"}])
    with pytest.raises(RuntimeError, match="no ETag"):
        apply_entity_plan(api, plan)
    assert api.replace_list_calls == []


def test_apply_refuses_a_plan_that_is_not_would_patch():
    plan = ere.EntityBindPlan(slug="case-x", action="NOOP")
    with pytest.raises(ValueError, match="NOOP"):
        apply_entity_plan(_SearchStubApi([]), plan)


def test_apply_refuses_a_merged_item_missing_nes_id_and_never_calls_replace_list():
    # `apply_entity_plan` re-validates every item in the merged list, INCLUDING
    # pre-existing binds it did not add itself -- `plan_case_entities` only
    # validates the additions it builds, so a bad item already sitting on the
    # case (e.g. a hand-edited record, or a schema that changed under it)
    # would otherwise reach `replace_list` unchecked.
    api = _SearchStubApi([])
    plan = ere.EntityBindPlan(
        slug="case-bad-item", action="WOULD_PATCH", if_match='W/"e"',
        patch_items=[{"relationship_type": "related", "notes": "missing nes_id"}])
    with pytest.raises(ValueError, match="canonical"):
        apply_entity_plan(api, plan)
    assert api.replace_list_calls == []


def test_nomatch_report_ranks_by_how_many_cases_a_name_appears_in(tmp_path):
    from casework.entity_resolver import NO_MATCH as NM
    from casework.entity_resolver import Decision

    def d():
        return Decision(NM, None, 0.0, "", "no NES entity scored high enough", ())

    rows = [("जिल्ला शिक्षा कार्यालय, दाङ", "case-a", d(), "related"),
            ("जिल्ला शिक्षा कार्यालय, दाङ", "case-b", d(), "related"),
            ("गुल्बा कोरी", "case-c", d(), "accused")]
    out = tmp_path / "run.nomatch.md"
    write_nomatch_report(out, rows)
    text = out.read_text(encoding="utf-8")
    assert text.index("जिल्ला शिक्षा कार्यालय, दाङ") < text.index("गुल्बा कोरी")
    assert "2" in text.splitlines()[text.splitlines().index(
        next(line for line in text.splitlines() if "दाङ" in line))]


def test_nomatch_report_escapes_table_breaking_characters(tmp_path):
    # Both cells hold text this module does not control -- an LLM-extracted name
    # and an NES title. A literal pipe or a newline in either ends the cell early
    # and shifts every column after it, so the row a caseworker is supposed to act
    # on becomes unreadable. This report IS the queue.
    from casework.entity_resolver import NO_MATCH as NM
    from casework.entity_resolver import Decision

    rows = [("मालपोत | कार्यालय", "case-a",
             Decision(NM, None, 0.42, "जिल्ला\nकार्यालय", "no match", ()),
             "related")]
    out = tmp_path / "run.nomatch.md"
    write_nomatch_report(out, rows)

    row = next(line for line in out.read_text(encoding="utf-8").splitlines()
               if "मालपोत" in line)
    # Five columns means five separators plus the bounding one; an unescaped pipe
    # or newline would change that count.
    assert row.count("|") - row.count(r"\|") == 6
    assert r"मालपोत \| कार्यालय" in row
    assert "जिल्ला कार्यालय" in row      # the newline became a space
    assert "\n" not in row


def test_report_paths_share_the_run_log_stem(tmp_path):
    paths = {"log": str(tmp_path / "20260803T101500Z-entities-abc.log"),
             "events": str(tmp_path / "20260803T101500Z-entities-abc.events.jsonl")}
    out = report_paths(paths)
    assert out["binds"].endswith("20260803T101500Z-entities-abc.binds.jsonl")
    assert out["review"].endswith("20260803T101500Z-entities-abc.review.jsonl")
    assert out["nomatch"].endswith("20260803T101500Z-entities-abc.nomatch.md")


# --- Correction 1: report_paths must not blind-slice a non-.log stem -----


def test_report_paths_does_not_mangle_a_log_path_without_a_log_suffix(tmp_path):
    # The brief's `str(Path(paths["log"]))[: -len(".log")]` unconditionally
    # chops the last 4 characters off ANY log path, .log or not -- garbling
    # the stem for a path that does not end in .log (e.g. a caller that
    # passes a bare run-id or a path with a different extension). Guarded:
    # the suffix is stripped only when it is actually present.
    stem_name = "20260803T101500Z-entities-abc"
    paths = {"log": str(tmp_path / stem_name)}
    out = report_paths(paths)
    assert out["binds"].endswith(f"{stem_name}.binds.jsonl")
    assert out["review"].endswith(f"{stem_name}.review.jsonl")
    assert out["nomatch"].endswith(f"{stem_name}.nomatch.md")
    # In particular, the last 4 characters of the real stem must survive --
    # the blind slice would have chopped "-abc" down to "-a".
    assert "abc.binds.jsonl" in out["binds"]


# --- Correction 2: write_jsonl needs its own round-trip test -------------


def test_write_jsonl_round_trips_one_object_per_line_with_devanagari_unescaped(
    tmp_path,
):
    out = tmp_path / "run.binds.jsonl"
    rows = [
        {"name": "अंकुर खत्री", "nes_id": ANKUR_IRI, "case": "case-a"},
        {"name": "गुल्बा कोरी", "nes_id": "https://jawafdehi.org/entity/person/x", "case": "case-b"},
    ]
    write_jsonl(out, rows)

    raw = out.read_bytes()
    text = raw.decode("utf-8")
    lines = text.splitlines()
    assert len(lines) == 2
    # Devanagari must appear as literal UTF-8 bytes, never as a \uXXXX escape
    # -- assert on the file's actual text, not on a value computed via the
    # same json.dumps(..., ensure_ascii=False) the code itself uses.
    assert "अंकुर खत्री" in text
    assert "गुल्बा कोरी" in text
    assert "\\u" not in text
    assert json.loads(lines[0]) == rows[0]
    assert json.loads(lines[1]) == rows[1]


# --- Correction 3: write_nomatch_report must keep the BEST candidate seen ---


def test_nomatch_report_keeps_the_best_scoring_candidate_in_a_group(tmp_path):
    # As briefed, write_nomatch_report takes near/score from the FIRST
    # decision seen for a normalised group and ignores every later one -- so
    # a later, higher-scoring near-miss in the same group is silently
    # dropped in favour of a worse one seen earlier. Here the SECOND row
    # scores higher than the first; the report must show the better one.
    from casework.entity_resolver import NO_MATCH as NM
    from casework.entity_resolver import Decision

    weak = Decision(NM, None, 0.40, "कमजोर मिल्दोजुल्दो", "no NES entity scored high enough", ())
    strong = Decision(NM, None, 0.83, "उत्तम मिल्दोजुल्दो", "no NES entity scored high enough", ())
    rows = [
        ("जिल्ला शिक्षा कार्यालय, दाङ", "case-a", weak, "related"),
        ("जिल्ला शिक्षा कार्यालय, दाङ", "case-b", strong, "related"),
    ]
    out = tmp_path / "run.nomatch.md"
    write_nomatch_report(out, rows)
    text = out.read_text(encoding="utf-8")
    line = next(line for line in text.splitlines() if "दाङ" in line)
    assert "उत्तम मिल्दोजुल्दो" in line
    assert "0.83" in line
    assert "कमजोर मिल्दोजुल्दो" not in line


# --- Correction 4: the summary must reconcile against extracted names ----


def test_plan_summary_reports_already_bound_names_instead_of_letting_them_vanish():
    # plan_case_entities (Task 6) drops a resolved BIND whose nes_id is
    # already on the case from bound, review AND nomatch alike -- correct
    # for a re-run (nothing new to write), but it means
    # len(bound)+len(review)+len(nomatch) alone silently undercounts the
    # extracted names on every re-run. plan_summary must surface the gap as
    # its own count so the totals add back up.
    case = {"slug": "case-x", "state": "DRAFT", "entities": [
        {"nes_id": ANKUR_IRI, "type": "related", "notes": "क"}]}
    api = _SearchStubApi([case], {"अंकुर खत्री": [ANKUR_CANDIDATE]})
    extracted_items = [
        {"entity_name": "अंकुर खत्री", "relationship_type": "related", "notes": "ख"}]
    plan = plan_case_entities(api, case, 'W/"e"', extracted_items)

    assert plan.action == "NOOP"
    assert plan.bound == []
    assert plan.review == []
    assert plan.nomatch == []

    summary = plan_summary(plan, extracted_items)
    assert summary["extracted"] == 1
    assert summary["bound"] == 0
    assert summary["review"] == 0
    assert summary["nomatch"] == 0
    assert summary["already_bound"] == 1
    assert (
        summary["bound"] + summary["review"] + summary["nomatch"]
        + summary["already_bound"] == summary["extracted"]
    )


def test_plan_summary_reconciles_on_the_ordinary_bind_review_nomatch_split():
    anish_a = {"id": "https://jawafdehi.org/entity/person/anish-shrestha-219986",
               "title": {"ne": "अनिष श्रेष्‍ठ"}, "score": 182.17}
    anish_b = {"id": "https://jawafdehi.org/entity/person/anish-shrestha-285096",
               "title": {"ne": "अनिष श्रेष्ठ"}, "score": 182.17}
    case = {"slug": "case-z", "state": "DRAFT", "entities": []}
    api = _SearchStubApi(
        [case], {"अंकुर खत्री": [ANKUR_CANDIDATE],
                 "अनिष श्रेष्ठ": [anish_a, anish_b], "खगेन्द्र पराजुली": []})
    extracted_items = [
        {"entity_name": "अंकुर खत्री", "relationship_type": "related", "notes": "क"},
        {"entity_name": "अनिष श्रेष्ठ", "relationship_type": "related", "notes": "ख"},
        {"entity_name": "खगेन्द्र पराजुली", "relationship_type": "related", "notes": "ग"},
    ]
    plan = plan_case_entities(api, case, 'W/"e"', extracted_items, strict=True)

    summary = plan_summary(plan, extracted_items)
    # Exact equality on purpose, and the exact KEY SET matters as much as the
    # values: every count here describes an extracted name, so the four must
    # reconcile with nothing left over. A future non-extraction source of names
    # must not add a key to this dict -- it would silently join the
    # `already_bound` subtraction and drive it negative, which is what the three
    # court-record keys that used to sit here existed to avoid.
    assert summary == {
        "extracted": 3, "bound": 1, "review": 1, "nomatch": 1, "created": 0, "already_bound": 0,
    }


def test_plan_summary_reconciles_when_permissive_mode_promotes_the_ambiguity():
    # Same three names, default mode: the ambiguity moves from `review` into
    # `bound`. The reconciliation must still close -- a promoted bind is counted
    # once, in one bucket, not double-counted or dropped.
    anish_a = {"id": "https://jawafdehi.org/entity/person/anish-shrestha-219986",
               "title": {"ne": "अनिष श्रेष्‍ठ"}, "score": 182.17}
    anish_b = {"id": "https://jawafdehi.org/entity/person/anish-shrestha-285096",
               "title": {"ne": "अनिष श्रेष्ठ"}, "score": 182.17}
    case = {"slug": "case-z", "state": "DRAFT", "entities": []}
    api = _SearchStubApi(
        [case], {"अंकुर खत्री": [ANKUR_CANDIDATE],
                 "अनिष श्रेष्ठ": [anish_a, anish_b], "खगेन्द्र पराजुली": []},
        documents={ANKUR_IRI: {"identifier": None},
                   anish_a["id"]: {"identifier": None},
                   anish_b["id"]: {"identifier": None}})
    extracted_items = [
        {"entity_name": "अंकुर खत्री", "relationship_type": "related", "notes": "क"},
        {"entity_name": "अनिष श्रेष्ठ", "relationship_type": "related", "notes": "ख"},
        {"entity_name": "खगेन्द्र पराजुली", "relationship_type": "related", "notes": "ग"},
    ]
    plan = plan_case_entities(api, case, 'W/"e"', extracted_items)

    # bound=3 from 3 names: अंकुर once, अनिष TWICE (both namesakes qualify). The
    # buckets deliberately no longer sum to `extracted` -- `bound` counts rows,
    # and `already_bound` is derived from names that produced no row at all, so
    # the old subtraction's -1 cannot come back.
    assert plan_summary(plan, extracted_items) == {
        "extracted": 3, "bound": 3, "review": 0, "nomatch": 1, "created": 0, "already_bound": 0,
    }


# --------------------------------------------------------------------------
# Task 8, fix round 1 -- the two refusal paths, the write-then-report order,
# and the report files `main()` actually writes.
# --------------------------------------------------------------------------


def _report_files():
    logger = logging.getLogger("casework.entities")
    return ere.report_paths(logger._casework_run_paths)


THREE_WAY_RESPONSE = with_evidence(json.dumps({
    "entities": [
        {"entity_name": "अंकुर खत्री", "relationship_type": "related", "notes": "क"},
        {"entity_name": "अनिष श्रेष्ठ", "relationship_type": "related", "notes": "ख"},
        {"entity_name": "खगेन्द्र पराजुली", "relationship_type": "related", "notes": "ग"},
    ],
    "accused_notes": [],
}), FIXTURE_EVIDENCE)

# One name binds outright, one is ambiguous between two same-name people (so it
# goes to review), one matches nothing in NES. Mirrors the real production
# split, where most extracted names have no NES entity at all.
ANISH_A = {"id": "https://jawafdehi.org/entity/person/anish-shrestha-219986",
           "title": {"ne": "अनिष श्रेष्‍ठ"}, "score": 182.17}
ANISH_B = {"id": "https://jawafdehi.org/entity/person/anish-shrestha-285096",
           "title": {"ne": "अनिष श्रेष्ठ"}, "score": 182.17}
THREE_WAY_SEARCH = {"अंकुर खत्री": [ANKUR_CANDIDATE],
                    "अनिष श्रेष्ठ": [ANISH_A, ANISH_B],
                    "खगेन्द्र पराजुली": []}


def test_a_non_draft_case_reports_nothing_as_already_bound(
    monkeypatch, patched_fetch_markdown, capsys
):
    # `ENRICHABLE_STATES` includes IN_REVIEW, so a non-DRAFT case really does
    # reach the planner, which refuses it. Before the fix, `plan_summary` still
    # ran on that refused plan and -- deriving `already_bound` by subtracting
    # three empty lists from the extracted count -- reported every extracted
    # name as already bound, while pointing at two empty report files.
    case = dict(PRESS_ONLY_CASE, slug="case-in-review", state="IN_REVIEW",
                entities=[])
    api = _SearchStubApi([case], THREE_WAY_SEARCH)
    report = _run_main(monkeypatch, api, invoke_text_stub=lambda **kw: THREE_WAY_RESPONSE,
                       argv=["--dry-run"])
    out = capsys.readouterr().out

    assert "TOTAL already bound (nothing to write): 0" in out
    assert "TOTAL reported for human review: 0" in out
    assert "TOTAL with no NES match: 0" in out
    rows = [r for r in report.rows if r["slug"] == "case-in-review"]
    assert [r["status"] for r in rows] == ["skipped"]


def test_a_payload_without_an_entities_key_is_an_error_not_already_bound(
    monkeypatch, patched_fetch_markdown, capsys
):
    # The planner's OTHER refusal. It leaves `action` at its "NOOP" default, so
    # a guard keyed on `action == "SKIP_STATE"` misses it and it falls into the
    # genuine-NOOP branch -- which is why the guard keys on `plan.examined`.
    # An incomplete read is a caller bug, so it is recorded as an error rather
    # than a routine skip.
    case = {k: v for k, v in PRESS_ONLY_CASE.items()}
    case["slug"] = "case-no-entities-key"
    case.pop("entities", None)
    assert "entities" not in case

    api = _SearchStubApi([case], THREE_WAY_SEARCH)
    report = _run_main(monkeypatch, api, invoke_text_stub=lambda **kw: THREE_WAY_RESPONSE,
                       argv=["--dry-run"])
    out = capsys.readouterr().out

    assert "TOTAL already bound (nothing to write): 0" in out
    rows = [r for r in report.rows if r["slug"] == "case-no-entities-key"]
    assert [r["status"] for r in rows] == ["error"]
    assert api.replace_list_calls == []


def test_a_failed_write_leaves_no_bound_row_and_no_bound_claim(
    monkeypatch, patched_fetch_markdown, capsys
):
    # A real, reproducible failure mode: no ETag was captured, so
    # `apply_entity_plan` refuses the unconditional whole-list replace. The
    # console and `*.binds.jsonl` must not claim a bind that never landed.
    case = dict(PRESS_ONLY_CASE, slug="case-write-fails", entities=[])
    api = _SearchStubApi([case], THREE_WAY_SEARCH)
    api.etag = None
    report = _run_main(monkeypatch, api, invoke_text_stub=lambda **kw: THREE_WAY_RESPONSE,
                       argv=["--apply"])
    out = capsys.readouterr().out

    assert "  BOUND " not in out
    assert "TOTAL bound to an EXISTING NES entity: 0" in out
    assert api.replace_list_calls == []
    assert Path(_report_files()["binds"]).read_text(encoding="utf-8") == ""
    statuses = [r["status"] for r in report.rows if r["slug"] == "case-write-fails"]
    assert "error" in statuses


def test_main_writes_the_three_report_files_with_the_right_rows(
    monkeypatch, patched_fetch_markdown
):
    # Nothing else pins which rows land in which file: swapping `bind_rows` for
    # `review_rows` at the `write_jsonl` calls passed the whole suite before
    # this test existed. Run under --strict, because that is the mode that still
    # produces one row of each kind -- the default promotes the ambiguity into a
    # bind and would leave the review file empty, testing nothing.
    case = dict(PRESS_ONLY_CASE, slug="case-three-way", entities=[])
    api = _SearchStubApi([case], THREE_WAY_SEARCH)
    _run_main(monkeypatch, api, invoke_text_stub=lambda **kw: THREE_WAY_RESPONSE,
              argv=["--apply", "--strict"])

    files = _report_files()
    binds = [json.loads(line) for line
             in Path(files["binds"]).read_text(encoding="utf-8").splitlines()]
    review = [json.loads(line) for line
              in Path(files["review"]).read_text(encoding="utf-8").splitlines()]
    nomatch = Path(files["nomatch"]).read_text(encoding="utf-8")

    assert [b["extracted"] for b in binds] == ["अंकुर खत्री"]
    assert binds[0]["nes_id"] == ANKUR_IRI
    assert binds[0]["written"] is True

    assert [r["extracted"] for r in review] == ["अनिष श्रेष्ठ"]
    assert "ambiguous" in review[0]["reason"]
    # The candidate list travels with the row, so a reviewer can reproduce the
    # decision from the file alone.
    assert len(review[0]["candidates"]) >= 2

    assert "खगेन्द्र पराजुली" in nomatch
    assert "अंकुर खत्री" not in nomatch


def test_dry_run_bind_rows_are_marked_unwritten(
    monkeypatch, patched_fetch_markdown
):
    case = dict(PRESS_ONLY_CASE, slug="case-dry-marked", entities=[])
    api = _SearchStubApi([case], THREE_WAY_SEARCH)
    _run_main(monkeypatch, api, invoke_text_stub=lambda **kw: THREE_WAY_RESPONSE,
              argv=["--dry-run"])

    binds = [json.loads(line) for line
             in Path(_report_files()["binds"]).read_text(encoding="utf-8").splitlines()]
    # THREE rows in the default mode: the clean match, plus BOTH namesakes of the
    # ambiguity. Every one marked unwritten -- a promoted bind is still only a
    # prediction in a dry run.
    assert [b["written"] for b in binds] == [False, False, False]
    assert api.replace_list_calls == []
    assert [b["reason"].startswith(PROMOTED_PREFIX) for b in binds] == [
        False, True, True]


# The end-to-end two-section check (one entity bound as a location and as
# `related`, each labelled in `*.binds.jsonl` and on the console) is
# `TestMainWiring.test_one_entity_bound_in_two_sections_is_labelled_per_section`.


# --------------------------------------------------------------------------
# Deferred items 7, 8 and 10 from the final review: a dry run must predict a
# real run, an all-skipped run must not describe names it never extracted, and
# the merged wire payload must be asserted on a case that already has a bind.
# --------------------------------------------------------------------------


def test_dry_run_refuses_what_apply_refuses_instead_of_promising_a_bind(
    monkeypatch, patched_fetch_markdown, capsys
):
    # The no-ETag branch of `plan_case_entities` sets `plan.reason` and keeps
    # resolving by design, so the plan reaches WOULD_PATCH with a bound name on
    # it. Dry run used to print WOULD BIND and record `would-bind` for exactly
    # the plan `--apply` errors on -- overstating the one output whose whole job
    # is to predict a real run.
    case = dict(PRESS_ONLY_CASE, slug="case-dry-no-etag", entities=[])
    api = _SearchStubApi([case], THREE_WAY_SEARCH)
    api.etag = None
    report = _run_main(monkeypatch, api, invoke_text_stub=lambda **kw: THREE_WAY_RESPONSE,
                       argv=["--dry-run"])
    out = capsys.readouterr().out

    assert "WOULD BIND" not in out
    assert "WOULD REFUSE" in out
    assert "no ETag" in out
    assert "TOTAL that WOULD bind to an EXISTING NES entity (dry run, nothing written): 0" in out
    statuses = [r["status"] for r in report.rows if r["slug"] == "case-dry-no-etag"]
    assert statuses == ["would-refuse"]
    assert Path(_report_files()["binds"]).read_text(encoding="utf-8") == ""
    assert api.replace_list_calls == []


def test_the_dry_run_refusal_and_the_apply_refusal_are_the_same_check():
    # One copy of the preconditions, so the pair cannot drift. `--apply` raises
    # and dry run reports, over the identical conditions in the identical order.
    no_etag = ere.EntityBindPlan(
        slug="case-x", action="WOULD_PATCH", if_match=None,
        patch_items=[{"nes_id": ANKUR_IRI, "relationship_type": "related", "notes": ""}])
    bad_item = ere.EntityBindPlan(
        slug="case-y", action="WOULD_PATCH", if_match='W/"e"',
        patch_items=[{"relationship_type": "related", "notes": "no nes_id"}])
    writable = ere.EntityBindPlan(
        slug="case-z", action="WOULD_PATCH", if_match='W/"e"',
        patch_items=[{"nes_id": ANKUR_IRI, "relationship_type": "related", "notes": ""}])

    assert "no ETag" in ere.entity_plan_refusal(no_etag)
    assert "canonical" in ere.entity_plan_refusal(bad_item)
    assert ere.entity_plan_refusal(ere.EntityBindPlan(slug="case-n", action="NOOP"))
    assert ere.entity_plan_refusal(writable) == ""

    for plan in (no_etag, bad_item):
        with pytest.raises((ValueError, RuntimeError)):
            apply_entity_plan(_SearchStubApi([]), plan)


def test_an_all_skipped_run_does_not_claim_names_went_to_review(
    monkeypatch, patched_fetch_markdown, capsys
):
    # Every case skipped on the idempotency gate, so nothing was extracted and
    # both report files are empty. The zero-bound footer used to say "Every
    # extracted name either went to review or matched no NES entity -- see the
    # two files above", describing names that do not exist and files that are
    # empty.
    already = dict(PRESS_ONLY_CASE, slug="case-all-skipped", entities=[
        {"nes_id": ANKUR_IRI, "type": "related", "notes": "पहिल्यै"},
        ALREADY_BOUND_DISTRICT,
    ])
    api = _SearchStubApi([already], THREE_WAY_SEARCH)
    stub = _call_tracking_stub(THREE_WAY_RESPONSE)
    _run_main(monkeypatch, api, invoke_text_stub=stub, argv=["--dry-run"])
    out = capsys.readouterr().out

    assert stub.calls == [], "the case should have been skipped before the LLM"
    assert "TOTAL entities extracted across all cases: 0" in out
    assert "bound zero entities because it extracted none" in out
    assert "Every extracted name either went to review" not in out
    files = _report_files()
    assert Path(files["review"]).read_text(encoding="utf-8") == ""


def test_apply_over_a_case_that_already_has_a_bind_writes_both_rows(
    monkeypatch, patched_fetch_markdown
):
    # The wire payload, end to end, for the shape `replace_list` makes dangerous:
    # a case that already carries a bind. Every omitted row is DELETED by the
    # whole-list replace, so the request body must carry the pre-existing bind
    # unchanged -- notes intact, relationship_type intact -- ahead of the new one.
    # No other test asserts this on a `main() --apply` run.
    case = dict(PRESS_ONLY_CASE, slug="case-merge-wire", entities=[
        {"nes_id": EXISTING_IRI, "display_name": "गोपाल बहादुर श्रेष्ठ",
         "entity_type": "Person", "type": "accused", "outcome": "convicted",
         "notes": "तत्कालीन अध्यक्ष"},
    ])
    api = _SearchStubApi([case], THREE_WAY_SEARCH)
    # --strict keeps the payload to exactly the pre-existing bind plus one new
    # one, which is what makes the byte-for-byte assertion below readable. What
    # this test guards -- that the merge never drops the human's row -- does not
    # depend on the mode.
    _run_main(monkeypatch, api, invoke_text_stub=lambda **kw: THREE_WAY_RESPONSE,
              argv=["--apply", "--strict"])

    assert len(api.replace_list_calls) == 1
    slug, path, items, if_match = api.replace_list_calls[0]
    assert (slug, path, if_match) == ("case-merge-wire", "entities", 'W/"abc123"')
    assert items == [
        # The human's bind, byte for byte as `current_entity_binds` read it.
        # `outcome` is deliberately absent so the server preserves 'convicted'.
        {"nes_id": EXISTING_IRI, "relationship_type": "accused",
         "notes": "तत्कालीन अध्यक्ष"},
        {"nes_id": ANKUR_IRI, "relationship_type": "related", "notes": "क"},
    ]


# --------------------------------------------------------------------------
# Extraction visibility -- what the model said, for every extracted name.
#
# Motivated by production run 645b1483 (2026-08-05, case 078-CR-0038): 13
# entities extracted, 0 bind, 0 review, 13 no-match. The run recorded COUNTS
# only, so the one thing a caseworker needed -- what each of the 13 names was
# said to BE -- reached no file. `bound` and `review` rows already carry their
# section; `nomatch` dropped it, and nothing recorded the extraction itself.
# --------------------------------------------------------------------------


def _nomatch_decision(score=0.0, near=""):
    from casework.entity_resolver import NO_MATCH as NM
    from casework.entity_resolver import Decision

    return Decision(NM, None, score, near, "no NES entity scored high enough", ())


def test_nomatch_rows_carry_the_section_they_were_extracted_under():
    # The section is the most useful triage field on an unresolved row: it says
    # whether the missing NES entity is an accused person or a district office.
    # `bound` and `review` carry it for a documented reason -- two extracted
    # items can name the same person under different sections, so it cannot be
    # recovered from the name afterwards. That reasoning applies here unchanged.
    #
    # `location` is deliberately NOT one of the two sections here any more: it
    # never reaches `nomatch` at all now (`plan.review` catches it before any
    # search), so `witness` stands in as the second non-`related` section.
    case = {"slug": "case-nomatch-sections", "state": "DRAFT", "entities": []}
    api = _SearchStubApi([case], {"हेम राज बिष्ट": [],
                                  "वन निर्देशनालय, धनगढी": []})
    plan = plan_case_entities(api, case, 'W/"e"', [
        {"entity_name": "हेम राज बिष्ट", "relationship_type": "related",
         "notes": "क"},
        {"entity_name": "वन निर्देशनालय, धनगढी", "relationship_type": "witness",
         "notes": "ख"}], strict=True)

    assert [(name, section) for name, _decision, section in plan.nomatch] == [
        ("हेम राज बिष्ट", "related"),
        ("वन निर्देशनालय, धनगढी", "witness"),
    ]


def test_nomatch_report_shows_the_section_for_each_unmatched_name(tmp_path):
    # The report IS the caseworker's queue for creating NES entities. Creating a
    # person and creating a district office are different jobs, and the queue
    # could not tell them apart.
    rows = [("हेम राज बिष्ट", "case-a", _nomatch_decision(), "related"),
            ("वन निर्देशनालय, धनगढी", "case-b", _nomatch_decision(), "location")]
    out = tmp_path / "run.nomatch.md"
    write_nomatch_report(out, rows)

    lines = out.read_text(encoding="utf-8").splitlines()
    assert "related" in next(line for line in lines if "हेम राज" in line)
    assert "location" in next(line for line in lines if "निर्देशनालय" in line)


def test_nomatch_report_lists_every_section_a_grouped_name_appeared_under(tmp_path):
    # The report groups by normalised name across cases, so one group can hold
    # rows extracted under different sections. Showing only the first would tell
    # a caseworker the name is a location when another case called it accused.
    rows = [("सुर्खेत जिल्ला", "case-a", _nomatch_decision(), "location"),
            ("सुर्खेत जिल्ला", "case-b", _nomatch_decision(), "related")]
    out = tmp_path / "run.nomatch.md"
    write_nomatch_report(out, rows)

    row = next(line for line in out.read_text(encoding="utf-8").splitlines()
               if "सुर्खेत" in line)
    assert "location" in row and "related" in row


def test_report_paths_includes_the_new_sidecars():
    paths = {"log": "/tmp/20260805T121433Z-entities-645b1483.log"}
    out = report_paths(paths)
    stem = "20260805T121433Z-entities-645b1483"
    assert out["extracted"].endswith(f"{stem}.extracted.jsonl")
    assert out["accused_notes"].endswith(f"{stem}.accused_notes.jsonl")
    assert out["created"].endswith(f"{stem}.created.jsonl")


NOTHING_RESOLVES_RESPONSE = with_evidence(json.dumps({
    "entities": [
        {"entity_name": "हेम राज बिष्ट", "relationship_type": "related",
         "notes": "तत्कालीन प्रमुख"},
        {"entity_name": "मानस नर्सरी, धनगढी", "relationship_type": "related",
         "notes": "ठेक्का पाएको फर्म"},
    ],
    "accused_notes": [
        {"name": "हेम राज बिष्ट", "notes": "वन अधिकृत, वन निर्देशनालय धनगढी"},
    ],
}), FIXTURE_EVIDENCE)


def test_extraction_sidecar_records_every_name_when_nothing_resolves(
    monkeypatch, patched_fetch_markdown
):
    # The run that motivated this: every extracted name failed to resolve, so
    # binds.jsonl and review.jsonl were both empty and the $0.34 the extraction
    # cost bought a report of counts. The sidecar is the only place the model's
    # own answer survives a zero-bind run.
    case = dict(PRESS_ONLY_CASE, slug="case-nothing-resolves", entities=[])
    api = _SearchStubApi([case], {"हेम राज बिष्ट": [], "मानस नर्सरी, धनगढी": []})
    _run_main(monkeypatch, api,
              invoke_text_stub=lambda **kw: NOTHING_RESOLVES_RESPONSE,
              argv=["--dry-run"])

    rows = [json.loads(line) for line in Path(_report_files()["extracted"])
            .read_text(encoding="utf-8").splitlines()]

    assert [(r["extracted"], r["relationship_type"]) for r in rows] == [
        ("हेम राज बिष्ट", "related"),
        ("मानस नर्सरी, धनगढी", "related"),
    ]
    assert rows[0]["notes"] == "तत्कालीन प्रमुख"
    assert rows[0]["slug"] == "case-nothing-resolves"


def test_extraction_sidecar_records_accused_notes(
    monkeypatch, patched_fetch_markdown
):
    # accused_notes is a whole second section of the extraction that reached no
    # output file at all -- the run log counted them and nothing else.
    case = dict(PRESS_ONLY_CASE, slug="case-accused-notes", entities=[])
    api = _SearchStubApi([case], {"हेम राज बिष्ट": [], "मानस नर्सरी, धनगढी": []})
    _run_main(monkeypatch, api,
              invoke_text_stub=lambda **kw: NOTHING_RESOLVES_RESPONSE,
              argv=["--dry-run"])

    notes = [json.loads(line) for line in Path(_report_files()["accused_notes"])
             .read_text(encoding="utf-8").splitlines()]
    assert notes == [{"slug": "case-accused-notes",
                      "name": "हेम राज बिष्ट",
                      "notes": "वन अधिकृत, वन निर्देशनालय धनगढी",
                      "evidence": FIXTURE_EVIDENCE}]


# --------------------------------------------------------------------------
# Draft-case enrichment binds or creates -- it never reviews.
#
# Gaurav set this on 2026-08-05: deciding which entities deserve to exist is a
# later pass, so this stage stops producing a review queue. Three behaviours
# change, each pinned below.
# --------------------------------------------------------------------------


TWO_NAMESAKES_ABOVE_THRESHOLD = {
    "अनिष श्रेष्ठ": [
        {"id": "https://jawafdehi.org/entity/person/anish-shrestha-219986",
         "title": {"ne": "अनिष श्रेष्‍ठ"}, "score": 182.17},
        {"id": "https://jawafdehi.org/entity/person/anish-shrestha-285096",
         "title": {"ne": "अनिष श्रेष्ठ"}, "score": 182.17},
    ],
}


def test_ambiguity_binds_every_qualifying_candidate():
    # Was: promote the top candidate, drop the runners-up. Now: bind all of them
    # and let the later filtering pass decide. Two NES rows scoring identically
    # are usually one person entered twice; when they are two different people
    # sharing a name, both get bound and a human unpicks it later.
    case = {"slug": "case-both-namesakes", "state": "DRAFT", "entities": []}
    api = _SearchStubApi([case], TWO_NAMESAKES_ABOVE_THRESHOLD)
    plan = plan_case_entities(api, case, 'W/"e"', [
        {"entity_name": "अनिष श्रेष्ठ", "relationship_type": "related",
         "notes": "क"}])

    assert plan.action == "WOULD_PATCH"
    assert plan.review == []
    bound_ids = sorted(item["nes_id"] for item in plan.patch_items)
    assert bound_ids == [
        "https://jawafdehi.org/entity/person/anish-shrestha-219986",
        "https://jawafdehi.org/entity/person/anish-shrestha-285096",
    ]
    # One extracted name, two bind rows -- both must be reported, or binds.jsonl
    # under-reports what reached the case.
    assert len(plan.bound) == 2


def test_ambiguity_still_reviews_nothing_under_strict():
    # `--strict` is untouched: it remains the conservative pipeline for anyone
    # who wants an ambiguity held rather than bound.
    case = {"slug": "case-strict-ambiguity", "state": "DRAFT", "entities": []}
    api = _SearchStubApi([case], TWO_NAMESAKES_ABOVE_THRESHOLD)
    plan = plan_case_entities(api, case, 'W/"e"', [
        {"entity_name": "अनिष श्रेष्ठ", "relationship_type": "related",
         "notes": "क"}], strict=True)

    assert plan.bound == []
    assert len(plan.review) == 1


def test_a_candidate_below_the_threshold_is_not_bound_alongside_a_qualifying_one():
    # "Bind every qualifying candidate" means every one at or above
    # MIN_BIND_SCORE, not every one the search returned. A weak near-miss riding
    # along on a strong match would bind an unrelated entity.
    case = {"slug": "case-one-strong-one-weak", "state": "DRAFT", "entities": []}
    strong = {"id": "https://jawafdehi.org/entity/person/anish-shrestha-219986",
              "title": {"ne": "अनिष श्रेष्ठ"}, "score": 182.17}
    weak = {"id": "https://jawafdehi.org/entity/person/anisha-shah-111111",
            "title": {"ne": "अनिशा शाह"}, "score": 12.0}
    api = _SearchStubApi([case], {"अनिष श्रेष्ठ": [strong, weak]})
    plan = plan_case_entities(api, case, 'W/"e"', [
        {"entity_name": "अनिष श्रेष्ठ", "relationship_type": "related",
         "notes": "क"}])

    assert [item["nes_id"] for item in plan.patch_items] == [
        "https://jawafdehi.org/entity/person/anish-shrestha-219986"]


def test_cross_script_only_match_is_bound_not_held():
    # REVERSES `test_permissive_mode_refuses_to_promote_a_cross_script_only_match`.
    # That veto was the one permissive mode left standing, because कमल (a man)
    # and कमला (a woman) score 0.96 across scripts and 0.00 within Devanagari --
    # so this bind names a different person than the case charges. Bound anyway
    # per the 2026-08-05 decision: no review queue in this stage.
    case = {"slug": "case-kamal-bound", "state": "DRAFT", "entities": []}
    api = _SearchStubApi([case], {"कमल थापा": [KAMALA_LATIN_ONLY]})
    plan = plan_case_entities(api, case, 'W/"e"', [
        {"entity_name": "कमल थापा", "relationship_type": "related", "notes": "क"}])

    assert plan.action == "WOULD_PATCH"
    assert plan.review == []
    assert [item["nes_id"] for item in plan.patch_items] == [KAMALA_IRI]


def test_an_unaccepted_relationship_type_is_coerced_to_related():
    # Was: review, because there was no section to bind into. Now: coerced to
    # `related`, the prompt's own default. This is not cosmetic -- PATCH
    # /entities validates the whole list, so one unaccepted section fails every
    # bind on the case, not just its own row.
    case = {"slug": "case-bad-section", "state": "DRAFT", "entities": []}
    api = _SearchStubApi([case], {"अंकुर खत्री": [ANKUR_CANDIDATE]})
    plan = plan_case_entities(api, case, 'W/"e"', [
        {"entity_name": "अंकुर खत्री", "relationship_type": "supervisor",
         "notes": "क"}])

    assert plan.review == []
    assert [item["relationship_type"] for item in plan.patch_items] == ["related"]


def test_a_missing_relationship_type_is_coerced_to_related():
    case = {"slug": "case-no-section", "state": "DRAFT", "entities": []}
    api = _SearchStubApi([case], {"अंकुर खत्री": [ANKUR_CANDIDATE]})
    plan = plan_case_entities(api, case, 'W/"e"', [
        {"entity_name": "अंकुर खत्री", "notes": "क"}])

    assert plan.review == []
    assert [item["relationship_type"] for item in plan.patch_items] == ["related"]


def test_the_coercion_is_recorded_so_a_reader_can_see_it_happened():
    # A silently relabelled section is a section nobody asserted. The original
    # value rides on the plan so the run log and the reports can name it.
    case = {"slug": "case-coercion-recorded", "state": "DRAFT", "entities": []}
    api = _SearchStubApi([case], {"अंकुर खत्री": [ANKUR_CANDIDATE]})
    plan = plan_case_entities(api, case, 'W/"e"', [
        {"entity_name": "अंकुर खत्री", "relationship_type": "supervisor",
         "notes": "क"}])

    assert plan.coerced == [("अंकुर खत्री", "supervisor", "related")]


def test_coercion_does_not_fire_for_an_accepted_section():
    case = {"slug": "case-good-section", "state": "DRAFT", "entities": []}
    api = _SearchStubApi([case], {"अंकुर खत्री": [ANKUR_CANDIDATE]})
    plan = plan_case_entities(api, case, 'W/"e"', [
        {"entity_name": "अंकुर खत्री", "relationship_type": "witness",
         "notes": "क"}])

    assert plan.coerced == []
    assert [item["relationship_type"] for item in plan.patch_items] == ["witness"]


def test_an_accused_name_never_reaches_the_case_at_all():
    # The one review this stage KEEPS. Escalating an already-characterised entity
    # to `accused` sets outcome=charged and asserts the person is the subject of
    # the case. Removing the review queue did not remove this guard, because the
    # alternative is not "bind it later", it is "publish a charge on an LLM's
    # say-so".
    case = {"slug": "case-escalation", "state": "DRAFT",
            "entities": [{"nes_id": ANKUR_IRI, "type": "related",
                          "notes": "पहिले नै जोडिएको"}]}
    api = _SearchStubApi([case], {"अंकुर खत्री": [ANKUR_CANDIDATE]})
    plan = plan_case_entities(api, case, 'W/"e"', [
        {"entity_name": "अंकुर खत्री", "relationship_type": "accused",
         "notes": "क"}])

    # Nothing to escalate and nothing to review: the section is refused before
    # resolution runs, so the case keeps exactly what it had.
    assert plan.review == []
    assert plan.patch_items == []
    assert plan.court_record_only == [("अंकुर खत्री", "accused")]


# --------------------------------------------------------------------------
# Creating the NES entity a name has no match for, then binding it.
#
# `--create-entities`, default OFF, on top of `--apply`. POST /api/entities has
# no sourcing gate (`entities/validation.py:150` checks @id, @type and name and
# nothing else) and the 2-distinct-publisher rule lives only in
# `manage.py bulk_ingest`, so entities created here publish unsourced. Accepted
# on 2026-08-05.
# --------------------------------------------------------------------------


CREATE_RESPONSE = with_evidence(json.dumps({
    "entities": [
        {"entity_name": "हेम राज बिष्ट", "relationship_type": "related",
         "entity_prefix": "person", "entity_type": "Person",
         "is_named_entity": True, "name_en": "",
         "notes": "तत्कालीन प्रमुख"},
        {"entity_name": "वन निर्देशनालय, धनगढी", "relationship_type": "related",
         "entity_prefix": "organization/government/district/dfo",
         "entity_type": "GovernmentOrganization",
         "is_named_entity": True, "name_en": "",
         "notes": "आरोपी कार्यरत रहेको निकाय"},
    ],
    "accused_notes": [],
}), FIXTURE_EVIDENCE)


def _created_rows():
    return [json.loads(line) for line in Path(_report_files()["created"])
            .read_text(encoding="utf-8").splitlines()]


def test_no_entity_is_created_without_the_flag_even_under_apply(
    monkeypatch, patched_fetch_markdown
):
    # THE SAFETY PROPERTY THAT MATTERS MOST. Creation opts in on TOP of --apply,
    # never with it, so upgrading this enricher cannot make an existing --apply
    # run start writing to NES.
    case = dict(PRESS_ONLY_CASE, slug="case-no-flag", entities=[])
    api = _SearchStubApi([case], {"हेम राज बिष्ट": [],
                                  "वन निर्देशनालय, धनगढी": []})
    _run_main(monkeypatch, api, invoke_text_stub=lambda **kw: CREATE_RESPONSE,
              argv=["--apply"])

    assert api.create_entity_calls == []
    # Both names stay unmatched and reach the no-match report, exactly as before.
    assert Path(_report_files()["created"]).read_text(encoding="utf-8") == ""


def test_creates_an_entity_for_an_unmatched_name_and_binds_it(
    monkeypatch, patched_fetch_markdown
):
    case = dict(PRESS_ONLY_CASE, slug="case-create-bind", entities=[])
    api = _SearchStubApi([case], {"हेम राज बिष्ट": [],
                                  "वन निर्देशनालय, धनगढी": []})
    _run_main(monkeypatch, api, invoke_text_stub=lambda **kw: CREATE_RESPONSE,
              argv=["--apply", "--create-entities"])

    # Both names were POSTed, under the prefix the extraction named.
    posted = sorted((p["prefix"], p["slug"]) for p in api.create_entity_calls)
    assert posted == [
        ("organization/government/district/dfo", "vana-nirdeshanalaya-dhanagadhi"),
        ("person", "hema-raja-bishta"),
    ]
    # ...and both reached the case, in the section the extraction gave them.
    _slug, _path, items, _etag = api.replace_list_calls[0]
    sections = {item["nes_id"].rsplit("/", 2)[-2]: item["relationship_type"]
                for item in items}
    assert sections["person"] == "related"
    assert sections["dfo"] == "related"


def test_a_dry_run_creates_nothing_but_reports_what_it_would_create(
    monkeypatch, patched_fetch_markdown
):
    case = dict(PRESS_ONLY_CASE, slug="case-create-dry", entities=[])
    api = _SearchStubApi([case], {"हेम राज बिष्ट": [],
                                  "वन निर्देशनालय, धनगढी": []})
    _run_main(monkeypatch, api, invoke_text_stub=lambda **kw: CREATE_RESPONSE,
              argv=["--dry-run", "--create-entities"])

    assert api.create_entity_calls == []
    assert api.replace_list_calls == []
    rows = _created_rows()
    assert [r["outcome"] for r in rows] == ["would-create", "would-create"]
    assert {r["extracted"] for r in rows} == {"हेम राज बिष्ट",
                                             "वन निर्देशनालय, धनगढी"}


def test_the_created_entity_cites_the_material_it_came_from(
    monkeypatch, patched_fetch_markdown
):
    # An entity created here has no sources, which the 2-publisher rule would
    # otherwise hold as staged. The citation is what keeps it traceable to the
    # document that justified it.
    case = dict(PRESS_ONLY_CASE, slug="case-create-citation", entities=[])
    api = _SearchStubApi([case], {"हेम राज बिष्ट": [],
                                  "वन निर्देशनालय, धनगढी": []})
    _run_main(monkeypatch, api, invoke_text_stub=lambda **kw: CREATE_RESPONSE,
              argv=["--apply", "--create-entities"])

    citations = {p["citation"] for p in api.create_entity_calls}
    assert citations == {"https://jawafdehi.org/material/ciaa/press_releases/1"}


TWO_SPELLINGS_RESPONSE = with_evidence(json.dumps({
    "entities": [
        {"entity_name": "वन निदेशनालय, धनगढी",
         "relationship_type": "related",
         "entity_prefix": "organization/government/district/dfo",
         "entity_type": "GovernmentOrganization",
         "is_named_entity": True, "name_en": "", "notes": "क"},
        {"entity_name": "वन निदेशनालय, धनगढी ",
         "relationship_type": "related",
         "entity_prefix": "organization/government/district/dfo",
         "entity_type": "GovernmentOrganization",
         "is_named_entity": True, "name_en": "", "notes": "ख"},
    ],
    "accused_notes": [],
}), FIXTURE_EVIDENCE)


def test_one_office_named_twice_creates_one_entity(
    monkeypatch, patched_fetch_markdown
):
    # Case 078-CR-0038 named the Dhangadhi forest directorate twice. Without the
    # within-run dedup that case creates two entities on its first run.
    case = dict(PRESS_ONLY_CASE, slug="case-two-spellings", entities=[])
    api = _SearchStubApi([case], {})
    _run_main(monkeypatch, api, invoke_text_stub=lambda **kw: TWO_SPELLINGS_RESPONSE,
              argv=["--apply", "--create-entities"])

    assert len(api.create_entity_calls) == 1
    # Two spellings, one entity, and one bind row -- the case cannot carry the
    # same entity twice in the same section, so the second bind merges away.
    _slug, _path, items, _etag = api.replace_list_calls[0]
    assert len(items) == 1
    assert items[0]["nes_id"].endswith("/vana-nideshanalaya-dhanagadhi")
    assert items[0]["relationship_type"] == "related"


# The other half of the dedup: two names that are the same STRING but not the
# same THING. A person and a school can carry one name -- the run cache is
# keyed on the prefix as well for exactly this reason, because the two live at
# different IRIs (`person/...` and `organization/...`) and the server would
# never 409 one against the other. Keyed on the name alone, the second case
# binds a PERSON entity as the organisation in a corruption case.
HOMONYM_PERSON_RESPONSE = with_evidence(json.dumps({
    "entities": [
        {"entity_name": "श्रीकृष्ण श्रेष्ठ", "relationship_type": "related",
         "entity_prefix": "person", "entity_type": "Person",
         "is_named_entity": True, "name_en": "", "notes": "क"},
    ],
    "accused_notes": [],
}), FIXTURE_EVIDENCE)

HOMONYM_ORG_RESPONSE = with_evidence(json.dumps({
    "entities": [
        {"entity_name": "श्रीकृष्ण श्रेष्ठ", "relationship_type": "related",
         "entity_prefix": "organization", "entity_type": "Organization",
         "is_named_entity": True, "name_en": "", "notes": "ख"},
    ],
    "accused_notes": [],
}), FIXTURE_EVIDENCE)


def test_the_same_name_under_a_different_prefix_is_not_reused(
    monkeypatch, patched_fetch_markdown
):
    person_case = dict(PRESS_ONLY_CASE, slug="case-homonym-person", entities=[])
    org_case = dict(PRESS_ONLY_CASE, slug="case-homonym-org", entities=[])
    api = _SearchStubApi([person_case, org_case], {"श्रीकृष्ण श्रेष्ठ": []})
    replies = iter([HOMONYM_PERSON_RESPONSE, HOMONYM_ORG_RESPONSE])
    _run_main(monkeypatch, api, invoke_text_stub=lambda **kw: next(replies),
              argv=["--apply", "--create-entities"])

    assert [p["prefix"] for p in api.create_entity_calls] == ["person", "organization"]
    bound = [items[0]["nes_id"] for _s, _p, items, _e in api.replace_list_calls]
    assert bound == [
        "https://jawafdehi.org/entity/person/shrikrishna-shreshtha",
        "https://jawafdehi.org/entity/organization/shrikrishna-shreshtha",
    ]
    # And the report agrees with itself: a row's prefix and its IRI can no
    # longer disagree, which is what a name-keyed reuse used to write.
    for row in _created_rows():
        assert f"/entity/{row['prefix']}/" in row["nes_id"]


# One name, two sections, contradictory metadata. `items_by_name` used to key on
# the name alone, so BOTH `plan.nomatch` entries read whichever item the model
# happened to emit last -- here the `person`/`is_named_entity: False` one, which
# fails the creation gate and takes the legitimate contractor down with it.
CROSSED_SECTIONS_RESPONSE = with_evidence(json.dumps({
    "entities": [
        {"entity_name": "गोरखा निर्माण सेवा", "relationship_type": "related",
         "entity_prefix": "organization/contractor",
         "entity_type": "Organization", "is_named_entity": True,
         "name_en": "Gorkha Construction Services", "notes": "ठेक्का पाएको फर्म"},
        {"entity_name": "गोरखा निर्माण सेवा", "relationship_type": "witness",
         "entity_prefix": "person", "entity_type": "Person",
         "is_named_entity": False, "name_en": "", "notes": "साक्षी"},
    ],
    "accused_notes": [],
}), FIXTURE_EVIDENCE)


def test_each_section_reads_its_own_extracted_item(
    monkeypatch, patched_fetch_markdown
):
    case = dict(PRESS_ONLY_CASE, slug="case-crossed-sections", entities=[])
    api = _SearchStubApi([case], {"गोरखा निर्माण सेवा": []})
    _run_main(monkeypatch, api,
              invoke_text_stub=lambda **kw: CROSSED_SECTIONS_RESPONSE,
              argv=["--apply", "--create-entities"])

    # The contractor is created from its OWN item -- its prefix, its English
    # name, its `is_named_entity`. The witness row is refused by its own.
    assert [p["prefix"] for p in api.create_entity_calls] == ["organization/contractor"]
    assert api.create_entity_calls[0]["slug"] == "gorkha-construction-services"

    by_role = {row["role"]: row for row in _created_rows()}
    assert by_role["related"]["outcome"] == "created"
    assert by_role["witness"]["outcome"] == "skipped"
    assert "is_named_entity" in by_role["witness"]["reason"]


COERCED_SECTION_RESPONSE = with_evidence(json.dumps({
    "entities": [
        {"entity_name": "साझा भण्डार सहकारी", "relationship_type": "employer",
         "entity_prefix": "organization", "entity_type": "Organization",
         "is_named_entity": True, "name_en": "Sajha Bhandar Cooperative",
         "notes": "क"},
    ],
    "accused_notes": [],
}), FIXTURE_EVIDENCE)


def test_a_coerced_section_still_finds_its_own_item(
    monkeypatch, patched_fetch_markdown
):
    # The other half of keying on the section: `employer` is not a section the
    # API accepts, so the planner coerces it to `related` and files the name
    # under THAT. A lookup keyed on the raw `relationship_type` misses, the item
    # comes back empty, and a perfectly good name is refused for having no
    # prefix -- a silent loss, since "no prefix" reads like a model failure.
    case = dict(PRESS_ONLY_CASE, slug="case-coerced-section", entities=[])
    api = _SearchStubApi([case], {"साझा भण्डार सहकारी": []})
    _run_main(monkeypatch, api,
              invoke_text_stub=lambda **kw: COERCED_SECTION_RESPONSE,
              argv=["--apply", "--create-entities"])

    assert [p["prefix"] for p in api.create_entity_calls] == ["organization"]
    row = _created_rows()[0]
    assert (row["role"], row["outcome"]) == ("related", "created")


BAD_PREFIX_RESPONSE = with_evidence(json.dumps({
    "entities": [
        {"entity_name": "हेम राज बिष्ट", "relationship_type": "related",
         "entity_prefix": "persen", "entity_type": "Person",
         "is_named_entity": True, "name_en": "", "notes": "क"},
    ],
    "accused_notes": [],
}), FIXTURE_EVIDENCE)


def test_a_prefix_with_no_existing_parent_is_skipped_not_posted(
    monkeypatch, patched_fetch_markdown
):
    # `persen` is a typo'd root: not in use and with no parent to vouch for it.
    # Creating it would strand the entity where no search filter reaches, and the
    # bad prefix would then report as live via /api/entity_prefixes.
    case = dict(PRESS_ONLY_CASE, slug="case-bad-prefix", entities=[])
    api = _SearchStubApi([case], {"हेम राज बिष्ट": []})
    _run_main(monkeypatch, api, invoke_text_stub=lambda **kw: BAD_PREFIX_RESPONSE,
              argv=["--apply", "--create-entities"])

    assert api.create_entity_calls == []
    rows = _created_rows()
    assert [r["outcome"] for r in rows] == ["skipped"]
    assert "persen" in rows[0]["reason"]


def test_an_already_exists_response_binds_the_existing_entity(
    monkeypatch, patched_fetch_markdown
):
    # Someone got there first, which is the good case. `create_entity` raises on a
    # duplicate @id (`publication/service.py:68`); the run must bind that IRI
    # rather than record an error.
    case = dict(PRESS_ONLY_CASE, slug="case-already-exists", entities=[])
    api = _SearchStubApi([case], {"हेम राज बिष्ट": [],
                                  "वन निर्देशनालय, धनगढी": []})
    api.create_conflicts = {"person/hema-raja-bishta"}
    _run_main(monkeypatch, api, invoke_text_stub=lambda **kw: CREATE_RESPONSE,
              argv=["--apply", "--create-entities"])

    _slug, _path, items, _etag = api.replace_list_calls[0]
    assert "https://jawafdehi.org/entity/person/hema-raja-bishta" in {
        item["nes_id"] for item in items}
    outcomes = {r["extracted"]: r["outcome"] for r in _created_rows()}
    assert outcomes["हेम राज बिष्ट"] == "already-exists"


def test_a_failed_post_skips_that_name_and_keeps_the_rest_of_the_case(
    monkeypatch, patched_fetch_markdown
):
    # One name's POST failing must not cost the case its other binds.
    case = dict(PRESS_ONLY_CASE, slug="case-post-fails", entities=[])
    api = _SearchStubApi([case], {"हेम राज बिष्ट": [],
                                  "वन निर्देशनालय, धनगढी": []})
    api.create_errors = {"person/hema-raja-bishta": RuntimeError("500 boom")}
    _run_main(monkeypatch, api, invoke_text_stub=lambda **kw: CREATE_RESPONSE,
              argv=["--apply", "--create-entities"])

    _slug, _path, items, _etag = api.replace_list_calls[0]
    bound_ids = {item["nes_id"] for item in items}
    assert "https://jawafdehi.org/entity/person/hema-raja-bishta" not in bound_ids
    assert any("dfo" in nes_id for nes_id in bound_ids)
    outcomes = {r["extracted"]: r["outcome"] for r in _created_rows()}
    assert outcomes["हेम राज बिष्ट"] == "error"


def test_creation_preserves_binds_already_on_the_case(
    monkeypatch, patched_fetch_markdown
):
    # PATCH /entities replaces the WHOLE list. A create step that sent only its
    # new entities would delete the press release and court order binds someone
    # attached last month.
    # The existing bind is `accused`, NOT `related`, on purpose: the idempotency
    # gate counts `related` binds only, so a case carrying one is skipped whole
    # and never reaches the create step at all (see the test below).
    existing = {"nes_id": ANKUR_IRI, "type": "accused", "outcome": "charged",
                "notes": "पहिलेको टिप्पणी"}
    case = dict(PRESS_ONLY_CASE, slug="case-preserve", entities=[existing])
    api = _SearchStubApi([case], {"हेम राज बिष्ट": [],
                                  "वन निर्देशनालय, धनगढी": []})
    _run_main(monkeypatch, api, invoke_text_stub=lambda **kw: CREATE_RESPONSE,
              argv=["--apply", "--create-entities"])

    _slug, _path, items, _etag = api.replace_list_calls[0]
    kept = [item for item in items if item["nes_id"] == ANKUR_IRI]
    assert len(kept) == 1
    assert kept[0]["notes"] == "पहिलेको टिप्पणी"
    assert len(items) == 3       # the existing one plus the two created


def test_a_case_with_a_related_bind_never_reaches_the_create_step(
    monkeypatch, patched_fetch_markdown
):
    # A LIMITATION, PINNED RATHER THAN FIXED. The idempotency gate skips a case
    # that already holds a `related` bind and a district, and it runs before
    # anything else -- so on an already-enriched case, --create-entities creates
    # nothing, however many unmatched names that case has. `--force` is the way
    # past it.
    existing = {"nes_id": ANKUR_IRI, "type": "related", "notes": "पहिलेको"}
    case = dict(PRESS_ONLY_CASE, slug="case-gated", entities=[existing, ALREADY_BOUND_DISTRICT])
    api = _SearchStubApi([case], {"हेम राज बिष्ट": [],
                                  "वन निर्देशनालय, धनगढी": []})
    _run_main(monkeypatch, api, invoke_text_stub=lambda **kw: CREATE_RESPONSE,
              argv=["--apply", "--create-entities"])

    assert api.create_entity_calls == []
    assert api.replace_list_calls == []


def test_force_gets_past_the_gate_and_creates(
    monkeypatch, patched_fetch_markdown
):
    existing = {"nes_id": ANKUR_IRI, "type": "related", "notes": "पहिलेको"}
    case = dict(PRESS_ONLY_CASE, slug="case-forced", entities=[existing, ALREADY_BOUND_DISTRICT])
    api = _SearchStubApi([case], {"हेम राज बिष्ट": [],
                                  "वन निर्देशनालय, धनगढी": []})
    _run_main(monkeypatch, api, invoke_text_stub=lambda **kw: CREATE_RESPONSE,
              argv=["--apply", "--create-entities", "--force"])

    assert len(api.create_entity_calls) == 2
    _slug, _path, items, _etag = api.replace_list_calls[0]
    assert ANKUR_IRI in {item["nes_id"] for item in items}


def test_the_prefix_section_lists_every_live_category():
    section = ere.prefix_prompt_section(
        ["person", "organization/government/district/dfo", "location/district"])
    assert "person" in section
    assert "organization/government/district/dfo" in section
    # Sorted, so two runs over the same prefix list build a byte-identical
    # prompt. A reshuffled list is a different prompt for no reason.
    assert section.index("location/district") < section.index("person")


def test_the_prefix_section_is_empty_without_prefixes():
    # An instruction to choose from an empty list makes the model invent values,
    # and every invented value is then discarded -- an expensive way to create
    # nothing.
    assert ere.prefix_prompt_section([]) == ""
    assert ere.prefix_prompt_section(None) == ""


def test_the_system_prompt_asks_for_the_two_new_fields():
    for prompt in (ere.COURT_ORDER_SYSTEM_PROMPT, ere.PRESS_RELEASE_SYSTEM_PROMPT):
        assert "entity_prefix" in prompt
        assert "entity_type" in prompt


def test_the_category_list_is_absent_from_the_prompt_without_the_flag(
    monkeypatch, patched_fetch_markdown
):
    # Two fields nobody reads cost prompt budget on a stage where the budget is
    # already the binding constraint, so the list only ships when it can be used.
    case = dict(PRESS_ONLY_CASE, slug="case-no-prefix-prompt", entities=[])
    api = _SearchStubApi([case], {"अंकुर खत्री": [ANKUR_CANDIDATE]})
    seen = {}

    def stub(**kw):
        seen.update(kw)
        return json.dumps({"entities": [], "accused_notes": []})

    _run_main(monkeypatch, api, invoke_text_stub=stub, argv=["--dry-run"])
    assert "ENTITY CATEGORY" not in seen["system"]


def test_the_category_list_ships_with_the_flag(
    monkeypatch, patched_fetch_markdown
):
    case = dict(PRESS_ONLY_CASE, slug="case-prefix-prompt", entities=[])
    api = _SearchStubApi([case], {"अंकुर खत्री": [ANKUR_CANDIDATE]})
    seen = {}

    def stub(**kw):
        seen.update(kw)
        return json.dumps({"entities": [], "accused_notes": []})

    _run_main(monkeypatch, api, invoke_text_stub=stub,
              argv=["--dry-run", "--create-entities"])
    assert "ENTITY CATEGORY" in seen["system"]
    assert "organization/government/district/dfo" in seen["system"]


def test_a_created_name_is_not_counted_as_already_bound():
    # Found on the first production dry run (226fb34b, case 078-CR-0038): 13
    # extracted, 1 bound, 12 would-create, 0 no-match -- reported as "already
    # bound (nothing to write): 12". The create step removes a name from
    # `plan.nomatch`, so it produced no row in bound/review/nomatch and the
    # accounted-for check counted it as dropped-because-already-bound. Every
    # created entity was reported as work that did not need doing.
    plan = ere.EntityBindPlan(slug="case-count", action="NOOP", examined=True)
    plan.bound = [("नागरिक लगानी कोष", None, "क", "related")]
    plan.created = ["हेम राज विष्ट", "सविना आले"]
    items = [{"entity_name": "नागरिक लगानी कोष"},
             {"entity_name": "हेम राज विष्ट"},
             {"entity_name": "सविना आले"}]

    summary = ere.plan_summary(plan, items)
    assert summary["created"] == 2
    assert summary["already_bound"] == 0


def test_a_genuinely_already_bound_name_is_still_counted():
    # The counter must keep working: a name that resolved to an entity already on
    # the case in that section produces no row anywhere, and that IS
    # already-bound.
    plan = ere.EntityBindPlan(slug="case-count-2", action="NOOP", examined=True)
    plan.bound = [("नागरिक लगानी कोष", None, "क", "related")]
    items = [{"entity_name": "नागरिक लगानी कोष"},
             {"entity_name": "पहिले नै जोडिएको नाम"}]

    summary = ere.plan_summary(plan, items)
    assert summary["already_bound"] == 1
    assert summary["created"] == 0


# --------------------------------------------------------------------------
# CaseworkApi.create_entity's error mapping.
#
# Found by the local harness run, not by a unit test: the first version keyed
# already-exists off 422, which is what the view returns for a VALIDATION
# failure. A duplicate IRI goes through `_map_service_value_error` instead and
# comes back 409 ENTITY_EXISTS (`entities/views.py:420`), so every re-run
# recorded `error` and rebound nothing.
# --------------------------------------------------------------------------


def _http_error(code, body):
    import io
    import urllib.error

    return urllib.error.HTTPError(
        "http://127.0.0.1:48010/api/entities", code, "err", {},
        io.BytesIO(body.encode("utf-8")))


def _api_raising(exc):
    from casework.common.api import CaseworkApi

    api = CaseworkApi("http://127.0.0.1:48010", basic=("u", "p"))

    def boom(*a, **kw):
        raise exc

    api._request = boom
    return api


def test_a_409_conflict_becomes_entity_already_exists():
    from casework.common.api import EntityAlreadyExists

    api = _api_raising(_http_error(409, json.dumps({"error": {
        "code": "ENTITY_EXISTS",
        "message": "Entity https://jawafdehi.org/entity/person/hema-raja-vishta "
                   "already exists"}})))
    with pytest.raises(EntityAlreadyExists):
        api.create_entity({"prefix": "person", "slug": "hema-raja-vishta",
                           "type": "Person", "name": {"ne": "हेम राज विष्ट"}})


def test_a_422_validation_failure_is_not_mistaken_for_a_conflict():
    # 422 is the view's VALIDATION status. Treating it as already-exists would
    # bind a nonexistent IRI on every malformed payload.
    from casework.common.api import EntityAlreadyExists

    api = _api_raising(_http_error(422, json.dumps({"error": {
        "code": "VALIDATION_ERROR",
        "message": "@type must be a known schema.org/jawafdehi type"}})))
    with pytest.raises(ValueError) as caught:
        api.create_entity({"prefix": "person", "slug": "x", "type": "Nonsense",
                           "name": {"ne": "क"}})
    assert not isinstance(caught.value, EntityAlreadyExists)


def test_a_500_propagates_untouched():
    api = _api_raising(_http_error(500, "boom"))
    with pytest.raises(Exception) as caught:
        api.create_entity({"prefix": "person", "slug": "x", "type": "Person",
                           "name": {"ne": "क"}})
    assert "500" in str(caught.value)


# --------------------------------------------------------------------------
# Four gates in front of the POST.
#
# Creating an entity is permanent and public, so each gate refuses on a
# different ground: the section it came from, the shape of the string, the
# model's own verdict on whether the string names a thing, and identity.
# A refused name still BINDS whatever it matched -- these gate creation only.
# The order is fixed in `_cannot_create`, which carries the reasoning for each.
# --------------------------------------------------------------------------


# `test_a_location_is_never_created_even_when_everything_else_is_valid`
# DELETED here (Task 5). It drove a `location`-typed extracted item all the
# way through `main()` to the create step's `_cannot_create` location gate --
# that item now never reaches `nomatch` (and so never reaches the create
# step) at all, since `plan_case_entities` refuses it straight to
# `plan.review` before any search. `_cannot_create`'s `section ==
# LOCATION_SECTION` branch is consequently unreachable via `main()` today;
# left in place rather than removed here, since this task's scope is
# `plan_case_entities` only.


COMPOSITE_RELATED_RESPONSE = with_evidence(json.dumps({
    "entities": [
        {"entity_name": "घरजग्गा सम्पत्ति - काठमाडौं", "relationship_type": "related",
         "entity_prefix": "organization", "entity_type": "Organization",
         "is_named_entity": True, "name_en": "Real estate property - Kathmandu",
         "notes": "जफत गरिएको सम्पत्ति"},
    ],
    "accused_notes": [],
}), FIXTURE_EVIDENCE)


def test_a_composite_name_in_the_related_section_is_never_created(
    monkeypatch, patched_fetch_markdown
):
    # The backstop. `_name_vetoes` catches nothing in the current production
    # sample that the location gate does not already catch, but a composite
    # reaching the RELATED section would otherwise create junk for free -- and
    # the model claiming is_named_entity=True does not make it a thing.
    case = dict(PRESS_ONLY_CASE, slug="case-composite-related", entities=[])
    api = _SearchStubApi([case], {"घरजग्गा सम्पत्ति - काठमाडौं": []})
    _run_main(monkeypatch, api,
              invoke_text_stub=lambda **kw: COMPOSITE_RELATED_RESPONSE,
              argv=["--apply", "--create-entities"])

    assert api.create_entity_calls == []
    row, = _created_rows()
    assert row["outcome"] == "skipped"
    assert "composite" in row["reason"]


def _named_entity_response(flag):
    """One related company, with `is_named_entity` set to whatever `flag` is.

    `flag is None` omits the key entirely -- the prompt-regression shape.
    """
    entity = {"entity_name": "सामुदायिक वन उपभोक्ता समूह",
              "relationship_type": "related",
              "entity_prefix": "organization", "entity_type": "Organization",
              "name_en": "Community Forest User Group",
              "notes": "रुख कटान गरिएको भनिएको समूह"}
    if flag is not None:
        entity["is_named_entity"] = flag
    return with_evidence(json.dumps({"entities": [entity], "accused_notes": []}),
                         FIXTURE_EVIDENCE)


def test_is_named_entity_false_blocks_creation(monkeypatch, patched_fetch_markdown):
    # "Community Forest User Group" is a category of body, not a named one.
    # `_name_vetoes` misses it: the generic rule needs EVERY word in its
    # 53-word list, and सामुदायिक and समूह are not in it. Only the model,
    # which read the passage, can tell.
    case = dict(PRESS_ONLY_CASE, slug="case-not-named", entities=[])
    api = _SearchStubApi([case], {"सामुदायिक वन उपभोक्ता समूह": []})
    _run_main(monkeypatch, api,
              invoke_text_stub=lambda **kw: _named_entity_response(False),
              argv=["--apply", "--create-entities"])

    assert api.create_entity_calls == []
    row, = _created_rows()
    assert row["outcome"] == "skipped"
    assert "named entity" in row["reason"]


def test_a_missing_is_named_entity_blocks_creation(
    monkeypatch, patched_fetch_markdown
):
    # FAIL CLOSED. A prompt regression that drops the field shows up as
    # `0 created` in the summary, which is visible and fixable. Defaulting the
    # other way fills NES with junk that cannot be deleted.
    case = dict(PRESS_ONLY_CASE, slug="case-flag-absent", entities=[])
    api = _SearchStubApi([case], {"सामुदायिक वन उपभोक्ता समूह": []})
    _run_main(monkeypatch, api,
              invoke_text_stub=lambda **kw: _named_entity_response(None),
              argv=["--apply", "--create-entities"])

    assert api.create_entity_calls == []
    row, = _created_rows()
    assert row["outcome"] == "skipped"


def test_is_named_entity_true_still_creates(monkeypatch, patched_fetch_markdown):
    case = dict(PRESS_ONLY_CASE, slug="case-named-true", entities=[])
    api = _SearchStubApi([case], {"सामुदायिक वन उपभोक्ता समूह": []})
    _run_main(monkeypatch, api,
              invoke_text_stub=lambda **kw: _named_entity_response(True),
              argv=["--apply", "--create-entities"])

    payload, = api.create_entity_calls
    assert payload["slug"] == "community-forest-user-group"


def test_a_gated_name_still_binds_when_the_resolver_matched_it(
    monkeypatch, patched_fetch_markdown
):
    # The create gates stop CREATION, never binding: a composite name
    # `_name_vetoes` would refuse to mint still reaches the case if the
    # resolver found it in NES. `location` used to be this test's example
    # gate, but a location item is refused outright before it can ever
    # resolve now (Task 5) -- the composite-name gate takes over as the
    # example of the same principle, a gate the create step still owns.
    org_iri = "https://jawafdehi.org/entity/organization/gharjagga-sampatti-kathmandu"
    case = dict(PRESS_ONLY_CASE, slug="case-gate-still-binds", entities=[])
    api = _SearchStubApi([case], {"घरजग्गा सम्पत्ति - काठमाडौं": [
        {"id": org_iri, "title": {"ne": "घरजग्गा सम्पत्ति - काठमाडौं"}, "score": 200.0},
    ]}, documents={org_iri: {"identifier": None}})
    _run_main(monkeypatch, api, invoke_text_stub=lambda **kw: COMPOSITE_RELATED_RESPONSE,
              argv=["--apply", "--create-entities"])

    assert api.create_entity_calls == []
    _slug, _path, items, _etag = api.replace_list_calls[0]
    assert [i["nes_id"] for i in items] == [org_iri]


# --------------------------------------------------------------------------
# The English name reaches the payload, not just the slug.
# --------------------------------------------------------------------------


def test_the_payload_carries_both_names_when_english_is_supplied(
    monkeypatch, patched_fetch_markdown
):
    # Canonical NES entities carry both ({"ne": "काठमाडौं", "en": "Kathmandu"}).
    # Ours carried `ne` only, so every entity we created was missing its
    # English name for the English UI and for search.
    case = dict(PRESS_ONLY_CASE, slug="case-payload-en", entities=[])
    api = _SearchStubApi([case], {"सामुदायिक वन उपभोक्ता समूह": []})
    _run_main(monkeypatch, api,
              invoke_text_stub=lambda **kw: _named_entity_response(True),
              argv=["--apply", "--create-entities"])

    payload, = api.create_entity_calls
    assert payload["name"] == {"ne": "सामुदायिक वन उपभोक्ता समूह",
                               "en": "Community Forest User Group"}


NO_ENGLISH_RESPONSE = with_evidence(json.dumps({
    "entities": [
        {"entity_name": "हेम राज बिष्ट", "relationship_type": "related",
         "entity_prefix": "person", "entity_type": "Person",
         "is_named_entity": True, "name_en": "",
         "notes": "तत्कालीन प्रमुख"},
    ],
    "accused_notes": [],
}), FIXTURE_EVIDENCE)


def test_the_payload_omits_the_english_name_rather_than_sending_it_blank(
    monkeypatch, patched_fetch_markdown
):
    case = dict(PRESS_ONLY_CASE, slug="case-payload-no-en", entities=[])
    api = _SearchStubApi([case], {"हेम राज बिष्ट": []})
    _run_main(monkeypatch, api, invoke_text_stub=lambda **kw: NO_ENGLISH_RESPONSE,
              argv=["--apply", "--create-entities"])

    payload, = api.create_entity_calls
    assert payload["name"] == {"ne": "हेम राज बिष्ट"}
    assert payload["slug"] == "hema-raja-bishta"


# --------------------------------------------------------------------------
# The prompt itself.
# --------------------------------------------------------------------------


def test_the_prompt_asks_for_both_new_fields():
    for prompt in (ere.COURT_ORDER_SYSTEM_PROMPT, ere.PRESS_RELEASE_SYSTEM_PROMPT):
        assert "is_named_entity" in prompt
        assert "name_en" in prompt


def test_the_prompt_rules_out_media_that_only_reported_the_case():
    # `नयाँ पत्रिका` was extracted as `related` for publishing the story. A
    # newspaper that reported a case is a source, not a participant.
    assert "नयाँ पत्रिका" in ere.COURT_ORDER_SYSTEM_PROMPT


def test_the_creation_block_explains_both_new_fields():
    section = ere.prefix_prompt_section(["person", "organization"])
    assert "is_named_entity" in section
    assert "name_en" in section


# --------------------------------------------------------------------------
# The LLM does not supply accused. The court record does.
#
# `GET /courtcases/<court>/<number>/entities` returns the defendants CIAA
# actually charged -- for 078-CR-0038, हेम राज विष्ट and रुबी जि.सी. विष्ट, the
# same two the extraction guessed at. `casework/court_record.py` already reads
# it and is deliberately unwired here (see the import comment at line 127).
#
# THE HARM THIS REMOVES: an accused bind carries `outcome = CHARGED`, and since
# 2026-08-05 one extracted name binds EVERY candidate above the threshold. An
# ambiguous accused name therefore recorded every namesake as charged in a
# corruption case -- `resolve`'s own docstring names 13 same-name entities for
# `संजय प्रसाद यादव`. Dropping the section removes the path entirely rather than
# narrowing it.
# --------------------------------------------------------------------------


ACCUSED_RESPONSE = with_evidence(json.dumps({
    "entities": [
        {"entity_name": "हेम राज बिष्ट", "relationship_type": "accused",
         "entity_prefix": "person", "entity_type": "Person",
         "is_named_entity": True, "name_en": "Hem Raj Bista",
         "notes": "प्रतिवादी"},
        {"entity_name": "नानी काजी थापा", "relationship_type": "alleged",
         "entity_prefix": "person", "entity_type": "Person",
         "is_named_entity": True, "name_en": "Nani Kaji Thapa",
         "notes": "घुस लेनदेनमा संलग्न भनी उल्लेख"},
    ],
    "accused_notes": [],
}), FIXTURE_EVIDENCE)


def test_an_extracted_accused_is_never_bound(monkeypatch, patched_fetch_markdown):
    case = dict(PRESS_ONLY_CASE, slug="case-accused-dropped", entities=[])
    api = _SearchStubApi([case], {
        "हेम राज बिष्ट": [{"id": "https://jawafdehi.org/entity/person/hem-raj-bista",
                            "title": {"ne": "हेम राज बिष्ट"}, "score": 180.0}],
        "नानी काजी थापा": [{"id": "https://jawafdehi.org/entity/person/nani-kaji-thapa",
                             "title": {"ne": "नानी काजी थापा"}, "score": 180.0}],
    })
    _run_main(monkeypatch, api, invoke_text_stub=lambda **kw: ACCUSED_RESPONSE,
              argv=["--apply"])

    _slug, _path, items, _etag = api.replace_list_calls[0]
    sections = {item["relationship_type"] for item in items}
    assert "accused" not in sections
    # The alleged name is untouched -- it is not in the court record, so the
    # extraction is the only source for it.
    assert sections == {"alleged"}


def test_an_extracted_accused_is_never_created(monkeypatch, patched_fetch_markdown):
    # Creation must not sneak an accused in through the other door.
    case = dict(PRESS_ONLY_CASE, slug="case-accused-nocreate", entities=[])
    api = _SearchStubApi([case], {"हेम राज बिष्ट": [], "नानी काजी थापा": []})
    _run_main(monkeypatch, api, invoke_text_stub=lambda **kw: ACCUSED_RESPONSE,
              argv=["--apply", "--create-entities"])

    posted = [p["slug"] for p in api.create_entity_calls]
    assert "hem-raj-bista" not in posted
    assert posted == ["nani-kaji-thapa"]


def test_a_dropped_accused_is_reported_not_silently_discarded(
    monkeypatch, patched_fetch_markdown
):
    case = dict(PRESS_ONLY_CASE, slug="case-accused-reported", entities=[])
    api = _SearchStubApi([case], {"हेम राज बिष्ट": [], "नानी काजी थापा": []})
    _run_main(monkeypatch, api, invoke_text_stub=lambda **kw: ACCUSED_RESPONSE,
              argv=["--dry-run"])

    rows = [json.loads(line) for line in
            Path(_report_files()["extracted"]).read_text(encoding="utf-8").splitlines()]
    dropped = [r for r in rows if r["extracted"] == "हेम राज बिष्ट"]
    assert dropped, "the accused name must still reach extracted.jsonl"
    assert dropped[0]["relationship_type"] == "accused"


def test_no_bind_this_module_writes_can_carry_a_charged_outcome(
    monkeypatch, patched_fetch_markdown
):
    # `outcome` is legal only on an accused bind (the `outcome_only_on_accused`
    # CHECK constraint). With accused gone, this module can never send one.
    case = dict(PRESS_ONLY_CASE, slug="case-no-outcome", entities=[])
    api = _SearchStubApi([case], {
        "हेम राज बिष्ट": [{"id": "https://jawafdehi.org/entity/person/hem-raj-bista",
                            "title": {"ne": "हेम राज बिष्ट"}, "score": 180.0}],
        "नानी काजी थापा": [{"id": "https://jawafdehi.org/entity/person/nani-kaji-thapa",
                             "title": {"ne": "नानी काजी थापा"}, "score": 180.0}],
    })
    _run_main(monkeypatch, api, invoke_text_stub=lambda **kw: ACCUSED_RESPONSE,
              argv=["--apply"])

    _slug, _path, items, _etag = api.replace_list_calls[0]
    assert all(not item.get("outcome") for item in items)


def test_the_prompt_no_longer_offers_accused_as_a_relationship_type():
    for prompt in (ere.COURT_ORDER_SYSTEM_PROMPT, ere.PRESS_RELEASE_SYSTEM_PROMPT):
        assert '"accused"' not in prompt
        assert "relationship_type" in prompt      # the others survive
    assert '"alleged"' in ere.COURT_ORDER_SYSTEM_PROMPT
    assert '"witness"' in ere.COURT_ORDER_SYSTEM_PROMPT


def test_the_prompt_says_where_defendants_actually_come_from():
    assert "court record" in ere.COURT_ORDER_SYSTEM_PROMPT


def test_the_carry_through_validator_still_accepts_an_existing_accused_bind():
    # THE TRAP THE SPLIT AVOIDS. `apply_entity_plan` validates every row of the
    # whole-list PATCH, including binds the case already had. A human's accused
    # bind -- or one the court-record path wrote -- must survive that, or the
    # case becomes unpatchable and we destroy the authoritative record.
    existing = {"nes_id": ANKUR_IRI, "relationship_type": "accused",
                "outcome": "charged", "notes": "मानव-लिखित"}
    assert ere.validate_bind_item(existing) == existing


def test_this_module_may_not_propose_an_accused_bind_of_its_own():
    proposed = {"nes_id": ANKUR_IRI, "relationship_type": "accused",
                "notes": "क"}
    with pytest.raises(ValueError, match="court record"):
        ere.validate_new_bind(proposed)


def test_the_new_bind_validator_still_applies_the_generic_rules():
    with pytest.raises(ValueError, match="canonical NES entity IRI"):
        ere.validate_new_bind({"nes_id": "not-an-iri",
                               "relationship_type": "related", "notes": ""})


def test_a_failed_prefix_read_costs_one_case_not_the_whole_run(
    monkeypatch, patched_fetch_markdown
):
    # Every other API call in the per-case loop is wrapped so one case's failure
    # does not cost the run. `entity_prefixes` was not, so a single 502 aborted
    # a 3,000-case batch -- and at the second call site, after entities had
    # already been created and cases already PATCHed.
    case = dict(PRESS_ONLY_CASE, slug="case-prefix-502", entities=[])
    api = _SearchStubApi([case], {"हेम राज बिष्ट": [], "वन निर्देशनालय, धनगढी": []})

    def boom(timeout=60):
        raise urllib.error.HTTPError("u", 502, "Bad Gateway", {}, None)
    api.entity_prefixes = boom

    _run_main(monkeypatch, api, invoke_text_stub=lambda **kw: CREATE_RESPONSE,
              argv=["--apply", "--create-entities"])

    # The run survived and created nothing, rather than raising out of main().
    assert api.create_entity_calls == []


def test_an_unreadable_prefix_list_does_not_blame_the_prefix(
    monkeypatch, patched_fetch_markdown
):
    # `read_live_prefixes` returns None rather than [] so a failed read cannot
    # be mistaken for "no prefix is in use" -- but `prefix_is_creatable` folds
    # both to an empty set, so every name came back refused for a reason that
    # was never checked. `person` is in use in production; telling a caseworker
    # its parent branch does not exist sends them to fix nothing.
    case = dict(PRESS_ONLY_CASE, slug="case-prefix-502-reason", entities=[])
    api = _SearchStubApi([case], {"हेम राज बिष्ट": [], "वन निर्देशनालय, धनगढी": []})

    def boom(timeout=60):
        raise urllib.error.HTTPError("u", 502, "Bad Gateway", {}, None)
    api.entity_prefixes = boom

    _run_main(monkeypatch, api, invoke_text_stub=lambda **kw: CREATE_RESPONSE,
              argv=["--apply", "--create-entities"])

    rows = _created_rows()
    assert rows, "the refused names must still reach created.jsonl"
    for row in rows:
        assert row["outcome"] == "skipped"
        assert "could not be read" in row["reason"]
        assert "parent branch does not exist" not in row["reason"]


def test_an_uncanonical_created_iri_costs_one_name_not_the_run(
    monkeypatch, patched_fetch_markdown
):
    # `_created_bind` validates, and validation RAISES. The per-name handler
    # only wrapped the POST, so a server answering with an off-authority `@id`
    # escaped both it and the call site, killing the run after entities had
    # already been created.
    case = dict(PRESS_ONLY_CASE, slug="case-bad-iri", entities=[])
    api = _SearchStubApi([case], {"हेम राज बिष्ट": [], "वन निर्देशनालय, धनगढी": []})
    real_create = api.create_entity

    def odd_iri(payload, timeout=60):
        real_create(payload, timeout)
        return {"@id": "https://elsewhere.example/entity/person/x"}
    api.create_entity = odd_iri

    _run_main(monkeypatch, api, invoke_text_stub=lambda **kw: CREATE_RESPONSE,
              argv=["--apply", "--create-entities"])

    rows = _created_rows()
    assert [r["outcome"] for r in rows] == ["error", "error"]
    assert all("canonical NES entity IRI" in r["reason"] for r in rows)


class TestApplyAccusedUpdates:
    IRI = "https://jawafdehi.org/entity/person/ram-bahadur-1"

    def _bind(self, notes="", outcome="charged", rel="accused"):
        return {"nes_id": self.IRI, "relationship_type": rel,
                "notes": notes, "outcome": outcome}

    def test_placeholder_note_is_replaced(self):
        binds = [self._bind(notes="प्रतिवादी — विशेष अदालत मुद्दा 079-cr-0071")]
        got = ere.apply_accused_updates(
            binds, {self.IRI: {"outcome": "convicted", "notes": "तत्कालीन सचिव — घूस लिने"}})
        assert got[0]["notes"] == "तत्कालीन सचिव — घूस लिने"
        assert got[0]["outcome"] == "convicted"

    def test_a_humans_note_is_never_overwritten(self):
        binds = [self._bind(notes="तत्कालीन प्रमुख नापी अधिकृत — मुख्य प्रतिवादी")]
        got = ere.apply_accused_updates(
            binds, {self.IRI: {"outcome": "convicted", "notes": "मोडेलको पाठ"}})
        assert got[0]["notes"] == "तत्कालीन प्रमुख नापी अधिकृत — मुख्य प्रतिवादी"
        # The verdict still lands. Refusing the note must not refuse the outcome.
        assert got[0]["outcome"] == "convicted"

    def test_an_empty_note_is_filled(self):
        binds = [self._bind(notes="")]
        got = ere.apply_accused_updates(
            binds, {self.IRI: {"outcome": "acquitted", "notes": "सहायक"}})
        assert got[0]["notes"] == "सहायक"

    def test_the_alias_tail_of_a_placeholder_survives(self):
        # enrich_court_record puts the court's own alias on the bind. It is the
        # only place the record says the court called this person something
        # else; replacing the whole note drops it on the floor.
        note = ("प्रतिवादी — विशेष अदालत मुद्दा 079-cr-0071"
                "; अदालतको अभिलेखमा: रामे भन्ने राम बहादुर")
        got = ere.apply_accused_updates(
            [self._bind(notes=note)],
            {self.IRI: {"outcome": "convicted", "notes": "तत्कालीन सचिव"}})
        assert got[0]["notes"].startswith("तत्कालीन सचिव")
        assert "; अदालतको अभिलेखमा: रामे भन्ने राम बहादुर" in got[0]["notes"]

    def test_an_empty_role_never_blanks_a_placeholder(self):
        # A judgment can convict a defendant it never describes. An empty
        # `notes` means "no role known", not "erase the placeholder".
        binds = [self._bind(notes="प्रतिवादी — विशेष अदालत मुद्दा 079-cr-0071")]
        got = ere.apply_accused_updates(
            binds, {self.IRI: {"outcome": "convicted", "notes": ""}})
        assert got[0]["notes"] == "प्रतिवादी — विशेष अदालत मुद्दा 079-cr-0071"
        assert got[0]["outcome"] == "convicted"

    def test_an_empty_role_never_blanks_a_placeholders_alias_tail(self):
        note = ("प्रतिवादी — विशेष अदालत मुद्दा 079-cr-0071"
                "; अदालतको अभिलेखमा: रामे भन्ने राम बहादुर")
        got = ere.apply_accused_updates(
            [self._bind(notes=note)],
            {self.IRI: {"outcome": "convicted", "notes": ""}})
        assert got[0]["notes"] == note

    def test_an_empty_note_and_an_empty_role_stay_empty(self):
        got = ere.apply_accused_updates(
            [self._bind(notes="")],
            {self.IRI: {"outcome": "convicted", "notes": ""}})
        assert got[0]["notes"] == ""

    def test_a_bind_with_no_update_is_copied_through_unchanged(self):
        other = {"nes_id": "https://jawafdehi.org/entity/person/other-9",
                 "relationship_type": "accused", "notes": "मानव लेख", "outcome": "acquitted"}
        got = ere.apply_accused_updates([self._bind(), other], {})
        assert got[1] == other

    def test_a_non_accused_bind_is_never_touched(self):
        rel = {"nes_id": self.IRI, "relationship_type": "related", "notes": "स्थान"}
        got = ere.apply_accused_updates(
            [rel], {self.IRI: {"outcome": "convicted", "notes": "x"}})
        assert got[0] == rel

    def test_unknown_is_never_written(self):
        binds = [self._bind(outcome="charged")]
        got = ere.apply_accused_updates(
            binds, {self.IRI: {"outcome": "unknown", "notes": "भूमिका"}})
        assert got[0]["outcome"] == "charged"
        # The role note is still worth having even when the verdict is not.
        assert got[0]["notes"] == "भूमिका"

    def test_order_and_length_are_preserved(self):
        binds = [self._bind(), {"nes_id": "https://jawafdehi.org/entity/person/b-2",
                                "relationship_type": "accused", "notes": "", "outcome": "charged"}]
        got = ere.apply_accused_updates(binds, {})
        assert [b["nes_id"] for b in got] == [b["nes_id"] for b in binds]

    def test_every_row_it_returns_passes_the_serializer_mirror(self):
        binds = [self._bind(notes="प्रतिवादी — विशेष अदालत मुद्दा 079-cr-0071")]
        got = ere.apply_accused_updates(
            binds, {self.IRI: {"outcome": "convicted", "notes": "सचिव"}})
        for item in got:
            ere.validate_bind_item(item)


class TestParseVerdictResponse:
    def test_extracts_the_defendants_array(self):
        got = ere.parse_verdict_response(
            'text before {"defendants":[{"name":"क","outcome":"convicted",'
            '"role":"सचिव","evidence":"ठहर्छ"}]} after')
        assert got[0]["name"] == "क"

    def test_a_row_with_no_name_is_dropped(self):
        got = ere.parse_verdict_response('{"defendants":[{"outcome":"convicted"}]}')
        assert got == []

    def test_an_unknown_outcome_value_is_dropped_not_coerced(self):
        got = ere.parse_verdict_response(
            '{"defendants":[{"name":"क","outcome":"दोषी","role":"","evidence":""}]}')
        assert got == []

    def test_unparseable_text_returns_empty(self):
        assert ere.parse_verdict_response("the model apologised") == []

    def test_role_is_capped_at_90_chars(self):
        got = ere.parse_verdict_response(
            '{"defendants":[{"name":"क","outcome":"charged","role":"' + "अ" * 300
            + '","evidence":""}]}')
        assert len(got[0]["role"]) <= 90


class TestVerdictPromptBounds:
    """Every field the reply carries is bounded. `role` was capped at 90 chars
    from the start; `evidence` -- quoted VERBATIM, and the one field that can
    run to a paragraph -- was not, and 20 unbounded quotes overrun
    VERDICT_MAX_TOKENS. A reply cut off there parses as nothing at all.
    """

    def test_the_prompt_bounds_the_evidence_quote(self):
        assert "under 200 characters" in ere.VERDICT_SYSTEM_PROMPT
        assert "one sentence" in ere.VERDICT_SYSTEM_PROMPT

    def test_the_quote_is_still_required_to_be_verbatim(self):
        # Bounded, not paraphrased: the evidence phrase is the only way a
        # wrong `convicted` is findable in the artefact.
        assert "VERBATIM" in ere.VERDICT_SYSTEM_PROMPT


class TestAccusedVerdicts:
    def _reply(self, names):
        import json
        return json.dumps({"defendants": [
            {"name": n, "outcome": "convicted", "role": "सचिव", "evidence": "ठहर्छ"}
            for n in names]}, ensure_ascii=False)

    def test_one_chunk_for_a_small_case(self):
        calls = []

        def fake(system, content, max_tokens, tier, usage=None):
            calls.append(content)
            return self._reply(["क", "ख"])

        got, errors = ere.accused_verdicts(["क", "ख"], "आदेश", fake)
        assert len(calls) == 1
        assert got["क"]["outcome"] == "convicted"
        assert errors == []

    def test_a_long_accused_list_is_chunked(self):
        # Measured: ~300 output tokens per defendant in Devanagari, so 8,000
        # buys about 25. Production holds cases with 185 and 249 accused binds.
        names = [f"नाम{i}" for i in range(45)]
        seen = []

        def fake(system, content, max_tokens, tier, usage=None):
            batch = [n for n in names if f"- {n}\n" in content + "\n"]
            seen.append(len(batch))
            return self._reply(batch)

        got, errors = ere.accused_verdicts(names, "आदेश", fake)
        assert len(seen) == 3          # 20 + 20 + 5
        assert max(seen) <= ere.VERDICT_CHUNK
        assert len(got) == 45

    def test_a_short_chunk_is_an_error_not_a_partial(self):
        # The probe's apparent "0 of 9 correct" was really nine rows silently
        # missing. A run that reports that as a clean result is the one failure
        # mode this must never ship with.
        def fake(system, content, max_tokens, tier, usage=None):
            return self._reply(["नाम0"])

        got, errors = ere.accused_verdicts([f"नाम{i}" for i in range(5)], "आदेश", fake)
        assert errors and "1 of 5" in errors[0]

    def test_a_failed_chunk_does_not_lose_the_others(self):
        names = [f"नाम{i}" for i in range(25)]
        calls = {"n": 0}

        def fake(system, content, max_tokens, tier, usage=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("provider 502")
            return self._reply([n for n in names if f"- {n}\n" in content + "\n"])

        got, errors = ere.accused_verdicts(names, "आदेश", fake)
        assert errors
        assert len(got) == 5           # the second chunk survived

    def test_a_short_chunk_is_retried_and_the_retry_fills_the_gap(self):
        # A chunk of 20 that returns 1 defendant must not lose the other 19:
        # retry once, asking only about the names still missing.
        names = [f"नाम{i}" for i in range(5)]
        calls = []

        def fake(system, content, max_tokens, tier, usage=None):
            calls.append(content)
            if len(calls) == 1:
                return self._reply(["नाम0"])
            batch = [n for n in names if f"- {n}\n" in content + "\n"]
            return self._reply(batch)

        got, errors = ere.accused_verdicts(names, "आदेश", fake)
        assert len(calls) == 2
        assert len(got) == 5
        assert errors == []

    def test_a_chunk_still_short_after_retry_keeps_partial_results_and_errors(self):
        # If the retry also comes back short, keep what was answered, error
        # on the rest, and never retry a second time.
        names = [f"नाम{i}" for i in range(5)]
        calls = {"n": 0}

        def fake(system, content, max_tokens, tier, usage=None):
            calls["n"] += 1
            return self._reply(["नाम0"])

        got, errors = ere.accused_verdicts(names, "आदेश", fake)
        assert calls["n"] == 2          # exactly one retry, never a second
        assert got["नाम0"]["outcome"] == "convicted"
        assert len(got) == 1
        assert errors

    def _sizes_stub(self, names, truncate_over):
        """A stub that truncates its reply whenever more than `truncate_over`
        names are asked for, and records the size of every request."""
        sizes = []

        def fake(system, content, max_tokens, tier, usage=None):
            batch = [n for n in names if f"- {n}\n" in content + "\n"]
            sizes.append(len(batch))
            reply = self._reply(batch)
            # A reply cut off at VERDICT_MAX_TOKENS: the JSON never closes, so
            # `parse_extraction_response` returns None and the WHOLE chunk is
            # lost -- not just the tail.
            return reply[:60] if len(batch) > truncate_over else reply

        return fake, sizes

    def test_a_truncated_reply_is_recovered_by_halving_the_chunk(self):
        # The likeliest cause of a short chunk is a reply truncated at
        # VERDICT_MAX_TOKENS, and a retry of the SAME size just reproduces it.
        # The retry re-enters the chunk loop at VERDICT_CHUNK // 2 instead.
        names = [f"नाम{i}" for i in range(ere.VERDICT_CHUNK)]
        fake, sizes = self._sizes_stub(names, ere.VERDICT_CHUNK // 2)

        got, errors = ere.accused_verdicts(names, "आदेश", fake)
        assert sizes == [ere.VERDICT_CHUNK,
                         ere.VERDICT_CHUNK // 2, ere.VERDICT_CHUNK // 2]
        assert len(got) == ere.VERDICT_CHUNK
        assert errors == []

    def test_a_chunk_short_after_the_halved_retry_is_never_retried_again(self):
        # One retry PASS, not a recursion: the halves are asked once each, and
        # every name still missing is an error carrying its own name.
        names = [f"नाम{i}" for i in range(ere.VERDICT_CHUNK)]
        fake, sizes = self._sizes_stub(names, 0)

        got, errors = ere.accused_verdicts(names, "आदेश", fake)
        assert sizes == [ere.VERDICT_CHUNK,
                         ere.VERDICT_CHUNK // 2, ere.VERDICT_CHUNK // 2]
        assert got == {}
        assert errors and f"0 of {ere.VERDICT_CHUNK}" in errors[0]
        assert all(name in errors[0] for name in names)

    def test_a_complete_first_reply_triggers_no_retry(self):
        names = ["क", "ख"]
        calls = []

        def fake(system, content, max_tokens, tier, usage=None):
            calls.append(content)
            return self._reply(names)

        got, errors = ere.accused_verdicts(names, "आदेश", fake)
        assert len(calls) == 1
        assert len(got) == 2
        assert errors == []

    def test_an_unrequested_name_in_the_reply_is_dropped_and_flagged(self):
        # A hallucinated extra row must not slip into `results` unflagged.
        def fake(system, content, max_tokens, tier, usage=None):
            return self._reply(["क", "ख", "अनाधिकृत नाम"])

        got, errors = ere.accused_verdicts(["क", "ख"], "आदेश", fake)
        assert "अनाधिकृत नाम" not in got
        assert got["क"]["outcome"] == "convicted"
        assert got["ख"]["outcome"] == "convicted"
        assert any("अनाधिकृत नाम" in e for e in errors)

    def test_a_swapped_name_leaves_the_request_missing_and_errors(self):
        # Same row count as requested, but the name is fabricated. The
        # requested defendant must not vanish with errors == [] -- that is
        # the exact silent-drop failure the chunking design exists to catch.
        def fake(system, content, max_tokens, tier, usage=None):
            return self._reply(["नक्कली नाम"])

        got, errors = ere.accused_verdicts(["क"], "आदेश", fake)
        assert "क" not in got
        assert errors

    def test_a_reply_omitting_one_requested_name_still_returns_the_others(self):
        def fake(system, content, max_tokens, tier, usage=None):
            return self._reply(["क"])  # "ख" was requested but never answered

        got, errors = ere.accused_verdicts(["क", "ख"], "आदेश", fake)
        assert got["क"]["outcome"] == "convicted"
        assert "ख" not in got
        assert any("ख" in e for e in errors)

    def test_an_unusable_reply_errors_for_every_name_in_the_chunk(self):
        def fake(system, content, max_tokens, tier, usage=None):
            return "the model apologised"

        got, errors = ere.accused_verdicts(["क", "ख", "ग"], "आदेश", fake)
        assert got == {}
        assert errors
        assert all(name in errors[0] for name in ("क", "ख", "ग"))

    def test_the_model_sees_the_verdict_zone_not_the_plain_tail(self):
        # Replaces the original brief's plain-tail assertion (Ruling 14): the
        # verdict call now reads `court_order_verdict_zone`, a union of the
        # marker-anchored window and the order's last chars, not a single
        # fixed-distance-from-the-end slice. Build an order whose marker
        # region carries a distinctive name and whose ending carries the
        # pronouncement, and require both to reach the prompt.
        seen = {}

        def fake(system, content, max_tokens, tier, usage=None):
            seen["content"] = content
            return self._reply(["क"])

        order = (
            "सुरुको भाग" + "ख" * 200_000
            + "ठहर खण्ड" + "राम बहादुर कारागार सजाय" + "ग" * 50_000
            + "सफाई पाउने ठहर्छ"
        )
        ere.accused_verdicts(["क"], order, fake)
        assert "राम बहादुर कारागार सजाय" in seen["content"]
        assert "सफाई पाउने ठहर्छ" in seen["content"]
        assert "सुरुको भाग" not in seen["content"]

    def test_the_accused_list_is_put_in_the_prompt(self):
        seen = {}

        def fake(system, content, max_tokens, tier, usage=None):
            seen["content"] = content
            return self._reply(["राम बहादुर"])

        ere.accused_verdicts(["राम बहादुर"], "आदेश", fake)
        assert "राम बहादुर" in seen["content"]


class TestTheServerPreservesOmittedOutcomes:
    """`apply_accused_updates` sends `outcome` only on the rows it decided.

    Every other accused row goes out with no `outcome` key, and
    `cases.api_views.CaseViewSet._rewrite_entity_binds` preserves that bind's prior
    verdict across the whole-list delete/recreate. If that ever stops being true,
    this enricher silently resets verdicts to 'charged' -- so pin it here.
    """

    def test_the_replace_handler_still_preserves_an_omitted_outcome(self):
        import inspect

        from cases.api_views import CaseViewSet

        source = inspect.getsource(CaseViewSet._rewrite_entity_binds)
        assert '"outcome" in item' in source
        assert "prior_outcomes" in source


# --------------------------------------------------------------------------
# Task 7 -- the verdict step wired into main().
#
# ROUTING NOTE. The brief's `_two_call_stub` routed on
# `VERDICT_SYSTEM_PROMPT[:40]`, but both system prompts open with the same 41
# characters ("You are a Nepali legal research assistant"), so a 40-char prefix
# matches the EXTRACTION call too and every call would land in `verdict_calls`.
# `accused_verdicts` passes the constant verbatim, so exact equality is both
# correct and provable.
# --------------------------------------------------------------------------

ACCUSED_IRI = "https://jawafdehi.org/entity/person/ram-bahadur-1"
SECOND_ACCUSED_IRI = "https://jawafdehi.org/entity/person/ram-bahadur-2"
SAJHA_IRI = ("https://jawafdehi.org/entity/organization/"
             "sajha-bhandara-sahakari-9f9f9f")


def _two_call_stub(entity_response=None, verdict_response=None):
    """Route by prompt: this stage now makes an extraction call and a verdict
    call, and a test that cannot tell them apart cannot prove the verdict gate
    fired. Recording rather than raising, for the reason `_call_tracking_stub`
    documents: a raise from a call that legitimately happens is swallowed by
    the per-case `except Exception` and counted as an `error` status instead of
    failing the test loudly.
    """
    if entity_response is None:
        entity_response = json.dumps({"entities": [], "accused_notes": []})
    if verdict_response is None:
        verdict_response = json.dumps({"defendants": []})
    entity_calls, verdict_calls = [], []

    def stub(**kw):
        if kw.get("system") == ere.VERDICT_SYSTEM_PROMPT:
            verdict_calls.append(kw)
            return verdict_response
        entity_calls.append(kw)
        return entity_response

    stub.entity_calls = entity_calls
    stub.verdict_calls = verdict_calls
    return stub


def _accused_case(slug="case-verdict", outcome="charged", court=True,
                  notes="प्रतिवादी — विशेष अदालत मुद्दा 079-cr-0071",
                  display_name="राम बहादुर", extra_entities=()):
    evidence = [
        {"material_iri": "https://jawafdehi.org/material/ciaa/press_releases/9",
         "material": {"material_type": "press_release", "urls": [
             {"link": "https://x/press.md", "role": "MARKDOWN"}]}},
    ]
    if court:
        evidence.append(
            {"material_iri": "https://jawafdehi.org/material/ngm/court_orders/9",
             "material": {"material_type": "court_order", "urls": [
                 {"link": "https://x/court.md", "role": "MARKDOWN"}]}})
    return {
        "slug": slug, "title": "फैसला भएको मुद्दा", "state": "DRAFT",
        "evidence": evidence,
        "entities": [
            {"nes_id": ACCUSED_IRI, "type": "accused", "display_name": display_name,
             "outcome": outcome, "notes": notes},
            *extra_entities,
        ],
    }


VERDICT_RESPONSE = json.dumps({"defendants": [
    {"name": "राम बहादुर", "outcome": "convicted",
     "role": "तत्कालीन सचिव, अर्थ मन्त्रालय — घूस लिने",
     "evidence": "निज प्रतिवादीले कसुर गरेको ठहर्छ"},
]}, ensure_ascii=False)


def _read_report(tmp_path, suffix):
    """The one report file this run wrote whose name ends in `suffix`."""
    matches = [p for p in Path(tmp_path).iterdir()
               if p.is_file() and p.name.endswith(suffix)]
    assert len(matches) == 1, f"expected one *{suffix}, found {matches}"
    return matches[0].read_text(encoding="utf-8")


def _verdict_rows(tmp_path):
    return [json.loads(line)
            for line in _read_report(tmp_path, "verdicts.jsonl").splitlines()
            if line.strip()]


class TestVerdictGate:
    # `_StubApi` is correct HERE: these assert that a call was NOT made, which
    # needs no ETag and no write. See TestVerdictWrite for why a write test
    # cannot use it.

    def test_a_case_with_no_court_order_makes_no_verdict_call(
            self, monkeypatch, patched_fetch_markdown):
        # The gate is the court order's presence: an order is bound to a case
        # after it is decided (31 of 31 in the sample carried verdict language),
        # so this costs no extra request and undecided cases spend nothing.
        api = _StubApi([_accused_case(court=False)])
        stub = _two_call_stub()
        _run_main(monkeypatch, api, invoke_text_stub=stub, argv=["--dry-run", "--verdicts"])
        assert stub.verdict_calls == []

    def test_a_case_whose_accused_ALL_carry_a_terminal_outcome_is_skipped(
            self, monkeypatch, patched_fetch_markdown):
        # 486 production cases read all-`acquitted` from bind_outcome's सफाई
        # rule -- whole-case acquittals, not re-litigated here.
        api = _StubApi([_accused_case(outcome="acquitted")])
        stub = _two_call_stub()
        _run_main(monkeypatch, api, invoke_text_stub=stub, argv=["--dry-run", "--verdicts"])
        assert stub.verdict_calls == []

    def test_the_spend_gate_and_the_planner_read_the_state_the_same_way(self):
        # The spend gate used to upper-case the state while the planner
        # compared it exactly, so a payload carrying `draft` passed the gate,
        # bought a premium verdict call per chunk, and was then refused at the
        # write -- the one thing the state clause exists to prevent.
        case = dict(_accused_case(), state="draft")
        assert ere.verdict_state_refusal(case), "the spend gate let `draft` through"
        plan = ere.plan_case_entities(None, case, 'W/"abc123"', [])
        assert plan.action == "SKIP_STATE"

    def test_an_in_review_case_never_pays_for_a_verdict_call(
            self, monkeypatch, patched_fetch_markdown):
        # `select.ENRICHABLE_STATES` admits IN_REVIEW, but the write requires
        # DRAFT. Without this clause every IN_REVIEW case in the population
        # buys a premium verdict prompt per chunk -- up to ~103k chars of zone
        # each time -- and is then refused at the write gate.
        case = dict(_accused_case(slug="case-in-review-verdict"), state="IN_REVIEW")
        api = _StubApi([case])
        stub = _two_call_stub(verdict_response=VERDICT_RESPONSE)
        _run_main(monkeypatch, api, invoke_text_stub=stub, argv=["--dry-run", "--verdicts"])
        assert stub.verdict_calls == []

    def test_the_state_skip_is_a_row_in_the_verdicts_file(
            self, monkeypatch, patched_fetch_markdown, tmp_path):
        # The skip must be VISIBLE. Its binds are decidable in every other
        # respect, so without a row the case is simply absent from the
        # artefact and nothing says its defendants were passed over.
        case = dict(_accused_case(slug="case-in-review-verdict"), state="IN_REVIEW")
        api = _StubApi([case])
        stub = _two_call_stub(verdict_response=VERDICT_RESPONSE)
        _run_main(monkeypatch, api, invoke_text_stub=stub, argv=["--dry-run", "--verdicts"])
        row = next(r for r in _verdict_rows(tmp_path) if r["nes_id"] == ACCUSED_IRI)
        assert row["written"] is False
        assert "IN_REVIEW" in row["reason"] and "DRAFT" in row["reason"]
        # Not the write refusal the planner records further down: this row says
        # the judgment was never read at all, which is the spend being saved.
        assert "judgment was not read" in row["reason"]
        assert stub.verdict_calls == []

    # THE TWO TESTS ABOVE GATE A SPEND, NOT THE WRITE. The planner's own
    # non-DRAFT refusal is what stops a notes-redacted IN_REVIEW read from
    # reaching the destructive whole-list replace, and it stays where it is --
    # `test_plan_refuses_a_non_draft_case` pins it.

    def test_a_charged_case_with_an_order_is_processed(
            self, monkeypatch, patched_fetch_markdown):
        api = _StubApi([_accused_case()])
        stub = _two_call_stub(verdict_response=VERDICT_RESPONSE)
        _run_main(monkeypatch, api, invoke_text_stub=stub, argv=["--dry-run", "--verdicts"])
        assert len(stub.verdict_calls) == 1

    def test_the_verdict_read_is_off_unless_asked_for(
            self, monkeypatch, patched_fetch_markdown):
        # OPT-IN. `convicted` on a real person is the worst thing this module
        # can get wrong, so a run that only wants entity binds must not write
        # one. Same case as the test above, minus the flag.
        api = _StubApi([_accused_case()])
        stub = _two_call_stub(verdict_response=VERDICT_RESPONSE)
        _run_main(monkeypatch, api, invoke_text_stub=stub,
                  argv=["--dry-run"])
        assert stub.verdict_calls == []

    def test_a_case_already_carrying_related_binds_still_gets_its_verdicts(
            self, monkeypatch, patched_fetch_markdown):
        # THE POINT OF A SEPARATE GATE. main() skips extraction for a case that
        # already has a `related` bind and a district, and nearly every case
        # this feature targets is in exactly that state. Sharing that gate would
        # skip all of them.
        case = _accused_case(extra_entities=[
            {"nes_id": "https://jawafdehi.org/entity/organization/o-1",
             "type": "related", "display_name": "संस्था", "notes": ""},
            ALREADY_BOUND_DISTRICT])
        api = _StubApi([case])
        stub = _two_call_stub(verdict_response=VERDICT_RESPONSE)
        _run_main(monkeypatch, api, invoke_text_stub=stub, argv=["--dry-run", "--verdicts"])
        assert stub.entity_calls == []
        assert len(stub.verdict_calls) == 1

    def test_without_verdicts_the_already_enriched_skip_stays_free(
            self, monkeypatch, patched_fetch_markdown):
        # The other half of the default: without --verdicts, an already-enriched
        # case must still cost nothing at all, not merely no LLM call.
        case = _accused_case(extra_entities=[
            {"nes_id": "https://jawafdehi.org/entity/organization/o-1",
             "type": "related", "display_name": "संस्था", "notes": ""},
            ALREADY_BOUND_DISTRICT])
        api = _StubApi([case])
        stub = _two_call_stub(verdict_response=VERDICT_RESPONSE)
        _run_main(monkeypatch, api, invoke_text_stub=stub,
                  argv=["--dry-run"])
        assert stub.entity_calls == [] and stub.verdict_calls == []


RELATED_BIND = {"nes_id": "https://jawafdehi.org/entity/organization/o-1",
                "type": "related", "display_name": "संस्था", "notes": ""}


def _sita(outcome):
    return {"nes_id": SECOND_ACCUSED_IRI, "type": "accused",
            "display_name": "सीता देवी", "outcome": outcome,
            "notes": "प्रतिवादी — विशेष अदालत मुद्दा 079-cr-0071"}


class TestSettledOutcomesAreSkippedPerBind:
    """A judgment that decides some defendants and not others is the NORMAL
    case -- 8 abstentions in 83 measured defendants. Refusing the whole case
    on any terminal outcome wrote the answered ones and then locked the rest
    at `charged` forever. The filter is per-BIND: a settled bind is never
    re-litigated, and the case stays finishable.
    """

    def test_a_partly_settled_case_is_still_read(
            self, monkeypatch, patched_fetch_markdown):
        api = _SearchStubApi([_accused_case(extra_entities=[_sita("convicted")])])
        stub = _two_call_stub(verdict_response=VERDICT_RESPONSE)
        _run_main(monkeypatch, api, invoke_text_stub=stub,
                  argv=["--apply", "--verdicts"])
        assert len(stub.verdict_calls) == 1
        prompt = stub.verdict_calls[0]["content"]
        assert "राम बहादुर" in prompt
        assert "सीता देवी" not in prompt

    def test_the_settled_bind_is_reported_with_its_reason(
            self, monkeypatch, patched_fetch_markdown, tmp_path):
        api = _SearchStubApi([_accused_case(extra_entities=[_sita("convicted")])])
        stub = _two_call_stub(verdict_response=VERDICT_RESPONSE)
        _run_main(monkeypatch, api, invoke_text_stub=stub,
                  argv=["--apply", "--verdicts"])
        row = next(r for r in _verdict_rows(tmp_path)
                   if r["nes_id"] == SECOND_ACCUSED_IRI)
        assert row["written"] is False
        assert row["old_outcome"] == "convicted"
        assert "terminal outcome" in row["reason"]

    def test_the_settled_binds_stored_outcome_is_not_rewritten(
            self, monkeypatch, patched_fetch_markdown):
        # An omitted `outcome` is what makes the server preserve the stored
        # one across the whole-list replace. The settled bind must go out
        # without one, and must not be dropped from the list either.
        api = _SearchStubApi([_accused_case(extra_entities=[_sita("convicted")])])
        stub = _two_call_stub(verdict_response=VERDICT_RESPONSE)
        _run_main(monkeypatch, api, invoke_text_stub=stub,
                  argv=["--apply", "--verdicts"])
        _slug, _path, items, _if_match = api.replace_list_calls[0]
        by_id = {i["nes_id"]: i for i in items}
        assert "outcome" not in by_id[SECOND_ACCUSED_IRI]
        assert by_id[ACCUSED_IRI]["outcome"] == "convicted"

    def test_a_re_run_finishes_a_half_decided_case(
            self, monkeypatch, patched_fetch_markdown):
        # THE GUARANTEE B1 RESTORES. This is the case exactly as a first run
        # that answered for राम बहादुर only would have left it; the second run
        # must be able to decide सीता देवी rather than find the case locked.
        api = _SearchStubApi([_accused_case(outcome="convicted",
                                            extra_entities=[_sita("charged")])])
        stub = _two_call_stub(verdict_response=json.dumps({"defendants": [
            {"name": "सीता देवी", "outcome": "acquitted", "role": "तत्कालीन लेखापाल",
             "evidence": "सफाई पाउने ठहर्छ"}]}, ensure_ascii=False))
        _run_main(monkeypatch, api, invoke_text_stub=stub,
                  argv=["--apply", "--verdicts"])
        assert api.replace_list_calls, "the re-run wrote nothing"
        _slug, _path, items, _if_match = api.replace_list_calls[0]
        by_id = {i["nes_id"]: i for i in items}
        assert by_id[SECOND_ACCUSED_IRI]["outcome"] == "acquitted"
        assert "outcome" not in by_id[ACCUSED_IRI]


class TestTheGateSpendsNothingItCannotUse:
    def test_a_refused_case_pays_for_no_document_fetch(
            self, monkeypatch, patched_fetch_markdown):
        # The clauses answerable from the case payload -- state, an accused
        # bind, an already-settled list -- run BEFORE the markdown fetch. The
        # API client has no 5xx retry, so a request that can only be discarded
        # is a request worth not making.
        import casework.common.materials as m
        fetched = []

        def counting(link, timeout=60):
            fetched.append(link)
            return "अदालतको आदेशमा ठहर खण्ड उल्लेख छ।"

        monkeypatch.setattr(m, "fetch_markdown", counting)
        api = _StubApi([_accused_case(outcome="acquitted",
                                      extra_entities=[RELATED_BIND, ALREADY_BOUND_DISTRICT])])
        stub = _two_call_stub(verdict_response=VERDICT_RESPONSE)
        _run_main(monkeypatch, api, invoke_text_stub=stub,
                  argv=["--dry-run", "--verdicts"])
        assert stub.verdict_calls == []
        assert fetched == []

    def test_the_state_skip_is_still_a_row_when_no_document_was_fetched(
            self, monkeypatch, patched_fetch_markdown, tmp_path):
        # Saving the fetch must not cost the artefact row: an IN_REVIEW case
        # that is ALSO skipped for extraction reaches the gate with no court
        # text at all, and the row is keyed on the order being BOUND.
        import casework.common.materials as m
        fetched = []

        def counting(link, timeout=60):
            fetched.append(link)
            return "अदालतको आदेशमा ठहर खण्ड उल्लेख छ।"

        monkeypatch.setattr(m, "fetch_markdown", counting)
        case = dict(_accused_case(slug="case-in-review-skipped",
                                  extra_entities=[RELATED_BIND, ALREADY_BOUND_DISTRICT]),
                    state="IN_REVIEW")
        api = _StubApi([case])
        stub = _two_call_stub(verdict_response=VERDICT_RESPONSE)
        _run_main(monkeypatch, api, invoke_text_stub=stub,
                  argv=["--dry-run", "--verdicts"])
        assert fetched == []
        row = next(r for r in _verdict_rows(tmp_path) if r["nes_id"] == ACCUSED_IRI)
        assert "judgment was not read" in row["reason"]


class TestAHumanWhoSettlesABindMidRunWins:
    """The gate reads `detail`; the write list is built from the later `fresh`
    read, and `If-Match` cannot catch a human who settled a bind in between --
    the ETag comes from that same later read.
    """

    class _RacedApi(_SearchStubApi):
        def get_case_with_etag(self, slug, timeout=60):
            case = dict(self._cases[slug])
            case["entities"] = [
                dict(bind, outcome="acquitted")
                if bind["nes_id"] == ACCUSED_IRI else bind
                for bind in case["entities"]]
            return case, self.etag

    def test_the_verdict_is_dropped_rather_than_written_over_the_human(
            self, monkeypatch, patched_fetch_markdown, tmp_path):
        api = self._RacedApi([_accused_case()])
        stub = _two_call_stub(verdict_response=VERDICT_RESPONSE)  # says convicted
        _run_main(monkeypatch, api, invoke_text_stub=stub,
                  argv=["--apply", "--verdicts"])
        assert api.replace_list_calls == []
        row = next(r for r in _verdict_rows(tmp_path) if r["nes_id"] == ACCUSED_IRI)
        assert row["written"] is False
        assert "between" in row["reason"]


class TestEveryRowIsAccountedFor:
    def test_a_refused_write_leaves_its_rows_in_the_undecided_count(
            self, monkeypatch, patched_fetch_markdown, capsys):
        # `_StubApi` has no `get_case_with_etag`, so the write gate refuses.
        # The row was computed and never written: counted in neither total,
        # the epilogue's two numbers stop accounting for every row in the file.
        api = _StubApi([_accused_case()])
        stub = _two_call_stub(verdict_response=VERDICT_RESPONSE)
        _run_main(monkeypatch, api, invoke_text_stub=stub,
                  argv=["--dry-run", "--verdicts"])
        out = capsys.readouterr().out
        assert "WOULD update from the judgment: 0" in out
        assert "1 accused bind(s) were left exactly as they were" in out

    def test_a_failed_write_leaves_its_rows_in_the_undecided_count(
            self, monkeypatch, patched_fetch_markdown, capsys):
        class _FailingApi(_SearchStubApi):
            def replace_list(self, slug, path, items, timeout=60, if_match=None):
                raise urllib.error.HTTPError(
                    "https://x/api/cases/", 412, "Precondition Failed", {}, None)

        api = _FailingApi([_accused_case()])
        stub = _two_call_stub(verdict_response=VERDICT_RESPONSE)
        _run_main(monkeypatch, api, invoke_text_stub=stub,
                  argv=["--apply", "--verdicts"])
        out = capsys.readouterr().out
        assert "updated from the judgment: 0" in out
        assert "1 accused bind(s) were left exactly as they were" in out


class TestVerdictNameMapping:
    def test_two_accused_sharing_a_display_name_are_both_left_alone(
            self, monkeypatch, patched_fetch_markdown, tmp_path):
        # A verdict keyed by name cannot say which of two namesakes the court
        # meant, and binding it to either is a coin flip on a criminal record.
        case = _accused_case(extra_entities=[
            {"nes_id": SECOND_ACCUSED_IRI, "type": "accused",
             "display_name": "राम बहादुर", "outcome": "charged", "notes": ""}])
        api = _SearchStubApi([case])
        stub = _two_call_stub(verdict_response=VERDICT_RESPONSE)
        _run_main(monkeypatch, api, invoke_text_stub=stub, argv=["--apply", "--verdicts"])
        assert stub.verdict_calls == []
        assert api.replace_list_calls == []
        rows = _verdict_rows(tmp_path)
        assert {r["nes_id"] for r in rows} == {ACCUSED_IRI, SECOND_ACCUSED_IRI}
        assert all(r["written"] is False and r["reason"] for r in rows)

    def test_an_accused_bind_with_no_display_name_is_recorded_not_guessed_at(
            self, monkeypatch, patched_fetch_markdown, tmp_path):
        # display_name is None when NES cannot resolve the id -- there is no
        # name to match against the judgment, so there is no verdict to be had.
        case = _accused_case(display_name=None)
        api = _SearchStubApi([case])
        stub = _two_call_stub(verdict_response=VERDICT_RESPONSE)
        _run_main(monkeypatch, api, invoke_text_stub=stub, argv=["--apply", "--verdicts"])
        assert stub.verdict_calls == []
        assert api.replace_list_calls == []
        rows = _verdict_rows(tmp_path)
        assert [r["nes_id"] for r in rows] == [ACCUSED_IRI]
        assert "display_name" in rows[0]["reason"]

    def test_a_name_the_model_never_answered_for_is_flagged_not_dropped(
            self, monkeypatch, patched_fetch_markdown, tmp_path):
        api = _SearchStubApi([_accused_case()])
        stub = _two_call_stub(verdict_response=json.dumps({"defendants": []}))
        _run_main(monkeypatch, api, invoke_text_stub=stub, argv=["--apply", "--verdicts"])
        assert api.replace_list_calls == []
        rows = _verdict_rows(tmp_path)
        assert [r["nes_id"] for r in rows] == [ACCUSED_IRI]
        assert rows[0]["reason"] and rows[0]["written"] is False

    def test_a_verdict_for_someone_else_is_never_applied(
            self, monkeypatch, patched_fetch_markdown, tmp_path):
        # `accused_verdicts` reconciles by exact name; this pins that the
        # name -> nes_id mapping main() owns does not re-open the hole.
        api = _SearchStubApi([_accused_case()])
        stub = _two_call_stub(verdict_response=json.dumps({"defendants": [
            {"name": "श्याम बहादुर", "outcome": "convicted", "role": "स",
             "evidence": "ठहर्छ"}]}, ensure_ascii=False))
        _run_main(monkeypatch, api, invoke_text_stub=stub, argv=["--apply", "--verdicts"])
        assert api.replace_list_calls == []
        rows = _verdict_rows(tmp_path)
        assert [r["nes_id"] for r in rows] == [ACCUSED_IRI]
        assert all(r["name"] != "श्याम बहादुर" for r in rows)


class TestVerdictWrite:
    # USE `_SearchStubApi`, NOT `_StubApi`, FOR ANY TEST THAT EXPECTS A WRITE.
    # `_StubApi` deliberately has no `get_case_with_etag`, so
    # `plan_case_entities` captures no ETag, `_check_entity_plan` refuses the
    # unconditional whole-list replace, and the case is recorded as an error
    # having written nothing. The module already documents this. `_SearchStubApi`
    # subclasses it, serves `etag = 'W/"abc123"'`, and records
    # `replace_list_calls` entries as FOUR-tuples `(slug, path, items, if_match)`.

    def test_the_patch_carries_the_new_outcome_and_note(
            self, monkeypatch, patched_fetch_markdown):
        api = _SearchStubApi([_accused_case()])
        stub = _two_call_stub(verdict_response=VERDICT_RESPONSE)
        _run_main(monkeypatch, api, invoke_text_stub=stub, argv=["--apply", "--verdicts"])
        assert api.replace_list_calls, "the run wrote nothing"
        _slug, path, items, if_match = api.replace_list_calls[0]
        assert path == "entities"
        assert if_match == api.etag
        row = next(i for i in items if i["nes_id"] == ACCUSED_IRI)
        assert row["outcome"] == "convicted"
        assert row["notes"].startswith("तत्कालीन सचिव")

    def test_a_dry_run_writes_nothing(self, monkeypatch, patched_fetch_markdown):
        api = _SearchStubApi([_accused_case()])
        stub = _two_call_stub(verdict_response=VERDICT_RESPONSE)
        _run_main(monkeypatch, api, invoke_text_stub=stub, argv=["--dry-run", "--verdicts"])
        assert api.replace_list_calls == []
        assert api.patch_calls == []

    def test_the_verdict_and_the_extraction_share_one_patch(
            self, monkeypatch, patched_fetch_markdown):
        # ONE conditional whole-list replace per case, never two: the /entities
        # write is destructive, so a second PATCH would re-run the whole
        # delete-and-recreate against a list the first one just changed.
        api = _SearchStubApi(
            [_accused_case()],
            {"साझा भण्डार सहकारी": [{"id": SAJHA_IRI,
                                     "title": {"ne": "साझा भण्डार सहकारी"},
                                     "score": 200.0}]})
        stub = _two_call_stub(entity_response=ENTITY_RESPONSE,
                              verdict_response=VERDICT_RESPONSE)
        _run_main(monkeypatch, api, invoke_text_stub=stub, argv=["--apply", "--verdicts"])
        assert len(api.replace_list_calls) == 1
        _slug, _path, items, _if_match = api.replace_list_calls[0]
        by_id = {i["nes_id"]: i for i in items}
        assert by_id[ACCUSED_IRI]["outcome"] == "convicted"
        assert SAJHA_IRI in by_id

    def test_a_non_terminal_outcome_never_reaches_the_patch(
            self, monkeypatch, patched_fetch_markdown):
        # Asserted on the ITEMS, not on the absence of a call: a test that only
        # checks `replace_list_calls == []` passes just as well when the whole
        # verdict feature is deleted. The run must reach the write path with a
        # non-terminal verdict in hand -- an extraction bind supplies the PATCH --
        # and the accused row in that PATCH must carry no new `outcome`, so the
        # server's omitted-outcome preservation keeps the stored verdict.
        api = _SearchStubApi(
            [_accused_case()],
            {"साझा भण्डार सहकारी": [{"id": SAJHA_IRI,
                                     "title": {"ne": "साझा भण्डार सहकारी"},
                                     "score": 200.0}]})
        stub = _two_call_stub(entity_response=ENTITY_RESPONSE,
                              verdict_response=json.dumps({"defendants": [
                                  {"name": "राम बहादुर", "outcome": "charged",
                                   "role": "", "evidence": ""}]}, ensure_ascii=False))
        _run_main(monkeypatch, api, invoke_text_stub=stub, argv=["--apply", "--verdicts"])
        assert len(api.replace_list_calls) == 1, "the write path was never reached"
        _slug, _path, items, if_match = api.replace_list_calls[0]
        assert if_match == api.etag
        assert SAJHA_IRI in {i["nes_id"] for i in items}, "no PATCH-forcing bind"
        row = next(i for i in items if i["nes_id"] == ACCUSED_IRI)
        assert "outcome" not in row
        assert row["notes"] == "प्रतिवादी — विशेष अदालत मुद्दा 079-cr-0071"

    def test_a_bind_this_run_never_touched_survives_the_whole_list_replace(
            self, monkeypatch, patched_fetch_markdown):
        # THE PROPERTY MOST WORTH PINNING. `/entities` deletes every bind and
        # recreates the list from exactly what is sent, so an omitted bind is a
        # deleted bind -- and this case is the production shape: a `related`
        # bind is already there (which skips extraction entirely), carrying a
        # human's note that nothing in this run may touch.
        human_note = "ठेक्का प्राप्त गर्ने संस्था — मानिसले लेखेको टिप्पणी"
        untouched = {"nes_id": SAJHA_IRI, "type": "related",
                     "display_name": "साझा भण्डार सहकारी", "notes": human_note}
        api = _SearchStubApi([_accused_case(extra_entities=[untouched])])
        stub = _two_call_stub(verdict_response=VERDICT_RESPONSE)
        _run_main(monkeypatch, api, invoke_text_stub=stub, argv=["--apply", "--verdicts"])

        assert len(api.replace_list_calls) == 1
        _slug, path, items, if_match = api.replace_list_calls[0]
        assert (path, if_match) == ("entities", api.etag)
        by_id = {i["nes_id"]: i for i in items}
        assert by_id[SAJHA_IRI] == {"nes_id": SAJHA_IRI,
                                    "relationship_type": "related",
                                    "notes": human_note}
        assert by_id[ACCUSED_IRI]["outcome"] == "convicted"


class TestVerdictReport:
    def test_verdicts_jsonl_records_a_row_that_was_not_written(
            self, monkeypatch, patched_fetch_markdown, tmp_path):
        # A run that changes nothing must still say what it saw.
        api = _StubApi([_accused_case()])
        stub = _two_call_stub(verdict_response=json.dumps({"defendants": [
            {"name": "राम बहादुर", "outcome": "unknown", "role": "",
             "evidence": ""}]}, ensure_ascii=False))
        _run_main(monkeypatch, api, invoke_text_stub=stub, argv=["--dry-run", "--verdicts"])
        rows = _verdict_rows(tmp_path)
        assert rows and rows[0]["new_outcome"] == "unknown"
        assert rows[0]["written"] is False

    def test_a_refused_human_note_is_reported(
            self, monkeypatch, patched_fetch_markdown, tmp_path):
        api = _StubApi([_accused_case(notes="तत्कालीन प्रमुख — मुख्य प्रतिवादी")])
        stub = _two_call_stub(verdict_response=VERDICT_RESPONSE)
        _run_main(monkeypatch, api, invoke_text_stub=stub, argv=["--dry-run", "--verdicts"])
        rows = _verdict_rows(tmp_path)
        assert any("note" in (r.get("reason") or "") for r in rows)

    def test_the_evidence_phrase_rides_on_the_bind_row(
            self, monkeypatch, patched_fetch_markdown, tmp_path):
        # Every factual claim needs a cited source. This is that rule applied to
        # a machine write, and the only way a wrong `convicted` is findable.
        api = _StubApi([_accused_case()])
        stub = _two_call_stub(verdict_response=VERDICT_RESPONSE)
        _run_main(monkeypatch, api, invoke_text_stub=stub, argv=["--dry-run", "--verdicts"])
        text = _read_report(tmp_path, "verdicts.jsonl")
        assert "निज प्रतिवादीले कसुर गरेको ठहर्छ" in text

    def test_a_written_verdict_also_lands_in_binds_jsonl_with_its_evidence(
            self, monkeypatch, patched_fetch_markdown, tmp_path):
        api = _SearchStubApi([_accused_case()])
        stub = _two_call_stub(verdict_response=VERDICT_RESPONSE)
        _run_main(monkeypatch, api, invoke_text_stub=stub, argv=["--apply", "--verdicts"])
        rows = [json.loads(line) for line in
                _read_report(tmp_path, "binds.jsonl").splitlines() if line.strip()]
        row = next(r for r in rows if r["nes_id"] == ACCUSED_IRI)
        assert row["role"] == "accused"
        assert "निज प्रतिवादीले कसुर गरेको ठहर्छ" in row["reason"]
        assert row["written"] is True

    def test_the_verdict_step_logs_an_event_per_case(
            self, monkeypatch, patched_fetch_markdown):
        api = _SearchStubApi([_accused_case()])
        stub = _two_call_stub(verdict_response=VERDICT_RESPONSE)
        _run_main(monkeypatch, api, invoke_text_stub=stub, argv=["--apply", "--verdicts"])
        steps = {(r["step"], r["status"]) for r in _read_events(_events_path())}
        assert ("verdicts", "ok") in steps

    def test_the_epilogue_totals_the_verdicts(
            self, monkeypatch, patched_fetch_markdown, capsys):
        api = _SearchStubApi([_accused_case()])
        stub = _two_call_stub(verdict_response=VERDICT_RESPONSE)
        _run_main(monkeypatch, api, invoke_text_stub=stub, argv=["--apply", "--verdicts"])
        assert "accused bind(s) updated from the judgment: 1" in capsys.readouterr().out

    def test_a_refused_write_says_so_on_the_row_it_would_have_written(
            self, monkeypatch, patched_fetch_markdown, tmp_path):
        # `_StubApi` has no `get_case_with_etag`, so the unconditional
        # whole-list replace is refused at the write gate. The bind DID change,
        # so `settle_verdict_rows` has nothing to explain -- without a reason
        # here the row reads `written: false` and says nothing at all.
        api = _StubApi([_accused_case()])
        stub = _two_call_stub(verdict_response=VERDICT_RESPONSE)
        _run_main(monkeypatch, api, invoke_text_stub=stub, argv=["--dry-run", "--verdicts"])
        rows = _verdict_rows(tmp_path)
        row = next(r for r in rows if r["nes_id"] == ACCUSED_IRI)
        assert row["written"] is False
        assert "not written" in row["reason"] and "ETag" in row["reason"]

    def test_a_failed_write_says_so_on_the_row_it_would_have_written(
            self, monkeypatch, patched_fetch_markdown, tmp_path):
        class _FailingApi(_SearchStubApi):
            def replace_list(self, slug, path, items, timeout=60, if_match=None):
                raise urllib.error.HTTPError(
                    "https://x/api/cases/", 412, "Precondition Failed", {}, None)

        api = _FailingApi([_accused_case()])
        stub = _two_call_stub(verdict_response=VERDICT_RESPONSE)
        _run_main(monkeypatch, api, invoke_text_stub=stub, argv=["--apply", "--verdicts"])
        row = next(r for r in _verdict_rows(tmp_path) if r["nes_id"] == ACCUSED_IRI)
        assert row["written"] is False
        assert "write failed" in row["reason"]

    def test_report_paths_carries_the_verdicts_file(self):
        paths = ere.report_paths({"log": "/tmp/run-abc.log"})
        assert paths["verdicts"] == "/tmp/run-abc.verdicts.jsonl"


# --------------------------------------------------------------------------
# A case the judgment only half-answered, and the epilogue that names it. The
# defendants a short chunk never answered for stay `charged`; the gate is
# per-bind, so a re-run asks about exactly those, and the epilogue's list is
# what to check by hand when a re-run leaves them undecided.
# --------------------------------------------------------------------------

SECOND_ACCUSED = {"nes_id": SECOND_ACCUSED_IRI, "type": "accused",
                  "display_name": "सीता देवी", "outcome": "charged",
                  "notes": "प्रतिवादी — विशेष अदालत मुद्दा 079-cr-0071"}


class TestVerdictCoverageEpilogue:
    def test_a_half_answered_judgment_is_named_as_partially_decided(
            self, monkeypatch, patched_fetch_markdown, capsys):
        # Two accused, and VERDICT_RESPONSE answers for राम बहादुर only.
        api = _SearchStubApi([_accused_case(slug="case-half",
                                            extra_entities=[SECOND_ACCUSED])])
        stub = _two_call_stub(verdict_response=VERDICT_RESPONSE)
        _run_main(monkeypatch, api, invoke_text_stub=stub, argv=["--apply", "--verdicts"])
        out = capsys.readouterr().out
        assert "1 partially decided" in out
        assert "\n      case-half\n" in out, "the half-decided case is not named"
        # NOT locked: the per-bind gate lets a re-run decide the rest.
        assert "LOCKED" not in out
        assert "re-run" in out

    def test_a_fully_answered_judgment_is_not_reported_as_partial(
            self, monkeypatch, patched_fetch_markdown, capsys):
        api = _SearchStubApi([_accused_case(slug="case-whole")])
        stub = _two_call_stub(verdict_response=VERDICT_RESPONSE)
        _run_main(monkeypatch, api, invoke_text_stub=stub, argv=["--apply", "--verdicts"])
        out = capsys.readouterr().out
        assert "1 fully decided" in out and "0 partially decided" in out
        assert "LOCKED" not in out

    def test_a_case_nothing_was_decided_on_counts_as_undecided(
            self, monkeypatch, patched_fetch_markdown, capsys):
        # No court order, so the gate never reads a judgment -- the case is
        # untouched, not half-done, and must not be filed with the partial ones.
        api = _StubApi([_accused_case(slug="case-untouched", court=False)])
        stub = _two_call_stub(verdict_response=VERDICT_RESPONSE)
        _run_main(monkeypatch, api, invoke_text_stub=stub, argv=["--dry-run", "--verdicts"])
        out = capsys.readouterr().out
        assert "1 undecided" in out
        assert "LOCKED" not in out

    def test_a_run_without_verdicts_reports_no_coverage_at_all(
            self, monkeypatch, patched_fetch_markdown, capsys):
        # Nothing read the judgments, so there is no coverage to claim.
        api = _StubApi([_accused_case(slug="case-skipped")])
        stub = _two_call_stub(verdict_response=VERDICT_RESPONSE)
        _run_main(monkeypatch, api, invoke_text_stub=stub,
                  argv=["--dry-run"])
        assert "ACCUSED VERDICT COVERAGE" not in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Accused role notes reach a settled case, and a defendant is not re-bound
# under a lesser section.
# ---------------------------------------------------------------------------

ROLE_NOTE = "तत्कालीन प्रबन्ध निर्देशक, नेपाल टेलिकम"


def _notes_response(name="राम बहादुर", role=ROLE_NOTE, entities=()):
    return with_evidence(json.dumps({"entities": list(entities),
                                     "accused_notes": [{"name": name, "notes": role}]},
                                    ensure_ascii=False), FIXTURE_EVIDENCE)


class TestAccusedNoteUpdates:
    """`accused_note_updates` -- the mapping, in isolation."""

    def test_a_settled_bind_still_gets_its_note(self):
        case = _accused_case(outcome="acquitted")
        updates = ere.accused_note_updates(
            case, [{"name": "राम बहादुर", "notes": ROLE_NOTE}])
        assert updates == {ACCUSED_IRI: {"notes": ROLE_NOTE}}

    def test_no_outcome_key_is_ever_produced(self):
        # The whole safety story: `apply_accused_updates` only writes an
        # outcome it is handed, so absence is what protects the verdict.
        case = _accused_case(outcome="convicted")
        updates = ere.accused_note_updates(
            case, [{"name": "राम बहादुर", "notes": ROLE_NOTE}])
        assert "outcome" not in updates[ACCUSED_IRI]

    def test_namesakes_are_both_skipped(self):
        case = _accused_case(extra_entities=[
            {"nes_id": SECOND_ACCUSED_IRI, "type": "accused",
             "display_name": "राम बहादुर", "outcome": "acquitted", "notes": ""}])
        assert ere.accused_note_updates(
            case, [{"name": "राम बहादुर", "notes": ROLE_NOTE}]) == {}

    def test_a_name_no_bind_carries_is_dropped(self):
        case = _accused_case()
        assert ere.accused_note_updates(
            case, [{"name": "श्याम बहादुर", "notes": ROLE_NOTE}]) == {}

    def test_a_blank_role_is_not_written(self):
        case = _accused_case()
        assert ere.accused_note_updates(
            case, [{"name": "राम बहादुर", "notes": "   "}]) == {}

    def test_junk_rows_do_not_raise(self):
        case = _accused_case()
        assert ere.accused_note_updates(case, ["nope", None, {}]) == {}

    def test_a_long_role_note_is_capped_like_the_verdict_path(self):
        # `CaseEntityRelationship.notes` is an uncapped TextField and the
        # serializer publishes it beside the name ("``notes`` is PUBLIC -- the
        # party's role line"), so the prompt's "under 80 chars" is a request.
        # The two writers must not cap the same column differently.
        long_role = "क" * 500
        case = _accused_case()
        written = ere.accused_note_updates(
            case, [{"name": "राम बहादुर", "notes": long_role}])[ACCUSED_IRI]
        assert len(written["notes"]) == ere.ROLE_NOTE_MAX_CHARS
        verdict = ere.parse_verdict_response(json.dumps(
            {"defendants": [{"name": "राम बहादुर", "outcome": "convicted",
                             "role": long_role}]}, ensure_ascii=False))
        assert len(verdict[0]["role"]) == len(written["notes"])

    def test_a_note_inside_the_cap_is_untouched(self):
        case = _accused_case()
        assert ere.accused_note_updates(
            case, [{"name": "राम बहादुर", "notes": ROLE_NOTE}]
        )[ACCUSED_IRI] == {"notes": ROLE_NOTE}


class TestAccusedVerdictTargetsAfterTheSplit:
    """The settled filter must stay on the VERDICT path, not the shared one."""

    def test_the_verdict_path_still_skips_a_settled_bind(self):
        targets, skipped = ere.accused_verdict_targets(
            _accused_case(outcome="acquitted"))
        assert targets == {}
        assert "terminal outcome" in skipped[0][2]

    def test_the_shared_grouping_does_not(self):
        grouped, skipped = ere.accused_binds_by_name(
            _accused_case(outcome="acquitted"))
        assert list(grouped) == ["राम बहादुर"]
        assert skipped == []


class TestNotesReachASettledCase:
    """End to end: the case the verdict gate refuses still gets its notes."""

    def test_a_fully_settled_case_gets_notes_without_verdicts(
            self, monkeypatch, patched_fetch_markdown):
        case = _accused_case(outcome="acquitted")
        api = _SearchStubApi([case])
        stub = _two_call_stub(entity_response=_notes_response())
        _run_main(monkeypatch, api, invoke_text_stub=stub,
                  argv=["--apply", "--verdicts"])
        # The gate refused the case, so no judgment was read...
        assert stub.verdict_calls == []
        # ...and the note landed anyway.
        _slug, _path, items, _etag = api.replace_list_calls[0]
        written = [i for i in items if i["nes_id"] == ACCUSED_IRI]
        assert written[0]["notes"] == ROLE_NOTE

    def test_the_verdict_survives_the_note_write(
            self, monkeypatch, patched_fetch_markdown):
        api = _SearchStubApi([_accused_case(outcome="acquitted")])
        _run_main(monkeypatch, api, invoke_text_stub=_two_call_stub(
            entity_response=_notes_response()), argv=["--apply"])
        _slug, _path, items, _etag = api.replace_list_calls[0]
        # `outcome` is omitted entirely, which is what makes the server keep it.
        assert all("outcome" not in i for i in items)

    def test_notes_do_not_need_the_verdicts_flag(
            self, monkeypatch, patched_fetch_markdown):
        api = _SearchStubApi([_accused_case(outcome="acquitted")])
        _run_main(monkeypatch, api, invoke_text_stub=_two_call_stub(
            entity_response=_notes_response()), argv=["--apply"])
        _slug, _path, items, _etag = api.replace_list_calls[0]
        assert [i for i in items if i["nes_id"] == ACCUSED_IRI][0]["notes"] == ROLE_NOTE

    def test_a_human_written_note_is_never_overwritten(
            self, monkeypatch, patched_fetch_markdown):
        human = "यो मानिसको भूमिका हातले लेखिएको"
        api = _SearchStubApi([_accused_case(outcome="acquitted", notes=human)])
        _run_main(monkeypatch, api, invoke_text_stub=_two_call_stub(
            entity_response=_notes_response()), argv=["--apply"])
        assert api.replace_list_calls == []

    def test_a_note_only_write_lands_in_the_binds_audit_file(
            self, monkeypatch, patched_fetch_markdown, capsys):
        # A NOTE-ONLY WRITE IS STILL A WRITE. `changed_ids` is derived from
        # VERDICT rows, and a note-only update makes none, so this run sent a
        # real `replace_list` while reporting `0 bound, 0 verdict update(s)`
        # and an epilogue reading "bound zero entities because it extracted
        # none". This module's own docstrings call `*.binds.jsonl` the sole
        # audit trail, and a name-matched note is exactly the judgement call it
        # exists to record.
        api = _SearchStubApi([_accused_case(outcome="acquitted")])
        _run_main(monkeypatch, api, invoke_text_stub=_two_call_stub(
            entity_response=_notes_response()), argv=["--apply", "--verdicts"])
        assert api.replace_list_calls, "the fixture must actually write"

        binds = [json.loads(line) for line in Path(_report_files()["binds"])
                 .read_text(encoding="utf-8").splitlines()]
        note_rows = [b for b in binds if b["nes_id"] == ACCUSED_IRI]
        assert len(note_rows) == 1
        assert note_rows[0]["notes"] == ROLE_NOTE
        assert note_rows[0]["written"] is True
        assert note_rows[0]["role"] == "accused"
        # Same shape as `verdict_bind_row`, so the file stays readable as one.
        assert set(note_rows[0]) == {
            "slug", "extracted", "role", "nes_id", "score", "matched_name",
            "notes", "reason", "written"}
        assert "SET (accused) राम बहादुर " in capsys.readouterr().out

    def test_a_dry_run_says_it_would_set_the_note(
            self, monkeypatch, patched_fetch_markdown, capsys):
        api = _SearchStubApi([_accused_case(outcome="acquitted")])
        _run_main(monkeypatch, api, invoke_text_stub=_two_call_stub(
            entity_response=_notes_response()), argv=["--dry-run"])
        out = capsys.readouterr().out
        assert "WOULD SET (accused)" in out and "note only" in out
        binds = [json.loads(line) for line in Path(_report_files()["binds"])
                 .read_text(encoding="utf-8").splitlines()]
        assert [b["written"] for b in binds if b["nes_id"] == ACCUSED_IRI] == [False]

    def test_the_epilogue_no_longer_claims_the_run_wrote_nothing(
            self, monkeypatch, patched_fetch_markdown, capsys):
        api = _SearchStubApi([_accused_case(outcome="acquitted")])
        _run_main(monkeypatch, api, invoke_text_stub=_two_call_stub(
            entity_response=_notes_response()), argv=["--apply", "--verdicts"])
        out = capsys.readouterr().out
        assert "bound zero entities because it extracted none" not in out
        assert "TOTAL accused bind(s) given a role note" in out

    def test_a_refused_note_produces_no_bind_row(
            self, monkeypatch, patched_fetch_markdown):
        # `apply_accused_updates` leaves a human-written note alone, so nothing
        # is written and nothing may be claimed. The row is keyed on the
        # base -> updated DIFF, never on what the merge intended.
        human = "यो मानिसको भूमिका हातले लेखिएको"
        api = _SearchStubApi([_accused_case(outcome="acquitted", notes=human)])
        _run_main(monkeypatch, api, invoke_text_stub=_two_call_stub(
            entity_response=_notes_response()), argv=["--apply"])
        assert Path(_report_files()["binds"]).read_text(encoding="utf-8") == ""

    def test_a_verdict_role_note_wins_over_an_extracted_one(
            self, monkeypatch, patched_fetch_markdown):
        # The judgment's own wording is the better source; the extraction's job
        # title only fills a gap it leaves.
        api = _SearchStubApi([_accused_case(outcome="charged")])
        stub = _two_call_stub(entity_response=_notes_response(),
                              verdict_response=VERDICT_RESPONSE)
        _run_main(monkeypatch, api, invoke_text_stub=stub,
                  argv=["--apply", "--verdicts"])
        _slug, _path, items, _etag = api.replace_list_calls[0]
        note = [i for i in items if i["nes_id"] == ACCUSED_IRI][0]["notes"]
        assert note == "तत्कालीन सचिव, अर्थ मन्त्रालय — घूस लिने"


class TestAnAccusedIsNotReboundUnderAnotherSection:
    """The extraction is told not to name defendants; this is what happens when
    it does it anyway."""

    @staticmethod
    def _extracted(section):
        return with_evidence(json.dumps({"entities": [
            {"entity_name": "राम बहादुर", "relationship_type": section,
             "entity_prefix": "person", "entity_type": "Person",
             "is_named_entity": True, "name_en": "Ram Bahadur",
             "notes": "सम्बद्ध"}], "accused_notes": []}, ensure_ascii=False), FIXTURE_EVIDENCE)

    def _api(self, case):
        return _SearchStubApi([case], {
            "राम बहादुर": [{"id": ACCUSED_IRI, "title": {"ne": "राम बहादुर"},
                             "score": 180.0}]})

    def test_a_defendant_relabelled_related_is_refused(
            self, monkeypatch, patched_fetch_markdown):
        api = self._api(_accused_case(outcome="acquitted"))
        _run_main(monkeypatch, api,
                  invoke_text_stub=lambda **kw: self._extracted("related"),
                  argv=["--apply"])
        assert api.replace_list_calls == []

    def test_a_defendant_relabelled_alleged_is_refused(
            self, monkeypatch, patched_fetch_markdown):
        api = self._api(_accused_case(outcome="acquitted"))
        _run_main(monkeypatch, api,
                  invoke_text_stub=lambda **kw: self._extracted("alleged"),
                  argv=["--apply"])
        assert api.replace_list_calls == []

    def test_the_refusal_is_recorded_on_the_plan(self):
        case = _accused_case(outcome="acquitted")
        api = self._api(case)
        plan = ere.plan_case_entities(
            api, case, "etag-1",
            json.loads(self._extracted("related"))["entities"])
        assert plan.action == "NOOP"
        assert [(n, sec) for n, sec, _ in plan.already_accused] == [
            ("राम बहादुर", "related")]
        assert plan.bound == []

    def test_a_non_defendant_still_binds(self):
        # The guard must key on THIS case's accused, not on being a person.
        case = _accused_case(outcome="acquitted")
        other = "https://jawafdehi.org/entity/person/nani-kaji-thapa"
        api = _SearchStubApi([case], {
            "नानी काजी थापा": [{"id": other, "title": {"ne": "नानी काजी थापा"},
                                 "score": 180.0}]})
        items = [{"entity_name": "नानी काजी थापा", "relationship_type": "alleged",
                  "entity_prefix": "person", "entity_type": "Person",
                  "is_named_entity": True, "name_en": "", "notes": "उल्लेख"}]
        plan = ere.plan_case_entities(api, case, "etag-1", items)
        assert plan.already_accused == []
        assert [d.nes_id for _n, d, _no, _s in plan.bound] == [other]

    def test_the_guard_cannot_be_silently_disabled(self):
        # `accused_ids` carries no default: a second caller that forgot it would
        # re-bind defendants under a lesser role with no error anywhere. Asserted
        # on the signature rather than by calling short -- `ty` rejects that call
        # at check time, which is the contract working.
        param = inspect.signature(ere._bind_one).parameters["accused_ids"]
        assert param.default is inspect.Parameter.empty


class TestNoteNameFolding:
    """The court and the model join compound given names; NES spaces them."""

    @pytest.mark.parametrize("written,bound", [
        ("रामप्रसाद घिमिरे", "राम प्रसाद घिमिरे"),
        ("जयराज घिमिरे", "जय राज घिमिरे"),
        ("चन्द्रकुमार पोखरेल", "चन्द्र कुमार पोखरेल"),
        ("धर्मराज खड्का", "धर्म राज खड्का"),
        ("बिष्णुप्रसाद न्यौपाने", "बिष्णु प्रसाद न्यौपाने"),
    ])
    def test_a_joined_given_name_matches_its_spaced_bind(self, written, bound):
        case = _accused_case(outcome="acquitted", display_name=bound)
        assert ere.accused_note_updates(
            case, [{"name": written, "notes": ROLE_NOTE}]) == {
                ACCUSED_IRI: {"notes": ROLE_NOTE}}

    def test_a_collision_created_by_the_fold_is_dropped(self):
        # If two binds fold to one key they are as ambiguous as two binds
        # sharing a display name, and get the same treatment.
        case = _accused_case(outcome="acquitted", display_name="राम प्रसाद")
        case["entities"].append(
            {"nes_id": SECOND_ACCUSED_IRI, "type": "accused",
             "display_name": "रामप्रसाद", "outcome": "acquitted", "notes": ""})
        assert ere.accused_note_updates(
            case, [{"name": "रामप्रसाद", "notes": ROLE_NOTE}]) == {}


class TestNoteVariantFolding:
    """Spelling variants that are one person, and the ones that are not."""

    @pytest.mark.parametrize("written,bound", [
        ("बिकास श्रेष्ठ", "विकास श्रेष्ठ"),          # ba / va
        ("घनश्याम दुबे", "घनश्याम दुवे"),
        ("हरिशंकर शर्मा", "हरीशंकर शर्मा"),          # vowel length
        ("इच्छाकुमार श्रेष्ठ", "ईच्छाकुमार श्रेष्ठ"),
        ("रामकिशोर शाह", "रामकिशोर साह"),          # sibilant
    ])
    def test_a_spelling_variant_matches(self, written, bound):
        case = _accused_case(outcome="acquitted", display_name=bound)
        assert ere.accused_note_updates(
            case, [{"name": written, "notes": ROLE_NOTE}]) == {
                ACCUSED_IRI: {"notes": ROLE_NOTE}}

    @pytest.mark.parametrize("written,bound", [
        ("सरोज श्रेष्ठ", "सुरज श्रेष्ठ"),
        ("मिना अधिकारी", "मुना अधिकारी"),
        ("हरि बहादुर", "हिरा बहादुर"),
        ("राजकुमार साह", "राजकुमार सिंह"),
    ])
    def test_two_different_people_never_match(self, written, bound):
        # Every pair here is one the "drop all matras" fold merged. It was
        # measured over 2,860 production binds and rejected for exactly this.
        case = _accused_case(outcome="acquitted", display_name=bound)
        assert ere.accused_note_updates(
            case, [{"name": written, "notes": ROLE_NOTE}]) == {}

    def test_an_inserted_matra_matches_when_it_is_the_only_candidate(self):
        case = _accused_case(outcome="acquitted", display_name="प्रशान्त बोहोरा")
        assert ere.accused_note_updates(
            case, [{"name": "प्रशान्त बोहरा", "notes": ROLE_NOTE}]) == {
                ACCUSED_IRI: {"notes": ROLE_NOTE}}

    def test_the_exact_match_wins_and_the_near_pass_never_runs(self):
        # दल / दिल differ by one matra and are different people. With BOTH on
        # the case, the exact key must claim the note; a near match must not
        # get the chance to take it.
        case = _accused_case(outcome="acquitted", display_name="दल बहादुर")
        case["entities"].append(
            {"nes_id": SECOND_ACCUSED_IRI, "type": "accused",
             "display_name": "दिल बहादुर", "outcome": "acquitted", "notes": ""})
        assert ere.accused_note_updates(
            case, [{"name": "दिल बहादुर", "notes": ROLE_NOTE}]) == {
                SECOND_ACCUSED_IRI: {"notes": ROLE_NOTE}}

    def test_two_near_candidates_are_refused_not_guessed_between(self):
        # वोहोरा and वोहेरा are the same length and each is one INSERTED matra
        # away from वोहरा, so the queried name has two candidates and may pick
        # neither. (The pair was वोहोरा/वोहारा until `ा` stopped being
        # insertable -- see `is_matra_variant`; वोहारा is no longer a candidate
        # at all, which left one and defeated the refusal this pins.)
        case = _accused_case(outcome="acquitted", display_name="वोहोरा")
        case["entities"].append(
            {"nes_id": SECOND_ACCUSED_IRI, "type": "accused",
             "display_name": "वोहेरा", "outcome": "acquitted", "notes": ""})
        assert ere.accused_note_updates(
            case, [{"name": "वोहरा", "notes": ROLE_NOTE}]) == {}

    def test_a_near_name_that_is_not_a_bind_cannot_take_an_exact_match(self):
        # THE DANGEROUS SHAPE, and the one the two passes being interleaved got
        # wrong. The case holds ONE accused, दिल बहादुर. The court order also
        # names दल बहादुर, who is not a bind at all, so the model returns a note
        # for each. The दल row finds no exact key, falls into the relaxed pass,
        # matches the one bind on the case -- and used to overwrite the note the
        # दिल row had already placed there, purely because it came second.
        case = _accused_case(outcome="acquitted", display_name="दिल बहादुर")
        assert ere.accused_note_updates(case, [
            {"name": "दिल बहादुर", "notes": ROLE_NOTE},
            {"name": "दल बहादुर", "notes": "वडा अध्यक्ष"},
        ]) == {ACCUSED_IRI: {"notes": ROLE_NOTE}}

    def test_the_exact_match_wins_whichever_order_the_notes_arrive_in(self):
        # The same two rows the other way round. An extraction's row order is
        # not a contract, so the answer may not depend on it.
        case = _accused_case(outcome="acquitted", display_name="दिल बहादुर")
        assert ere.accused_note_updates(case, [
            {"name": "दल बहादुर", "notes": "वडा अध्यक्ष"},
            {"name": "दिल बहादुर", "notes": ROLE_NOTE},
        ]) == {ACCUSED_IRI: {"notes": ROLE_NOTE}}

    def test_two_notes_folding_to_one_exact_key_are_refused(self):
        # विकास and बिकास fold to the same `note_match_key`, so both rows claim
        # the one bind. Last-wins silently staples whichever the model happened
        # to emit second onto the defendant; refuse instead, the way
        # `accused_binds_by_name` drops a shared display name.
        case = _accused_case(outcome="acquitted", display_name="विकास शर्मा")
        assert ere.accused_note_updates(case, [
            {"name": "विकास शर्मा", "notes": ROLE_NOTE},
            {"name": "बिकास शर्मा", "notes": "वडा अध्यक्ष"},
        ]) == {}

    def test_one_key_claimed_twice_with_the_SAME_role_is_not_a_conflict(self):
        # A duplicated row says nothing contradictory, so refusing it would cost
        # a real note for no gain.
        case = _accused_case(outcome="acquitted", display_name="विकास शर्मा")
        assert ere.accused_note_updates(case, [
            {"name": "विकास शर्मा", "notes": ROLE_NOTE},
            {"name": "बिकास शर्मा", "notes": ROLE_NOTE},
        ]) == {ACCUSED_IRI: {"notes": ROLE_NOTE}}

    def test_two_relaxed_notes_claiming_one_bind_are_refused(self):
        # The relaxed pass has the same collision. `वोहरा` and `वहोरा` each drop
        # a different `ो` from the single bind `वोहोरा`, so both are one
        # insertion away, neither is an exact key, and both reach it --
        # first-wins is as much a guess as last-wins.
        case = _accused_case(outcome="acquitted", display_name="वोहोरा")
        assert ere.accused_note_updates(case, [
            {"name": "वोहरा", "notes": ROLE_NOTE},
            {"name": "वहोरा", "notes": "वडा अध्यक्ष"},
        ]) == {}

    def test_a_relaxed_note_may_not_take_a_bind_an_exact_row_refused(self):
        # A key the exact pass REFUSED is spoken for, not free. Letting the
        # relaxed pass fill it would route round the refusal.
        case = _accused_case(outcome="acquitted", display_name="विकास")
        assert ere.accused_note_updates(case, [
            {"name": "विकास", "notes": ROLE_NOTE},
            {"name": "बिकास", "notes": "वडा अध्यक्ष"},
            {"name": "विकाास", "notes": "सचिव"},
        ]) == {}

    # A TRAILING `ा` IS THE FEMININE MARKER, not an ordinary inserted matra.
    # कमल/कमला are one insertion apart and are different people, usually of
    # different gender -- and the module docstring already names कमल थापा /
    # Kamala Thapa as a known hazard on the cross-script side. Rejecting an
    # insertion "at the end of the string" does NOT catch these: `note_match_key`
    # strips spaces, so on कमलाथापा the inserted `ा` sits mid-key.
    FEMININE_PAIRS = [
        ("कमल थापा", "कमला थापा"),
        ("सुनिल", "सुनिला"),
        ("गोपाल", "गोपाला"),
        ("बिमल", "बिमला"),
        ("रमेश", "रमेशा"),
    ]

    @pytest.mark.parametrize("masculine,feminine", FEMININE_PAIRS)
    def test_the_feminine_marker_is_not_an_insertable_matra(self, masculine,
                                                            feminine):
        assert not ere.is_matra_variant(ere.note_match_key(masculine),
                                        ere.note_match_key(feminine))

    @pytest.mark.parametrize("masculine,feminine", FEMININE_PAIRS)
    def test_a_masculine_note_never_lands_on_a_feminine_bind(self, masculine,
                                                             feminine):
        # End to end, with the feminine name as the case's ONLY accused bind --
        # the single-candidate shape the relaxed pass accepts.
        case = _accused_case(outcome="acquitted", display_name=feminine)
        assert ere.accused_note_updates(
            case, [{"name": masculine, "notes": ROLE_NOTE}]) == {}

    def test_the_motivating_insertion_still_matches(self):
        # The fold exists for बोहरा -> बोहोरा, which inserts `ो`, not `ा`.
        # Excluding the feminine marker must not cost this.
        assert ere.is_matra_variant(ere.note_match_key("बोहरा"),
                                    ere.note_match_key("बोहोरा"))


def test_the_variant_fold_keeps_these_known_pairs_apart():
    """A regression table of pairs `NOTE_VARIANTS` must never merge.

    NOT the measurement itself. `NOTE_VARIANTS` was chosen by running the fold
    over all 2,860 accused binds in FY076-079 and rejecting any candidate that
    collapsed two different accused; the six pairs below are the ones that
    rejected the aggressive fold, kept here so a future entry that re-merges
    them fails in CI. To re-run the corpus measurement, use
    `work/2026-09-01-fy078-079-enrichment-status/fold_probe.py` (read-only) in
    the jawafdehi-meta checkout -- a new `NOTE_VARIANTS` entry needs that, not
    this test.
    """
    different_people = [
        ("सरोज", "सुरज"), ("मिना", "मुना"), ("हरि", "हिरा"),
        ("दल बहादुर", "दिल बहादुर"), ("राजकुमार साह", "राजकुमार सिंह"),
        ("नविन कुमार साह", "नविन कुमार सिंह"),
    ]
    for a, b in different_people:
        assert ere.note_match_key(a) != ere.note_match_key(b), (a, b)


# ---------------------------------------------------------------------------
# enricher-fix-rules.json: entity.drop_duplicate_location_org and
# entity.reject_ecn_candidate_binds.
# ---------------------------------------------------------------------------

KANCHANPUR_CODED = "https://jawafdehi.org/entity/location/district/kanchanpur-np0772"
KANCHANPUR_BARE = "https://jawafdehi.org/entity/location/kanchanpur"


class TestAGazetteerTwinIsNotBoundAlongsideItsCodedRecord:
    """NES holds कञ्चनपुर twice. `resolve` drops the bare twin; this pins that
    `qualifying_binds` does not put it back.

    Filed under `related`, not `location` -- a `location`-typed item is refused
    before it can ever reach `resolve`/`qualifying_binds` now (Task 5). The
    narrowing itself is not a location-section feature: `qualifying_binds`
    fires on the shape of the QUALIFYING CANDIDATES (all `/entity/location/`
    IRIs, at least one gazetteer-coded), regardless of which section the
    extraction filed the name under -- see `TestTheGazetteerNarrowingSurvives
    APromotedReview.test_a_place_filed_under_another_section_still_drops_the_
    bare_twin` for the same point made explicitly.
    """

    @staticmethod
    def _api(case):
        return _SearchStubApi([case], {"कञ्चनपुर": [
            {"id": KANCHANPUR_CODED, "title": {"ne": "कञ्चनपुर"}, "score": 180.0},
            {"id": KANCHANPUR_BARE, "title": {"ne": "कञ्चनपुर"}, "score": 180.0}]})

    @staticmethod
    def _items():
        return [{"entity_name": "कञ्चनपुर", "relationship_type": "related",
                 "entity_prefix": "location/district", "entity_type": "Place",
                 "is_named_entity": True, "name_en": "Kanchanpur",
                 "notes": "कसुर भएको जिल्ला"}]

    def test_only_the_coded_record_binds(self):
        case = {"slug": "case-kanchanpur", "state": "DRAFT", "entities": []}
        plan = plan_case_entities(self._api(case), case, 'W/"e"', self._items())
        assert [i["nes_id"] for i in plan.patch_items] == [KANCHANPUR_CODED]

    def test_the_dropped_twin_still_reaches_the_report(self):
        # `candidates` is the audit trail -- narrowing the BIND must not hide
        # that NES holds the place twice.
        case = {"slug": "case-kanchanpur-2", "state": "DRAFT", "entities": []}
        plan = plan_case_entities(self._api(case), case, 'W/"e"', self._items())
        _name, decision, _notes, _section = plan.bound[0]
        assert KANCHANPUR_BARE in {c[1] for c in decision.candidates}

    def test_a_person_with_two_candidates_still_binds_both(self):
        # The narrowing must not touch the person fan-out that
        # `qualifying_binds` exists for.
        a = "https://jawafdehi.org/entity/person/anish-shrestha-1"
        b = "https://jawafdehi.org/entity/person/anish-shrestha-2"
        case = {"slug": "case-person-fanout", "state": "DRAFT", "entities": []}
        api = _SearchStubApi([case], {"अनिष श्रेष्ठ": [
            {"id": a, "title": {"ne": "अनिष श्रेष्ठ"}, "score": 180.0},
            {"id": b, "title": {"ne": "अनिष श्रेष्ठ"}, "score": 180.0}]})
        plan = plan_case_entities(api, case, 'W/"e"', [
            {"entity_name": "अनिष श्रेष्ठ", "relationship_type": "related",
             "entity_prefix": "person", "entity_type": "Person",
             "is_named_entity": True, "name_en": "", "notes": "क"}])
        assert {i["nes_id"] for i in plan.patch_items} == {a, b}


# कञ्चनपुर MASKS THE BUG BELOW, which is why the class above passes. The
# narrowing used to key on `decision.nes_id`, and `_promote_top_candidate`
# re-derives that from `Decision.candidates` -- a tuple `resolve` captured
# BEFORE its own narrowing. Which twin sorts first is pure lexicography:
# `location/district/kanchanpur-np0772` beats `location/kanchanpur` because
# `d` < `k`. It goes the other way for every district whose slug sorts before
# the literal `district/` -- achham, baglung, banke, bara, chitwan, dailekh,
# dhading -- and for six of the seven provinces against `province/`.
ACHHAM_CODED = "https://jawafdehi.org/entity/location/district/achham-np0901"
ACHHAM_BARE = "https://jawafdehi.org/entity/location/achham"


class _TruncatedCandidates(list):
    """A search result the API stopped early on -- `resolve`'s truncation veto."""

    complete = False


class TestTheGazetteerNarrowingSurvivesAPromotedReview:
    """The narrowing has to hold on every path that reaches a bind, not only
    the one where `resolve` returns a clean BIND."""

    @staticmethod
    def _candidates():
        return [{"id": ACHHAM_BARE, "title": {"ne": "अछाम"}, "score": 180.0},
                {"id": ACHHAM_CODED, "title": {"ne": "अछाम"}, "score": 180.0}]

    def _bind(self, slug, rel_type="related", complete=True):
        case = {"slug": slug, "state": "DRAFT", "entities": []}
        found = (list(self._candidates()) if complete
                 else _TruncatedCandidates(self._candidates()))
        api = _SearchStubApi([case], {"अछाम": found})
        plan = plan_case_entities(api, case, 'W/"e"', [
            {"entity_name": "अछाम", "relationship_type": rel_type,
             "entity_prefix": "location/district", "entity_type": "Place",
             "is_named_entity": True, "name_en": "Achham",
             "notes": "कसुर भएको जिल्ला"}])
        return {i["nes_id"] for i in plan.patch_items}

    def test_a_truncated_candidate_window_still_drops_the_bare_twin(self):
        # Routine for a common district name: the search stopped early, so
        # `resolve` REVIEWs on the truncation veto and the promotion re-derives
        # the winner from the un-narrowed tuple -- the bare twin, here. Filed
        # under `related`: a `location`-typed item is refused before any search
        # now (Task 5), so this can no longer be reached through that section
        # -- `prefer_gazetteer` (which would have narrowed at `resolve()` time
        # instead) is moot for the same reason.
        assert self._bind("case-achham-truncated", complete=False) == {
            ACHHAM_CODED}

    def test_a_place_filed_under_another_section_still_drops_the_bare_twin(self):
        # `prefer_gazetteer` was only ever on for the location section (and a
        # location item can no longer reach `resolve` at all, Task 5), so this
        # REVIEWs as an ambiguity and is then promoted. `bind_section`'s
        # coercion to `related` makes this easy for the extraction to produce.
        assert self._bind("case-achham-related", rel_type="related") == {
            ACHHAM_CODED}

    # `test_the_clean_location_path_is_unchanged` DELETED here (Task 5). It
    # pinned a `location`-typed item binding cleanly via `prefer_gazetteer`
    # narrowing at `resolve()` time -- a `location` item is refused outright
    # now, before any search, so that path is gone rather than "unchanged".
    # `test_a_place_filed_under_another_section_still_drops_the_bare_twin`
    # above covers the same narrowing via the (still-live) promoted-review path.

    def test_the_dropped_twin_still_reaches_the_report_on_a_promotion(self):
        case = {"slug": "case-achham-report", "state": "DRAFT", "entities": []}
        api = _SearchStubApi([case], {"अछाम": self._candidates()})
        plan = plan_case_entities(api, case, 'W/"e"', [
            {"entity_name": "अछाम", "relationship_type": "related",
             "notes": "कसुर भएको जिल्ला"}])
        _name, decision, _notes, _section = plan.bound[0]
        assert ACHHAM_BARE in {c[1] for c in decision.candidates}

    def test_a_place_beside_a_non_location_candidate_is_left_alone(self):
        # The narrowing may only fire when EVERY qualifying candidate is a
        # location. A coded district scoring alongside an organisation is a real
        # ambiguity, and dropping the organisation would decide it silently.
        org = "https://jawafdehi.org/entity/organization/achham"
        case = {"slug": "case-achham-mixed", "state": "DRAFT", "entities": []}
        api = _SearchStubApi([case], {"अछाम": [
            {"id": ACHHAM_CODED, "title": {"ne": "अछाम"}, "score": 180.0},
            {"id": org, "title": {"ne": "अछाम"}, "score": 180.0}]})
        plan = plan_case_entities(api, case, 'W/"e"', [
            {"entity_name": "अछाम", "relationship_type": "related",
             "notes": "क"}])
        assert {i["nes_id"] for i in plan.patch_items} == {ACHHAM_CODED, org}


class TestElectionRecordsAreNeverPromoted:
    ELECTION_DOC = {"identifier": [
        {"propertyID": "ecn-candidate-id", "value": "187623"}]}

    def test_strict_and_permissive_agree_on_an_election_record(self):
        # The one veto where the two modes must NOT differ.
        case = {"slug": "case-ecn-modes", "state": "DRAFT", "entities": []}
        items = [{"entity_name": "अंकुर खत्री", "relationship_type": "related",
                  "notes": "क"}]
        for strict in (False, True):
            api = _SearchStubApi([case], {"अंकुर खत्री": [ANKUR_CANDIDATE]},
                                 documents={ANKUR_IRI: self.ELECTION_DOC})
            plan = plan_case_entities(api, case, 'W/"e"', items, strict=strict)
            assert plan.bound == [], f"bound under strict={strict}"

    def test_a_clean_document_still_promotes_an_ambiguity(self):
        # Only the election veto became absolute. The ambiguity promotion that
        # permissive mode exists for must survive.
        case = {"slug": "case-still-promotes", "state": "DRAFT", "entities": []}
        api = _SearchStubApi([case], {"अनिष श्रेष्ठ": [ANISH_A, ANISH_B]})
        plan = plan_case_entities(api, case, 'W/"e"', [
            {"entity_name": "अनिष श्रेष्ठ", "relationship_type": "related",
             "notes": "क"}])
        assert plan.bound, "a clean ambiguity must still promote"

    # The fan-out, not the winner. `_resolve_with_vetoes` reads ONE document --
    # `decision.nes_id` -- and `qualifying_binds` then turns that decision into
    # one bind per qualifying candidate. Without a per-candidate re-check the
    # veto only ever fires when the election record happens to sort first, and
    # the sort is `(-score, nes_id)`, so a clean record with a lower slug hides
    # every namesake behind it. This is the shape the FY078/079 batch produced:
    # one CIAA investigating officer bound to five defeated local candidates.
    CLEAN_ANISH = "https://jawafdehi.org/entity/person/anish-shrestha-000001"

    def _tied_candidates(self):
        return [{"id": nes_id, "title": {"ne": "अनिष श्रेष्ठ"}, "score": 180.0}
                for nes_id in (self.CLEAN_ANISH, ANISH_A["id"], ANISH_B["id"])]

    def test_an_election_runner_up_is_not_bound_behind_a_clean_winner(self):
        case = {"slug": "case-ecn-fanout", "state": "DRAFT", "entities": []}
        api = _SearchStubApi(
            [case], {"अनिष श्रेष्ठ": self._tied_candidates()},
            documents={ANISH_A["id"]: self.ELECTION_DOC,
                       ANISH_B["id"]: self.ELECTION_DOC})
        plan = plan_case_entities(api, case, 'W/"e"', [
            {"entity_name": "अनिष श्रेष्ठ", "relationship_type": "related",
             "notes": "क"}])
        bound = {decision.nes_id for _n, decision, _no, _s in plan.bound}
        assert bound == {self.CLEAN_ANISH}, "an ECN record was bound as a runner-up"

    def test_a_vetoed_runner_up_is_reported_for_review_not_dropped(self):
        # Refused is not the same as unseen. The runner-up must reach
        # `*.review.jsonl` carrying the veto, or a bind this run declined
        # appears in no artefact at all.
        case = {"slug": "case-ecn-fanout-review", "state": "DRAFT", "entities": []}
        api = _SearchStubApi(
            [case], {"अनिष श्रेष्ठ": self._tied_candidates()},
            documents={ANISH_A["id"]: self.ELECTION_DOC,
                       ANISH_B["id"]: self.ELECTION_DOC})
        plan = plan_case_entities(api, case, 'W/"e"', [
            {"entity_name": "अनिष श्रेष्ठ", "relationship_type": "related",
             "notes": "क"}])
        reasons = [decision.reason for _n, decision, _s in plan.review]
        assert len(reasons) == 2
        assert all("Election Commission record" in reason for reason in reasons)
        # `apply_document_veto` blanks `nes_id` on a downgrade by contract, so
        # the only place the refused candidate is named is the reason. Without
        # that the two rows one fan-out produces are indistinguishable.
        assert all(decision.nes_id is None for _n, decision, _s in plan.review)
        assert {ANISH_A["id"], ANISH_B["id"]} == {
            iri for iri in (ANISH_A["id"], ANISH_B["id"])
            if any(iri in reason for reason in reasons)}

    def test_every_fanned_out_candidate_is_read_exactly_once(self):
        # The re-check costs one `get_entity` per runner-up and must not cost
        # two: the winner's document is already read by `_resolve_with_vetoes`.
        case = {"slug": "case-ecn-fanout-reads", "state": "DRAFT", "entities": []}
        api = _SearchStubApi(
            [case], {"अनिष श्रेष्ठ": self._tied_candidates()},
            documents={ANISH_A["id"]: self.ELECTION_DOC,
                       ANISH_B["id"]: self.ELECTION_DOC})
        plan_case_entities(api, case, 'W/"e"', [
            {"entity_name": "अनिष श्रेष्ठ", "relationship_type": "related",
             "notes": "क"}])
        assert sorted(api.get_entity_calls) == sorted(
            [self.CLEAN_ANISH, ANISH_A["id"], ANISH_B["id"]])

    def test_an_unreadable_runner_up_document_refuses_the_bind(self):
        # Fail closed on the fan-out exactly as `_resolve_with_vetoes` does on
        # the winner: a transient read failure must never leave a bind standing.
        case = {"slug": "case-ecn-fanout-unreadable", "state": "DRAFT",
                "entities": []}
        api = _SearchStubApi(
            [case], {"अनिष श्रेष्ठ": self._tied_candidates()},
            documents={ANISH_A["id"]: RuntimeError("502 Bad Gateway")})
        plan = plan_case_entities(api, case, 'W/"e"', [
            {"entity_name": "अनिष श्रेष्ठ", "relationship_type": "related",
             "notes": "क"}])
        bound = {decision.nes_id for _n, decision, _no, _s in plan.bound}
        assert ANISH_A["id"] not in bound
        assert ANISH_B["id"] in bound, "a clean runner-up must still bind"

    def test_a_clean_fan_out_still_binds_every_candidate(self):
        # The re-check must not become a second ambiguity veto: three clean
        # records still produce three binds and read three documents.
        case = {"slug": "case-clean-fanout", "state": "DRAFT", "entities": []}
        api = _SearchStubApi([case], {"अनिष श्रेष्ठ": self._tied_candidates()})
        plan = plan_case_entities(api, case, 'W/"e"', [
            {"entity_name": "अनिष श्रेष्ठ", "relationship_type": "related",
             "notes": "क"}])
        bound = {decision.nes_id for _n, decision, _no, _s in plan.bound}
        assert bound == {self.CLEAN_ANISH, ANISH_A["id"], ANISH_B["id"]}


class TestTheVerdictPromptCarriesItsGuardrails:
    """enricher-fix-rules.json `entity.outcome_from_verdict`. Each marker below
    is quoted from a real order in this corpus, so a reworded prompt that drops
    one fails here rather than in a criminal outcome."""

    @pytest.mark.parametrize("marker", [
        "जफत प्रयोजनको लागि प्रतिवादी",      # 079-CR-0019
        "प्रयोजनार्थ मात्र प्रतिवादी",         # 079-CR-0156
    ])
    def test_confiscation_only_defendants_are_described(self, marker):
        assert marker in ere.VERDICT_SYSTEM_PROMPT

    def test_a_confiscation_only_defendant_is_told_to_stay_charged(self):
        block = ere.VERDICT_SYSTEM_PROMPT[
            ere.VERDICT_SYSTEM_PROMPT.index("CONFISCATION-ONLY"):]
        block = block[:block.index("A SPLIT BENCH")]
        assert "charged" in block and "never acquitted" in block

    @pytest.mark.parametrize("marker", [
        "फरक राय", "मतैक्य हुन नसकी", "दफा ६ को उपदफा (४)",   # 079-CR-0025
    ])
    def test_the_split_bench_markers_are_named(self, marker):
        assert marker in ere.VERDICT_SYSTEM_PROMPT

    def test_a_split_bench_disagreement_is_told_to_answer_unknown(self):
        block = ere.VERDICT_SYSTEM_PROMPT[
            ere.VERDICT_SYSTEM_PROMPT.index("A SPLIT BENCH"):]
        block = block[:block.index("AN ABETTOR")]
        assert "unknown" in block

    @pytest.mark.parametrize("marker", [
        "मतियार", "दफा २२", "प्रतिबन्धात्मक वाक्यांश",          # 078-CR-0073
    ])
    def test_the_abettor_markers_are_named(self, marker):
        assert marker in ere.VERDICT_SYSTEM_PROMPT

    def test_abated_is_still_reserved_for_death(self):
        assert "मुद्दा तामेली" in ere.VERDICT_SYSTEM_PROMPT

    def test_the_outcome_vocabulary_is_unchanged(self):
        # The guardrails must not have introduced a fifth answer.
        assert ere.VERDICT_OUTCOMES == frozenset(
            {"convicted", "acquitted", "abated", "charged", "unknown"})


# --------------------------------------------------------------------------
# Task 6: source selection, the two window prompts, and the start-window loop,
# called directly. `TestMainWiring` drives the same path through `main()`.
# --------------------------------------------------------------------------

CAPTION_SHORT = "विशेष अदालत काठमाडौं। मुद्दा:"
FILLER_SENTENCE = "यो अदालतको आदेशको विवरण हो। "
BANKE_EVIDENCE = ("जिल्ला बाँके, खजुरा गाउँपालिका वडा नं.४ स्थित घरजग्गामा "
                  "भ्रष्ट रकम लगानी भएको पाइयो।")
BANKE_PLACE = "जिल्ला बाँके, खजुरा गाउँपालिका वडा नं.४"
READABLE_PRESS = "साझा भण्डार सहकारीको बिरुद्ध भ्रष्टाचार मुद्दा दायर गरिएको छ। " * 15
PREETI_ASCII_ORDER = "abc def ghijk lmnop qrstuv wxyz ABCDEFGHIJKLMNOPQRSTUVWXYZ 0123456789 " * 20


def _filler(n):
    """`n` chars of neutral Devanagari filler, no newline and no caption/holding markers --
    so `order_windows` cuts every window at exactly `size` chars, never earlier."""
    body = (FILLER_SENTENCE * (n // len(FILLER_SENTENCE) + 1))[:n]
    return body.replace("\n", " ")


def _order_with_location_at(n, position):
    """An `n`-char order: `CAPTION_SHORT` then filler, with `BANKE_EVIDENCE` spliced in at `position`."""
    body = CAPTION_SHORT + _filler(n - len(CAPTION_SHORT))
    return _splice(body, position, BANKE_EVIDENCE)


def _splice(text, position, snippet):
    """Same-length replace: put `snippet` at `position`, keeping `len(text)` unchanged."""
    return text[:position] + snippet + text[position + len(snippet):]


def _gaz():
    return Gazetteer(DISTRICTS, UNITS)


def _api():
    return _SearchStubApi([], {})


def _response(locations=(), entities=(), accused_notes=()):
    return json.dumps({"locations": list(locations), "entities": list(entities),
                        "accused_notes": list(accused_notes)}, ensure_ascii=False)


def _sequenced_stub(*responses):
    """An `invoke_text` stub answering each call from `responses` in turn, then repeating
    a nothing-found reply -- so a test only scripts the calls it cares about."""
    calls = []
    remaining = list(responses)
    nothing = _response()

    def stub(**kw):
        calls.append(kw)
        return remaining.pop(0) if remaining else nothing

    stub.calls = calls
    return stub


def _material(iri, mtype, link):
    return {"material_iri": iri, "material": {"material_type": mtype,
                                              "urls": [{"link": link, "role": "MARKDOWN"}]}}


class TestPickSource:
    """`pick_source`: court order first, the press release only on a real fallback."""

    def test_falls_back_to_the_press_release_when_the_order_is_preeti(self, monkeypatch):
        import casework.common.materials as m

        links = {"https://x/order.md": PREETI_ASCII_ORDER, "https://x/press.md": READABLE_PRESS}
        monkeypatch.setattr(m, "fetch_markdown", lambda link, timeout=60: links[link])
        detail = {"slug": "case-preeti", "state": "DRAFT", "evidence": [
            _material("https://jawafdehi.org/material/ngm/court_orders/1", "court_order",
                      "https://x/order.md"),
            _material("https://jawafdehi.org/material/ciaa/press_releases/1", "press_release",
                      "https://x/press.md"),
        ]}

        source = pick_source(detail)

        assert source.kind == "press_release"
        assert source.text == READABLE_PRESS
        assert "court order" in source.reason

    def test_joins_bound_parts_by_material_iri_not_evidence_order(self, monkeypatch):
        import casework.common.materials as m

        links = {"https://x/part1.md": "पहिलो अंश।", "https://x/part2.md": "दोस्रो अंश।"}
        monkeypatch.setattr(m, "fetch_markdown", lambda link, timeout=60: links[link])
        detail = {"slug": "case-parts", "state": "DRAFT", "evidence": [
            _material("https://jawafdehi.org/material/ngm/court_orders/x.2", "court_order",
                      "https://x/part2.md"),
            _material("https://jawafdehi.org/material/ngm/court_orders/x.1", "court_order",
                      "https://x/part1.md"),
        ]}

        source = pick_source(detail)

        assert source.kind == "court_order"
        assert source.text == "पहिलो अंश।\n\nदोस्रो अंश।"

    def test_prefers_a_readable_court_order_outright(self, monkeypatch):
        import casework.common.materials as m

        text = "यो अदालतको आदेशको पूर्ण पाठ हो। " * 20
        monkeypatch.setattr(m, "fetch_markdown", lambda link, timeout=60: text)
        detail = {"slug": "case-order-only", "state": "DRAFT", "evidence": [
            _material("https://jawafdehi.org/material/ngm/court_orders/1", "court_order",
                      "https://x/order.md"),
        ]}

        source = pick_source(detail)

        assert source == Source("court_order", text, "")

    def test_never_fetches_the_press_release_when_the_order_is_readable(self, monkeypatch):
        import casework.common.materials as m

        fetched = []

        def fake_fetch(link, timeout=60):
            fetched.append(link)
            return {"https://x/order.md": "यो अदालतको आदेशको पूर्ण पाठ हो। " * 20,
                    "https://x/press.md": READABLE_PRESS}[link]

        monkeypatch.setattr(m, "fetch_markdown", fake_fetch)
        detail = {"slug": "case-both-readable", "state": "DRAFT", "evidence": [
            _material("https://jawafdehi.org/material/ngm/court_orders/1", "court_order",
                      "https://x/order.md"),
            _material("https://jawafdehi.org/material/ciaa/press_releases/1", "press_release",
                      "https://x/press.md"),
        ]}

        source = pick_source(detail)

        assert source.kind == "court_order"
        assert fetched == ["https://x/order.md"]

    def test_returns_none_when_neither_source_is_usable(self, monkeypatch):
        import casework.common.materials as m

        monkeypatch.setattr(m, "fetch_markdown", lambda link, timeout=60: "")
        detail = {"slug": "case-none", "state": "DRAFT", "evidence": []}

        source = pick_source(detail)

        assert source.kind is None
        assert source.text == ""
        assert source.reason


#: The last main-branch commit carrying the retired single-call `SYSTEM_PROMPT`.
RETIRED_PROMPT_COMMIT = "5406f9e"


def _retired_system_prompt() -> str:
    """`SYSTEM_PROMPT` as it stood at `RETIRED_PROMPT_COMMIT`, read via `git show` + `ast`."""
    proc = subprocess.run(
        ["git", "show", f"{RETIRED_PROMPT_COMMIT}:casework/enrich_related_entities.py"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        pytest.skip(f"commit {RETIRED_PROMPT_COMMIT} not in local history (shallow clone?)")
    for node in ast.parse(proc.stdout).body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == "SYSTEM_PROMPT"):
            return ast.literal_eval(node.value)
    raise AssertionError(f"no SYSTEM_PROMPT at {RETIRED_PROMPT_COMMIT}")


class TestCourtOrderSystemPrompt:
    """`COURT_ORDER_SYSTEM_PROMPT`: PART 2/3 word for word, new PART 1 and output format."""

    def test_keeps_part_2_and_part_3_verbatim_from_the_old_prompt(self):
        old = _retired_system_prompt()
        tail_marker = "Only include primary accused persons. Keep notes under 80 chars."
        old_body = old[old.index("PART 2 — PEOPLE AND ORGANIZATIONS"):
                       old.index(tail_marker) + len(tail_marker)]
        assert old_body in ere.COURT_ORDER_SYSTEM_PROMPT

    def test_has_the_new_part_1_location_rules(self):
        prompt = ere.COURT_ORDER_SYSTEM_PROMPT
        assert "PART 1 — WHERE THE EVENTS HAPPENED" in prompt
        assert "place_as_written" in prompt
        for marker in ("स्थायी", "जन्मस्थान", "बस्ने", '"locations": []'):
            assert marker in prompt

    def test_output_format_matches_the_brief_shape(self):
        prompt = ere.COURT_ORDER_SYSTEM_PROMPT
        assert '"locations": [{"place_as_written"' in prompt
        assert '"accused_notes": [{"name"' in prompt
        assert "Every entities and accused_notes item also carries evidence" in prompt


class TestPressReleaseSystemPrompt:
    def test_names_the_boilerplate_and_refuses_witnesses_and_verdicts(self):
        prompt = ere.PRESS_RELEASE_SYSTEM_PROMPT
        for marker in ("टंगाल काठमाडौं", "विशेष अदालत काठमाडौं", "आयोगको कार्यालय"):
            assert marker in prompt
        assert "witnesses" in prompt
        assert "verdict" in prompt
        assert "जफत गर्ने प्रयोजन" in prompt

    def test_the_output_json_shape_matches_the_court_order_prompt(self):
        # Same three top-level keys, same per-item field names -- so
        # `parse_window_response` reads either reply identically.
        output = '{"locations": [{"place_as_written"'
        assert output in ere.PRESS_RELEASE_SYSTEM_PROMPT
        assert output in ere.COURT_ORDER_SYSTEM_PROMPT
        for field in ('"district"', '"evidence"', '"entity_name"', '"relationship_type"',
                     '"accused_notes"', '"name"', '"notes"'):
            assert field in ere.PRESS_RELEASE_SYSTEM_PROMPT
            assert field in ere.COURT_ORDER_SYSTEM_PROMPT


class TestParseWindowResponse:
    def test_reads_all_three_keys(self):
        text = json.dumps({
            "locations": [{"place_as_written": "बाँके", "district": "बाँके",
                          "evidence": "बाँके जिल्लामा घटना भएको हो।", "notes": ""}],
            "entities": [{"entity_name": "संस्था", "relationship_type": "related",
                        "evidence": "संस्था संलग्न रहेको पाइयो।", "notes": "क"}],
            "accused_notes": [{"name": "राम", "notes": "अधिकृत",
                              "evidence": "राम अधिकृत रहेका थिए।"}],
        }, ensure_ascii=False)

        answer = parse_window_response(text)

        assert answer.locations[0]["place_as_written"] == "बाँके"
        assert answer.entities[0]["entity_name"] == "संस्था"
        assert answer.accused_notes[0]["name"] == "राम"

    def test_moves_a_location_typed_entity_into_locations(self):
        text = json.dumps({
            "locations": [],
            "entities": [{"entity_name": "सुर्खेत", "relationship_type": "location",
                        "evidence": "सुर्खेत जिल्लामा घटना भएको हो।", "notes": "घटनास्थल"}],
            "accused_notes": [],
        }, ensure_ascii=False)

        answer = parse_window_response(text)

        assert answer.entities == []
        assert answer.locations == [{"place_as_written": "सुर्खेत", "district": "",
                                    "evidence": "सुर्खेत जिल्लामा घटना भएको हो।",
                                    "notes": "घटनास्थल"}]


class TestAccusedMissingNotes:
    """`accused_missing_notes` -- the same "needs a note" test `apply_accused_updates` uses."""

    def test_reports_a_placeholder_note_the_notes_do_not_fill(self):
        case = _accused_case()
        assert accused_missing_notes(case, []) == {ACCUSED_IRI}

    def test_is_empty_once_the_notes_fill_it(self):
        case = _accused_case()
        assert accused_missing_notes(
            case, [{"name": "राम बहादुर", "notes": ROLE_NOTE}]) == set()

    def test_ignores_a_bind_that_already_has_a_real_note(self):
        case = _accused_case(notes="वास्तविक व्यक्तिगत विवरण भएको नोट")
        assert accused_missing_notes(case, []) == set()


class TestExtractFromSourceCourtOrder:
    """`extract_from_source`'s start-window loop over a court order."""

    def test_stops_after_one_window_when_the_first_grounds_a_district(self):
        text = _order_with_location_at(100_000, len(CAPTION_SHORT) + 500)
        source = Source("court_order", text, "")
        case = {"slug": "case-banke", "state": "DRAFT", "entities": []}
        stub = _sequenced_stub(_response(locations=[
            {"place_as_written": BANKE_PLACE, "district": "बाँके",
             "evidence": BANKE_EVIDENCE, "notes": "रकम लगानी भएको जिल्ला"}]))

        extraction = extract_from_source(_api(), _gaz(), case, source, stub, usage=None)

        assert len(stub.calls) == 1
        assert any("/location/district/" in b.nes_id for b in extraction.location_binds)
        assert len(extraction.windows) == 1 and extraction.windows[0][0] == 0

    def test_grows_to_a_second_window_when_the_first_finds_no_location(self):
        text = _order_with_location_at(100_000, len(CAPTION_SHORT) + 100)
        assert ere.caption_end(text) < 1_000
        source = Source("court_order", text, "")
        case = {"slug": "case-grows", "state": "DRAFT", "entities": []}
        stub = _sequenced_stub(
            _response(),  # window 1: nothing found
            _response(locations=[{"place_as_written": BANKE_PLACE, "district": "बाँके",
                                  "evidence": BANKE_EVIDENCE, "notes": ""}]),
        )

        extraction = extract_from_source(_api(), _gaz(), case, source, stub, usage=None)

        assert len(stub.calls) == 2
        expected_label = list(start_windows(text))[1].label()
        assert stub.calls[1]["content"].startswith(expected_label)
        assert any("/location/district/" in b.nes_id for b in extraction.location_binds)

    def test_rejects_an_entity_and_an_accused_note_whose_evidence_is_not_in_the_window(self):
        text = _order_with_location_at(100_000, len(CAPTION_SHORT) + 100)
        source = Source("court_order", text, "")
        case = {"slug": "case-reject", "state": "DRAFT", "entities": []}
        stub = _sequenced_stub(_response(
            locations=[{"place_as_written": BANKE_PLACE, "district": "बाँके",
                       "evidence": BANKE_EVIDENCE, "notes": ""}],
            entities=[{"entity_name": "काल्पनिक संस्था", "relationship_type": "related",
                     "evidence": "यो वाक्य स्रोत पाठमा कहीं पनि छैन।", "notes": "क"}],
            accused_notes=[{"name": "काल्पनिक व्यक्ति", "notes": "क",
                           "evidence": "यो पनि स्रोत पाठमा छैन।"}]))

        extraction = extract_from_source(_api(), _gaz(), case, source, stub, usage=None)

        assert extraction.entities == []
        assert extraction.accused_notes == []
        rejected_names = {r.get("entity_name") or r.get("name") for r in extraction.rejected}
        assert rejected_names == {"काल्पनिक संस्था", "काल्पनिक व्यक्ति"}
        assert all(r["reason"] == "evidence not found in the source" for r in extraction.rejected)

    def test_the_invoke_text_call_carries_the_brief_shape(self):
        text = "यो अदालतको आदेशको पूर्ण पाठ हो। " * 5
        source = Source("court_order", text, "")
        case = {"slug": "case-call-shape", "state": "DRAFT", "entities": []}
        stub = _sequenced_stub(_response())
        sentinel_usage = object()

        extract_from_source(_api(), _gaz(), case, source, stub, usage=sentinel_usage)

        call = stub.calls[0]
        window = list(start_windows(text))[0]
        assert call["system"] == ere.COURT_ORDER_SYSTEM_PROMPT
        assert call["content"] == f"{window.label()}\n\n{window.text}"
        assert call["max_tokens"] == ere.EXTRACTION_MAX_TOKENS
        assert call["tier"] == ere.tier_for("entities")
        assert call["usage"] is sentinel_usage

    def test_keeps_growing_until_every_accused_note_is_filled_and_dedupes_the_district(self):
        text = _order_with_location_at(100_000, len(CAPTION_SHORT) + 100)
        w2 = list(start_windows(text))[1]
        note_evidence = "राम बहादुर तत्कालीन सचिव थिए भनी उल्लेख छ।"
        text = _splice(text, w2.start + 50, note_evidence)
        source = Source("court_order", text, "")
        case = {"slug": "case-needs-note", "state": "DRAFT", "entities": [
            {"nes_id": "https://jawafdehi.org/entity/person/x", "type": "accused",
             "display_name": "राम बहादुर", "notes": ere.MACHINE_NOTE_PREFIX + "079-cr-0001"},
        ]}
        banke_answer = {"place_as_written": BANKE_PLACE, "district": "बाँके",
                        "evidence": BANKE_EVIDENCE, "notes": ""}
        stub = _sequenced_stub(
            _response(locations=[banke_answer]),
            _response(locations=[banke_answer],
                      accused_notes=[{"name": "राम बहादुर", "notes": "तत्कालीन सचिव",
                                    "evidence": note_evidence}]),
        )

        extraction = extract_from_source(_api(), _gaz(), case, source, stub, usage=None)

        assert len(stub.calls) == 2
        district_binds = [b for b in extraction.location_binds if "/location/district/" in b.nes_id]
        assert len(district_binds) == 1, "the same district must not be bound twice across windows"
        assert extraction.accused_notes == [
            {"name": "राम बहादुर", "notes": "तत्कालीन सचिव", "evidence": note_evidence}]

    def test_stops_at_the_window_cap_without_a_district(self):
        text = CAPTION_SHORT + _filler(150_000 - len(CAPTION_SHORT))
        source = Source("court_order", text, "")
        case = {"slug": "case-no-district-ever", "state": "DRAFT", "entities": []}
        stub = _sequenced_stub()  # every call answers "nothing found"

        extract_from_source(_api(), _gaz(), case, source, stub, usage=None)

        assert len(stub.calls) == MAX_ENTITY_WINDOWS

    def test_sends_a_short_order_whole_in_one_call(self):
        text = "यो सानो अदालतको आदेश हो। " * 5
        source = Source("court_order", text, "")
        case = {"slug": "case-short", "state": "DRAFT", "entities": []}
        stub = _sequenced_stub(_response())

        extraction = extract_from_source(_api(), _gaz(), case, source, stub, usage=None)

        assert len(stub.calls) == 1
        assert extraction.windows == [(0, len(text))]

    def test_appends_the_system_suffix(self):
        text = "यो अदालतको आदेशको पूर्ण पाठ हो। " * 5
        source = Source("court_order", text, "")
        case = {"slug": "case-suffix", "state": "DRAFT", "entities": []}
        stub = _sequenced_stub(_response())

        extract_from_source(_api(), _gaz(), case, source, stub, usage=None,
                           system_suffix="\nEXTRA")

        assert stub.calls[0]["system"] == ere.COURT_ORDER_SYSTEM_PROMPT + "\nEXTRA"

    def test_propagates_an_invoke_text_failure(self):
        text = "यो अदालतको आदेशको पूर्ण पाठ हो। " * 5
        source = Source("court_order", text, "")
        case = {"slug": "case-raise", "state": "DRAFT", "entities": []}

        def boom(**kw):
            raise RuntimeError("claude_cli failed")

        with pytest.raises(RuntimeError, match="claude_cli failed"):
            extract_from_source(_api(), _gaz(), case, source, boom, usage=None)


class TestExtractFromSourcePressRelease:
    def test_is_one_call_with_caption_end_zero(self):
        # Evidence at the very start of the text would be refused for a court
        # order as "only in the caption" -- it must NOT be here, since the
        # press-release path passes `caption_end=0`.
        text = BANKE_EVIDENCE + " " + FILLER_SENTENCE * 20
        source = Source("press_release", text, "")
        case = {"slug": "case-press-window", "state": "DRAFT", "entities": []}
        stub = _sequenced_stub(_response(locations=[
            {"place_as_written": BANKE_PLACE, "district": "बाँके",
             "evidence": BANKE_EVIDENCE, "notes": ""}]))

        extraction = extract_from_source(_api(), _gaz(), case, source, stub, usage=None)

        assert len(stub.calls) == 1
        assert stub.calls[0]["system"] == ere.PRESS_RELEASE_SYSTEM_PROMPT
        assert any("/location/district/" in b.nes_id for b in extraction.location_binds)
        assert extraction.windows == [(0, len(text))]


# --------------------------------------------------------------------------
# Fix round 1 (post-review) -- six items, one test class per item.
# --------------------------------------------------------------------------


class TestPickSourceFetchFailure:
    """Review item 1: a court-order fetch failure must refuse, never fall back."""

    def test_a_court_order_fetch_failure_refuses_rather_than_falling_back(self, monkeypatch):
        import casework.common.materials as m

        fetched = []

        def fake_fetch(link, timeout=60):
            fetched.append(link)
            if link == "https://x/order.md":
                raise TimeoutError("503 Service Unavailable")
            return READABLE_PRESS

        monkeypatch.setattr(m, "fetch_markdown", fake_fetch)
        detail = {"slug": "case-order-503", "state": "DRAFT", "evidence": [
            _material("https://jawafdehi.org/material/ngm/court_orders/1", "court_order",
                      "https://x/order.md"),
            _material("https://jawafdehi.org/material/ciaa/press_releases/1", "press_release",
                      "https://x/press.md"),
        ]}

        source = pick_source(detail)

        assert source.kind is None
        assert "MARKDOWN fetch failed" in source.reason
        assert "503" in source.reason
        assert fetched == ["https://x/order.md"], "the press release must never be fetched"

    def test_a_missing_court_order_material_still_falls_back_normally(self, monkeypatch):
        # The other half: a case with NO court-order material at all (as opposed to one
        # that failed to fetch) must still fall back to a readable press release.
        import casework.common.materials as m

        monkeypatch.setattr(m, "fetch_markdown", lambda link, timeout=60: READABLE_PRESS)
        detail = {"slug": "case-no-order", "state": "DRAFT", "evidence": [
            _material("https://jawafdehi.org/material/ciaa/press_releases/1", "press_release",
                      "https://x/press.md"),
        ]}

        source = pick_source(detail)

        assert source.kind == "press_release"


class TestParseWindowResponseMalformed:
    """Review item 2: the malformed-reply recovery must not borrow a neighbour's array."""

    def test_a_truncated_reply_recovers_each_key_from_its_own_array_not_a_neighbours(self):
        full = _response(
            locations=[{"place_as_written": BANKE_PLACE, "district": "बाँके",
                       "evidence": BANKE_EVIDENCE, "notes": ""}],
            entities=[{"entity_name": "संस्था", "relationship_type": "related",
                     "evidence": "संस्था संलग्न रहेको पाइयो।", "notes": "क"}],
            accused_notes=[{"name": "राम", "notes": "अधिकृत",
                          "evidence": "राम अधिकृत रहेका थिए।"}],
        )
        truncated = full[:full.index('"accused_notes"') + 20]
        assert not truncated.rstrip().endswith("}")  # the object never closes

        answer = parse_window_response(truncated)

        assert answer.locations == [{"place_as_written": BANKE_PLACE, "district": "बाँके",
                                    "evidence": BANKE_EVIDENCE, "notes": ""}]
        assert answer.entities == [{"entity_name": "संस्था", "relationship_type": "related",
                                   "evidence": "संस्था संलग्न रहेको पाइयो।", "notes": "क"}]
        assert answer.accused_notes == []

    def test_a_key_truncated_before_its_own_array_opens_is_reported_empty(self, caplog):
        full = _response(locations=[{"place_as_written": BANKE_PLACE, "district": "बाँके",
                                     "evidence": BANKE_EVIDENCE, "notes": ""}])
        truncated = full[:full.index('"accused_notes"')]

        with caplog.at_level(logging.WARNING, logger="casework.enrich_related_entities"):
            answer = parse_window_response(truncated)

        assert answer.accused_notes == []
        assert answer.locations  # the intact key is still recovered
        assert any("accused_notes" in r.message for r in caplog.records)


class TestExtractFromSourceNoteAccumulation:
    """Review item 3: a later window's reworded note must not erase the first."""

    def test_a_later_windows_reworded_note_does_not_erase_the_first(self):
        note1_evidence = "राम बहादुर तत्कालीन सचिव, कृषि मन्त्रालय रहेका थिए भनी उल्लेख छ।"
        text = CAPTION_SHORT + _filler(100_000 - len(CAPTION_SHORT))
        text = _splice(text, len(CAPTION_SHORT) + 100, note1_evidence)
        w2 = list(start_windows(text))[1]
        note2_evidence = "राम बहादुर सचिव, कृषि मन्त्रालय रहेका थिए भनी पुनः उल्लेख छ।"
        text = _splice(text, w2.start + 50, note2_evidence)
        text = _splice(text, w2.start + 300, BANKE_EVIDENCE)
        source = Source("court_order", text, "")
        case = {"slug": "case-reword", "state": "DRAFT", "entities": [
            {"nes_id": "https://jawafdehi.org/entity/person/ram", "type": "accused",
             "display_name": "राम बहादुर", "notes": ere.MACHINE_NOTE_PREFIX + "079-cr-0002"},
        ]}
        stub = _sequenced_stub(
            _response(accused_notes=[{"name": "राम बहादुर",
                                     "notes": "तत्कालीन सचिव, कृषि मन्त्रालय",
                                     "evidence": note1_evidence}]),
            _response(locations=[{"place_as_written": BANKE_PLACE, "district": "बाँके",
                                  "evidence": BANKE_EVIDENCE, "notes": ""}],
                      accused_notes=[{"name": "राम बहादुर", "notes": "सचिव, कृषि मन्त्रालय",
                                    "evidence": note2_evidence}]),
        )

        extraction = extract_from_source(_api(), _gaz(), case, source, stub, usage=None)

        assert len(stub.calls) == 2
        assert extraction.accused_notes == [
            {"name": "राम बहादुर", "notes": "तत्कालीन सचिव, कृषि मन्त्रालय",
             "evidence": note1_evidence}]


class TestAccusedMissingNotesRomanized:
    """Review item 4: a romanized-only accused bind can never be filled from a
    Devanagari order, so it must not hold the start-window loop open."""

    def test_ignores_a_romanized_display_name(self):
        case = {"slug": "case-roman", "state": "DRAFT", "entities": [
            {"nes_id": "https://jawafdehi.org/entity/person/roman", "type": "accused",
             "display_name": "Ram Bahadur",
             "notes": ere.MACHINE_NOTE_PREFIX + "079-cr-0004"},
        ]}
        assert accused_missing_notes(case, []) == set()

    def test_a_romanized_only_accused_bind_does_not_hold_the_loop_open(self):
        text = _order_with_location_at(100_000, len(CAPTION_SHORT) + 100)
        source = Source("court_order", text, "")
        case = {"slug": "case-romanized-loop", "state": "DRAFT", "entities": [
            {"nes_id": "https://jawafdehi.org/entity/person/roman2", "type": "accused",
             "display_name": "Ram Bahadur",
             "notes": ere.MACHINE_NOTE_PREFIX + "079-cr-0005"},
        ]}
        stub = _sequenced_stub(_response(locations=[
            {"place_as_written": BANKE_PLACE, "district": "बाँके",
             "evidence": BANKE_EVIDENCE, "notes": ""}]))

        extraction = extract_from_source(_api(), _gaz(), case, source, stub, usage=None)

        assert len(stub.calls) == 1
        assert any("/location/district/" in b.nes_id for b in extraction.location_binds)


class TestExtractFromSourceNoSource:
    """Controller ruling (item 5): `source.kind is None` makes no call at all."""

    def test_a_none_source_returns_an_empty_extraction_with_no_call(self):
        case = {"slug": "case-no-source", "state": "DRAFT", "entities": []}
        stub = _sequenced_stub()

        extraction = extract_from_source(_api(), _gaz(), case, Source(None, "", "no source"),
                                        stub, usage=None)

        assert extraction == Extraction([], [], [], [], [])
        assert stub.calls == []


class TestPressReleaseLabelAndPromptDuplicate:
    """Controller ruling (item 6): the press-release call is labelled as itself, and
    `COURT_ORDER_SYSTEM_PROMPT`'s duplicated sentence is gone."""

    def test_the_press_release_call_is_labelled_as_a_press_release(self):
        text = BANKE_EVIDENCE + " " + FILLER_SENTENCE * 20
        source = Source("press_release", text, "")
        case = {"slug": "case-press-label", "state": "DRAFT", "entities": []}
        stub = _sequenced_stub(_response())

        extract_from_source(_api(), _gaz(), case, source, stub, usage=None)

        content = stub.calls[0]["content"]
        assert content.startswith(f"प्रेस विज्ञप्ति, अक्षर 0–{len(text):,} (कुल {len(text):,})")
        assert "अदालतको आदेश" not in content.split("\n\n", 1)[0]

    def test_the_labelled_sentence_appears_only_once_and_stays_in_part_1(self):
        prompt = ere.COURT_ORDER_SYSTEM_PROMPT
        marker = "You are reading part of a Special Court judgment, labelled with its character range."
        assert prompt.count(marker) == 1
        assert marker in prompt[prompt.index("PART 1"):]


# --------------------------------------------------------------------------
# Task 7 -- `main()` reads through `pick_source` / `extract_from_source`, skips
# only a case with a `related` bind AND a district, and reports every case that
# ends without a district.
# --------------------------------------------------------------------------

BANKE_IRI = "https://jawafdehi.org/entity/location/district/banke-np0557"
KHAJURA_IRI = "https://jawafdehi.org/entity/location/localunit/khajura-gaunpalika-50001"
SAJHA_EVIDENCE = "साझा भण्डार सहकारीले ठेक्का प्राप्त गरेको थियो।"
KHAJURA_EVIDENCE = "खजुरा गाउँपालिकाको बजेटबाट भुक्तानी गरिएको थियो।"
WIRED_ORDER = "\n".join([CAPTION_SHORT, SAJHA_EVIDENCE, BANKE_EVIDENCE, KHAJURA_EVIDENCE,
                         FILLER_SENTENCE * 20])
WIRED_LINKS = {"https://x/wired-court.md": WIRED_ORDER,
               "https://x/wired-press.md": READABLE_PRESS}
SAJHA_ITEM = {"entity_name": "साझा भण्डार सहकारी", "relationship_type": "related",
              "evidence": SAJHA_EVIDENCE, "notes": "ठेक्का प्राप्त गर्ने संस्था"}
BANKE_LOCATION = {"place_as_written": BANKE_PLACE, "district": "बाँके",
                  "evidence": BANKE_EVIDENCE, "notes": "रकम लगानी भएको स्थान"}
SAJHA_SEARCH = {"साझा भण्डार सहकारी": [{"id": SAJHA_IRI, "title": {"ne": "साझा भण्डार सहकारी"},
                                       "score": 200.0}]}
DISTRICT_BIND = {"nes_id": BANKE_IRI, "type": "location", "notes": ""}
SAJHA_RELATED_BIND = {"nes_id": SAJHA_IRI, "type": "related", "notes": "ठेक्का प्राप्त गर्ने संस्था"}


def _patch_links(monkeypatch, links=None):
    """Serve `links` from `fetch_markdown` and record every link fetched."""
    import casework.common.materials as m

    fetched = []
    links = WIRED_LINKS if links is None else links

    def fake_fetch(link, timeout=60):
        fetched.append(link)
        return links.get(link, "")

    monkeypatch.setattr(m, "fetch_markdown", fake_fetch)
    return fetched


def _wired_case(slug="case-wired", entities=(), court=True, press=True):
    evidence = []
    if court:
        evidence.append(_material("https://jawafdehi.org/material/ngm/court_orders/7",
                                  "court_order", "https://x/wired-court.md"))
    if press:
        evidence.append(_material("https://jawafdehi.org/material/ciaa/press_releases/7",
                                  "press_release", "https://x/wired-press.md"))
    return {"slug": slug, "title": "जोडिएको मुद्दा", "state": "DRAFT",
            "evidence": evidence, "entities": list(entities)}


def _jsonl(key):
    return [json.loads(line) for line in
            Path(_report_files()[key]).read_text(encoding="utf-8").splitlines()]


class TestAlreadyEnriched:
    """The skip rule: a `related` bind AND a coded-district bind, nothing less."""

    def test_a_related_bind_alone_is_not_enough(self):
        assert not ere.already_enriched({"entities": [SAJHA_RELATED_BIND]})

    def test_a_district_bind_alone_is_not_enough(self):
        assert not ere.already_enriched({"entities": [DISTRICT_BIND]})

    def test_a_municipality_is_not_a_district(self):
        municipality = {"nes_id": KHAJURA_IRI, "type": "location", "notes": ""}
        assert not ere.already_enriched({"entities": [SAJHA_RELATED_BIND, municipality]})

    def test_both_is_already_enriched_in_either_key_shape(self):
        write_shape = {"nes_id": SAJHA_IRI, "relationship_type": "related", "notes": ""}
        assert ere.already_enriched({"entities": [SAJHA_RELATED_BIND, DISTRICT_BIND]})
        assert ere.already_enriched({"entities": [write_shape, DISTRICT_BIND]})


class TestMainWiring:
    def test_a_case_with_a_related_bind_but_no_district_is_not_skipped(self, monkeypatch):
        _patch_links(monkeypatch)
        case = _wired_case(entities=[SAJHA_RELATED_BIND])
        stub = _call_tracking_stub(_response(locations=[BANKE_LOCATION]))
        report = _run_main(monkeypatch, _SearchStubApi([case], SAJHA_SEARCH), stub,
                           argv=["--dry-run"])

        assert len(stub.calls) == 1
        assert "already present" not in report.rows[0]["reason"]

    def test_a_case_with_a_related_bind_and_a_district_is_skipped(self, monkeypatch):
        fetched = _patch_links(monkeypatch)
        case = _wired_case(entities=[SAJHA_RELATED_BIND, DISTRICT_BIND])
        stub = _call_tracking_stub(_response(locations=[BANKE_LOCATION]))
        loader = _stub_gazetteer_loader()
        report = _run_main(monkeypatch, _SearchStubApi([case], SAJHA_SEARCH), stub,
                           argv=["--dry-run"], gazetteer_loader=loader)

        assert stub.calls == []
        assert fetched == []
        assert loader.calls == []
        assert report.rows[0]["status"] == "already"
        assert "already present" in report.rows[0]["reason"]

    def test_a_court_order_case_with_no_district_writes_location_missing_and_still_binds(
        self, monkeypatch, capsys
    ):
        _patch_links(monkeypatch)
        case = _wired_case(slug="case-no-district", press=False)
        stub = _call_tracking_stub(_response(
            locations=[{**BANKE_LOCATION, "evidence": "यो वाक्य आदेशमा कतै छैन, कतै पनि।"}],
            entities=[SAJHA_ITEM]))
        report = _run_main(monkeypatch, _SearchStubApi([case], SAJHA_SEARCH), stub,
                           argv=["--dry-run"])
        out = capsys.readouterr().out

        missing = _jsonl("location_missing")
        assert len(missing) == 1
        row = missing[0]
        assert (row["slug"], row["source"]) == ("case-no-district", "court_order")
        assert row["windows"] == [[0, len(WIRED_ORDER)]]
        assert [r["reason"] for r in row["rejected_locations"]] == [
            "evidence not found in the source"]
        assert "location_missing: 1" in out
        assert [(b["extracted"], b["role"]) for b in _jsonl("binds")] == [
            ("साझा भण्डार सहकारी", "related")]
        assert [r["slug"] for r in report.rows] == ["case-no-district"]

    def test_a_case_already_holding_a_district_is_not_location_missing(
        self, monkeypatch, capsys
    ):
        _patch_links(monkeypatch)
        case = _wired_case(slug="case-has-district", entities=[DISTRICT_BIND], press=False)
        stub = _call_tracking_stub(_response(entities=[SAJHA_ITEM]))
        _run_main(monkeypatch, _SearchStubApi([case], SAJHA_SEARCH), stub, argv=["--dry-run"])

        assert _jsonl("location_missing") == []
        assert "location_missing: 0" in capsys.readouterr().out

    def test_a_readable_court_order_means_the_press_release_is_never_fetched(
        self, monkeypatch
    ):
        fetched = _patch_links(monkeypatch)
        stub = _call_tracking_stub(_response(locations=[BANKE_LOCATION]))
        _run_main(monkeypatch, _SearchStubApi([_wired_case()], SAJHA_SEARCH), stub,
                  argv=["--dry-run"])

        assert len(stub.calls) == 1
        assert "https://x/wired-press.md" not in fetched
        assert "https://x/wired-court.md" in fetched

    def test_a_dry_run_never_calls_replace_list(self, monkeypatch, capsys):
        _patch_links(monkeypatch)
        api = _SearchStubApi([_wired_case()], SAJHA_SEARCH)
        stub = _call_tracking_stub(_response(locations=[BANKE_LOCATION], entities=[SAJHA_ITEM]))
        _run_main(monkeypatch, api, stub, argv=["--dry-run"])
        out = capsys.readouterr().out

        assert api.replace_list_calls == []
        assert api.patch_calls == []
        assert f"WOULD BIND (location) {BANKE_PLACE}  ->  {BANKE_IRI}" in out
        assert "WOULD BIND (related) साझा भण्डार सहकारी" in out

    def test_apply_writes_the_district_and_the_related_bind_in_one_replace(self, monkeypatch):
        _patch_links(monkeypatch)
        api = _SearchStubApi([_wired_case()], SAJHA_SEARCH)
        stub = _call_tracking_stub(_response(locations=[BANKE_LOCATION], entities=[SAJHA_ITEM]))
        _run_main(monkeypatch, api, stub, argv=["--apply"])

        assert len(api.replace_list_calls) == 1
        items = api.replace_list_calls[0][2]
        assert [(i["nes_id"], i["relationship_type"]) for i in items] == [
            (BANKE_IRI, "location"), (KHAJURA_IRI, "location"), (SAJHA_IRI, "related")]

    def test_one_entity_bound_in_two_sections_is_labelled_per_section(
        self, monkeypatch, capsys
    ):
        # The municipality binds as a location through the gazetteer, and the
        # model also names it as the related body whose budget paid out.
        _patch_links(monkeypatch)
        khajura_related = {"entity_name": "खजुरा गाउँपालिका", "relationship_type": "related",
                           "evidence": KHAJURA_EVIDENCE, "notes": "भुक्तानी गर्ने निकाय"}
        search = {"खजुरा गाउँपालिका": [{"id": KHAJURA_IRI, "title": {"ne": "खजुरा गाउँपालिका"},
                                      "score": 200.0}]}
        api = _SearchStubApi([_wired_case()], search)
        stub = _call_tracking_stub(
            _response(locations=[BANKE_LOCATION], entities=[khajura_related]))
        _run_main(monkeypatch, api, stub, argv=["--apply"])
        out = capsys.readouterr().out

        rows = [(b["nes_id"], b["role"]) for b in _jsonl("binds")]
        assert (KHAJURA_IRI, "location") in rows
        assert (KHAJURA_IRI, "related") in rows
        assert f"BOUND (location) {BANKE_PLACE}  ->  {KHAJURA_IRI}" in out
        assert f"BOUND (related) खजुरा गाउँपालिका  ->  {KHAJURA_IRI}" in out

    def test_the_gazetteer_is_loaded_once_for_the_run(self, monkeypatch):
        _patch_links(monkeypatch)
        cases = [_wired_case(slug="case-a"), _wired_case(slug="case-b")]
        loader = _stub_gazetteer_loader()
        stub = _call_tracking_stub(_response(locations=[BANKE_LOCATION]))
        _run_main(monkeypatch, _SearchStubApi(cases, SAJHA_SEARCH), stub,
                  argv=["--dry-run"], gazetteer_loader=loader)

        assert len(stub.calls) == 2
        assert len(loader.calls) == 1

    def test_a_gazetteer_load_failure_stops_the_run(self, monkeypatch):
        _patch_links(monkeypatch)

        class _PagedSearchApi(_SearchStubApi):
            def get(self, path, params=None, timeout=60):
                rows = {"location/district": DISTRICTS,
                        "location/localunit": UNITS}[params["entity_prefix"]]
                o, n = params["offset"], params["limit"]
                return {"entities": rows[o:o + n], "total": len(rows)}

        stub = _call_tracking_stub(_response(locations=[BANKE_LOCATION]))
        api = _PagedSearchApi([_wired_case(slug="case-a"), _wired_case(slug="case-b")])
        with pytest.raises(RuntimeError, match="expected 77 NES districts"):
            _run_main(monkeypatch, api, stub, argv=["--dry-run"], real_gazetteer=True)
        assert stub.calls == []

    def test_the_extract_event_records_the_windows_read(self, monkeypatch):
        _patch_links(monkeypatch)
        stub = _call_tracking_stub(_response(locations=[BANKE_LOCATION]))
        _run_main(monkeypatch, _SearchStubApi([_wired_case()], SAJHA_SEARCH), stub,
                  argv=["--dry-run"])

        extract = [r for r in _read_events(_events_path()) if r["step"] == "extract"]
        assert len(extract) == 1
        assert f"windows [(0, {len(WIRED_ORDER)})]" in extract[0]["detail"]

    def test_an_answer_that_fails_grounding_lands_in_dropped_jsonl(self, monkeypatch):
        _patch_links(monkeypatch)
        unfounded = {**SAJHA_ITEM, "evidence": "यो वाक्य आदेशमा कतै पनि लेखिएको छैन।"}
        stub = _call_tracking_stub(_response(locations=[BANKE_LOCATION], entities=[unfounded]))
        _run_main(monkeypatch, _SearchStubApi([_wired_case()], SAJHA_SEARCH), stub,
                  argv=["--dry-run"])

        dropped = _jsonl("dropped")
        assert [(r["slug"], r["entity_name"], r["reason"]) for r in dropped] == [
            ("case-wired", "साझा भण्डार सहकारी", "evidence not found in the source")]
        assert _jsonl("extracted") == []

    def test_a_window_over_the_hard_max_is_refused_before_the_call(self, monkeypatch):
        long_press = READABLE_PRESS * ((ere.PROMPT_HARD_MAX // len(READABLE_PRESS)) + 1)
        _patch_links(monkeypatch, {"https://x/wired-press.md": long_press})
        stub = _call_tracking_stub(_response())
        report = _run_main(monkeypatch, _SearchStubApi([_wired_case(court=False)]), stub,
                           argv=["--dry-run"])

        assert stub.calls == []
        assert report.rows[0]["status"] == "error"
        assert "PROMPT_HARD_MAX" in report.rows[0]["reason"]


def test_prompt_hard_max_fits_a_whole_start_window():
    assert ere.PROMPT_HARD_MAX == 40_000


def test_report_paths_carries_dropped_and_location_missing():
    out = ere.report_paths({"log": "/tmp/20260925T000000Z-entities-abc.log"})
    assert out["dropped"] == "/tmp/20260925T000000Z-entities-abc.dropped.jsonl"
    assert out["location_missing"] == "/tmp/20260925T000000Z-entities-abc.location_missing.jsonl"


class TestEntitiesMergeAcrossWindows:
    """Spec §3: one `(normalised name, section)` per case, first non-empty note wins."""

    def test_a_name_repeated_in_a_later_window_is_one_item_with_the_first_real_note(self):
        text = _order_with_location_at(100_000, 45_000)
        first = "यो अदालतको आदेशको विवरण हो।"
        source = Source("court_order", text, "")
        case = {"slug": "case-merge", "state": "DRAFT", "entities": []}
        bare = {"entity_name": "साझा भण्डार सहकारी", "relationship_type": "related",
                "evidence": first, "notes": ""}
        stub = _sequenced_stub(
            _response(entities=[bare]),
            _response(entities=[{**bare, "entity_name": "साझा  भण्डार सहकारी",
                                 "evidence": BANKE_EVIDENCE, "notes": "ठेक्का पाउने"}],
                      locations=[BANKE_LOCATION]),
        )

        extraction = extract_from_source(_api(), _gaz(), case, source, stub, usage=None)

        assert len(stub.calls) == 2
        assert len(extraction.entities) == 1
        merged = extraction.entities[0]
        assert merged["entity_name"] == "साझा भण्डार सहकारी"
        assert merged["notes"] == "ठेक्का पाउने"
        assert merged["evidence_quotes"] == [first, BANKE_EVIDENCE]

    def test_the_same_name_in_two_sections_stays_two_items(self):
        text = WIRED_ORDER
        case = {"slug": "case-two-sections", "state": "DRAFT", "entities": []}
        stub = _sequenced_stub(_response(entities=[
            SAJHA_ITEM, {**SAJHA_ITEM, "relationship_type": "alleged"}]))

        extraction = extract_from_source(_api(), _gaz(), case, Source("court_order", text, ""),
                                         stub, usage=None)

        assert [e["relationship_type"] for e in extraction.entities] == ["related", "alleged"]

    def test_main_searches_a_repeated_name_once(self, monkeypatch):
        _patch_links(monkeypatch)
        api = _SearchStubApi([_wired_case()], SAJHA_SEARCH)
        stub = _call_tracking_stub(_response(locations=[BANKE_LOCATION],
                                             entities=[SAJHA_ITEM, SAJHA_ITEM]))
        _run_main(monkeypatch, api, stub, argv=["--dry-run"])

        assert api.search_calls == ["साझा भण्डार सहकारी"]
        assert [r["extracted"] for r in _jsonl("extracted")] == ["साझा भण्डार सहकारी"]
