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

`TestDonorFidelity` re-derives every slicing constant, the system prompt, and
the `tier`/`max_tokens` LLM-call arguments directly from the donor at commit
`0321a85` (via `git show` + `ast`, never by trusting this file's own
transcription).
"""
import ast
import json
import logging
import subprocess
import sys
import types
from pathlib import Path

import pytest

from casework import enrich_related_entities as ere
from casework.common.api import ENTITY_SEARCH_MAX_PAGES, ENTITY_SEARCH_PAGE_SIZE
from casework.enrich_related_entities import (
    _build_content_parts,
    _enforce_prompt_budget,
    _parse_extraction_response,
    _truncate_court_order,
    _truncate_press_release,
    current_entity_binds,
    merge_entity_binds,
    plan_case_entities,
    validate_bind_item,
)

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


def _literal_from_value_node(value_node):
    """Return the literal a donor constant assignment resolves to: either the
    node itself (a plain literal) or, for `env_int("NAME", default)` calls,
    the literal `default` (second positional arg)."""
    if isinstance(value_node, ast.Call):
        return ast.literal_eval(value_node.args[1])
    return ast.literal_eval(value_node)


def _donor_constants() -> dict:
    """Extract top-level constant assignments from the donor source via AST
    (never `exec`/`import` it -- the donor's own imports no longer resolve
    against the refactored `casework.common` package)."""
    wanted = {
        "SYSTEM_PROMPT",
        "COURT_ORDER_FULL_THRESHOLD",
        "COURT_ORDER_HEAD_CHARS",
        "COURT_ORDER_TAIL_CHARS",
        "COURT_ORDER_THAHAR_CHARS",
        "PRESS_RELEASE_CHARS",
        "PRESS_RELEASE_CHARS_NO_COURT",
        "PROMPT_HARD_MAX",
    }
    tree = ast.parse(_donor_source())
    found = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and target.id in wanted:
                found[target.id] = _literal_from_value_node(node.value)
    return found


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


@pytest.fixture(scope="module")
def donor():
    return _donor_constants()


class TestDonorFidelity:
    """Byte-for-byte pins against the donor at commit 0321a85 -- NOT this
    module's own transcription. A drifted prompt or truncation constant is
    the highest-consequence silent failure available in these files: it
    changes LLM behavior/prompt budgeting with zero other test failures."""

    def test_system_prompt_matches_donor(self, donor):
        assert ere.SYSTEM_PROMPT == donor["SYSTEM_PROMPT"]

    def test_court_order_full_threshold_matches_donor(self, donor):
        assert ere.COURT_ORDER_FULL_THRESHOLD == donor["COURT_ORDER_FULL_THRESHOLD"]

    def test_court_order_head_chars_matches_donor(self, donor):
        assert ere.COURT_ORDER_HEAD_CHARS == donor["COURT_ORDER_HEAD_CHARS"]

    def test_court_order_tail_chars_matches_donor(self, donor):
        assert ere.COURT_ORDER_TAIL_CHARS == donor["COURT_ORDER_TAIL_CHARS"]

    def test_court_order_thahar_chars_matches_donor(self, donor):
        assert ere.COURT_ORDER_THAHAR_CHARS == donor["COURT_ORDER_THAHAR_CHARS"]

    def test_press_release_chars_matches_donor(self, donor):
        assert ere.PRESS_RELEASE_CHARS == donor["PRESS_RELEASE_CHARS"]

    def test_press_release_chars_no_court_matches_donor(self, donor):
        assert ere.PRESS_RELEASE_CHARS_NO_COURT == donor["PRESS_RELEASE_CHARS_NO_COURT"]

    def test_prompt_hard_max_matches_donor(self, donor):
        assert ere.PROMPT_HARD_MAX == donor["PROMPT_HARD_MAX"]

    def test_invoke_text_tier_and_max_tokens_match_donor(self):
        # Pins donor line ~421 `tier="premium"` and line ~420 `max_tokens=2000`.
        donor_kwargs = _donor_invoke_text_kwargs()
        assert donor_kwargs["tier"] == "premium"
        assert donor_kwargs["max_tokens"] == 2000
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

    def test_donor_has_no_create_entity_method_on_current_api(self):
        # CaseworkApi (this branch) genuinely has no create_entity method --
        # confirms the donor's write path is not just discouraged but
        # structurally impossible to call as written.
        assert not hasattr(ere.CaseworkApi, "create_entity")


# --------------------------------------------------------------------------
# _truncate_press_release
# --------------------------------------------------------------------------


class TestTruncatePressRelease:
    def test_short_text_is_not_truncated(self):
        text = "छोटो पाठ।"
        assert _truncate_press_release(text, limit=100) == text

    def test_none_text_passthrough(self):
        assert _truncate_press_release(None, limit=100) is None

    def test_empty_text_passthrough(self):
        assert _truncate_press_release("", limit=100) == ""

    def test_default_limit_is_press_release_chars(self):
        text = "अ" * (ere.PRESS_RELEASE_CHARS + 500)
        # No sentence separators at all -- falls through to the raw chunk,
        # which proves the default limit (no explicit `limit=`) is used.
        result = _truncate_press_release(text)
        assert len(result) == ere.PRESS_RELEASE_CHARS

    def test_cuts_at_last_danda_before_limit(self):
        # Two dandas: one just past the halfway point, one right at the cut.
        head = "पहिलो वाक्य।" + "भ" * 40 + "।"
        tail = "अ" * 40
        text = head + tail
        limit = len(head) + 5
        result = _truncate_press_release(text, limit=limit)
        assert result == head

    def test_falls_back_to_raw_chunk_when_no_separator_in_second_half(self):
        # A single danda sits in the FIRST half of the chunk (before
        # limit // 2) -- must not be used as the cut point.
        text = "क।" + ("अ" * 100)
        limit = 20
        result = _truncate_press_release(text, limit=limit)
        assert result == text[:limit]
        assert len(result) == limit

    def test_long_text_over_limit_is_shortened(self):
        text = "स" * 50
        result = _truncate_press_release(text, limit=10)
        assert len(result) <= 10


# --------------------------------------------------------------------------
# _truncate_court_order
# --------------------------------------------------------------------------


class TestTruncateCourtOrder:
    def test_none_text_passthrough(self):
        assert _truncate_court_order(None) is None

    def test_text_under_threshold_is_unchanged(self):
        text = "छोटो आदेश।" * 5
        assert len(text) < ere.COURT_ORDER_FULL_THRESHOLD
        assert _truncate_court_order(text) == text

    def test_thahar_khanda_extracted_when_present_and_short(self):
        head = "क" * ere.COURT_ORDER_FULL_THRESHOLD
        thahar = "ठहर खण्ड\nयो ठहर खण्डको सामग्री हो।"
        text = head + thahar
        result = _truncate_court_order(text)
        assert "ठहर खण्ड (verdict section)" in result
        assert thahar in result
        assert head not in result

    def test_thahar_khanda_truncated_at_sentence_boundary_when_long(self):
        head = "क" * ere.COURT_ORDER_FULL_THRESHOLD
        long_thahar_body = "वाक्य एक। " + ("ख" * (ere.COURT_ORDER_THAHAR_CHARS + 500))
        text = head + "ठहर खण्ड" + long_thahar_body
        result = _truncate_court_order(text)
        assert "[...ठहर खण्ड (verdict section)...]" in result
        # Truncated content must not exceed the thahar budget.
        after_label = result.split("...]\n\n", 1)[1]
        assert len(after_label) <= ere.COURT_ORDER_THAHAR_CHARS

    def test_head_tail_fallback_when_no_thahar_khanda(self):
        text = "श" * (ere.COURT_ORDER_FULL_THRESHOLD + 1000)
        result = _truncate_court_order(text)
        assert "[...court order header section...]" in result
        assert "[...court order verdict section...]" in result
        assert text[:ere.COURT_ORDER_HEAD_CHARS] in result
        assert text[-ere.COURT_ORDER_TAIL_CHARS:] in result


# --------------------------------------------------------------------------
# _enforce_prompt_budget
# --------------------------------------------------------------------------


class TestEnforcePromptBudget:
    def test_within_budget_returns_joined_parts_unchanged(self):
        parts = ["--- PRESS RELEASE ---", "छोटो पाठ।"]
        result = _enforce_prompt_budget(list(parts))
        assert result == "\n\n".join(parts)

    def test_over_budget_truncates_largest_part(self):
        small = "--- COURT ORDER ---"
        large = "अ" * (ere.PROMPT_HARD_MAX + 5000)
        parts = [small, large]
        result = _enforce_prompt_budget(list(parts))
        assert len(result) <= ere.PROMPT_HARD_MAX
        assert small in result

    def test_over_budget_result_capped_at_hard_max(self):
        parts = ["अ" * ere.PROMPT_HARD_MAX, "आ" * ere.PROMPT_HARD_MAX]
        result = _enforce_prompt_budget(list(parts))
        assert len(result) <= ere.PROMPT_HARD_MAX

    def test_over_budget_still_fills_the_budget_it_is_given(self):
        # A LOWER bound, deliberately. Every other assertion here is
        # `len(result) <= PROMPT_HARD_MAX`, which a function returning ""
        # satisfies perfectly -- over-truncation is invisible to them. That is
        # this branch's signature failure mode (code silently doing LESS than
        # asked) wearing a test's clothes: the budget guard exists to fit as
        # much source text as possible under the cap, so a guard that returns
        # nothing has failed at its actual job while passing every check.
        #
        # Not reachable in today's implementation -- the final
        # `combined[:PROMPT_HARD_MAX]` hard-slice always preserves content.
        # This pins that property so a future refactor of the truncation
        # arithmetic cannot quietly drop it.
        parts = ["अ" * ere.PROMPT_HARD_MAX, "आ" * ere.PROMPT_HARD_MAX]
        result = _enforce_prompt_budget(list(parts))
        assert len(result) == ere.PROMPT_HARD_MAX, (
            "input is 2x the budget, so the result should fill it exactly; "
            "a short or empty return means the guard over-truncated")


# --------------------------------------------------------------------------
# _build_content_parts -- press-only / court-only / both / neither matrix
# --------------------------------------------------------------------------


class TestBuildContentPartsMatrix:
    def test_neither_source_yields_empty_parts(self):
        assert _build_content_parts(None, None) == []

    def test_press_only_uses_no_court_limit(self):
        # Longer than PRESS_RELEASE_CHARS but shorter than the NO_COURT
        # limit -- must survive intact only when treated as press-only.
        text = "प्रेस विज्ञप्ति। " * 200
        assert ere.PRESS_RELEASE_CHARS < len(text) < ere.PRESS_RELEASE_CHARS_NO_COURT
        parts = _build_content_parts(text, None)
        assert parts[0] == "--- PRESS RELEASE ---"
        assert parts[1] == text  # untouched: under the NO_COURT limit
        assert "COURT ORDER" not in "\n".join(parts)

    def test_court_only_yields_only_court_section(self):
        court_text = "अदालतको आदेश।" * 5
        parts = _build_content_parts(None, court_text)
        assert parts == ["--- COURT ORDER ---", court_text]

    def test_both_present_press_uses_the_smaller_limit(self):
        # Same press text as the press-only case, but WITH a court order
        # present -- must now be capped at the smaller PRESS_RELEASE_CHARS,
        # not the NO_COURT limit.
        press_text = "प्रेस विज्ञप्ति। " * 200
        court_text = "अदालतको आदेश।"
        parts = _build_content_parts(press_text, court_text)
        assert parts[0] == "--- PRESS RELEASE ---"
        assert len(parts[1]) <= ere.PRESS_RELEASE_CHARS
        assert parts[1] != press_text  # was truncated
        assert parts[2] == "--- COURT ORDER ---"
        assert parts[3] == court_text

    def test_press_and_court_order_is_press_section_first(self):
        parts = _build_content_parts("प्रेस।", "आदेश।")
        assert parts[0] == "--- PRESS RELEASE ---"
        assert parts[2] == "--- COURT ORDER ---"


# --------------------------------------------------------------------------
# _parse_extraction_response
# --------------------------------------------------------------------------


class TestParseExtractionResponse:
    def test_parses_both_entities_and_accused_notes(self):
        body = json.dumps({
            "entities": [{"entity_name": "क", "relationship_type": "related", "notes": "n"}],
            "accused_notes": [{"name": "ख", "notes": "पद"}],
        })
        entities, notes = _parse_extraction_response(body)
        assert entities == [{"entity_name": "क", "relationship_type": "related", "notes": "n"}]
        assert notes == [{"name": "ख", "notes": "पद"}]

    def test_entities_only_response_leaks_into_accused_notes_via_shared_fallback(self):
        # KNOWN QUIRK of the shared `parse_extraction_response` (see
        # tests/casework/test_parse.py::
        # test_parse_extraction_response_returns_none_when_key_absent): when
        # the requested wrapper key is absent, it falls through to a bare
        # top-level-array scan and returns the FIRST array it finds -- so a
        # response with ONLY "entities" (no "accused_notes" key) has its
        # entities list echoed back as accused_notes too. This is the
        # donor's own two-call pattern against this same parser (donor
        # lines 233-234), not a defect introduced by this port. Downstream,
        # `main()`'s `valid_items` filter requires `entity_name` +
        # `relationship_type`, which accused-note dicts (`name`/`notes`)
        # never carry, so this leak is harmless in practice.
        body = json.dumps({
            "entities": [{"entity_name": "क", "relationship_type": "location", "notes": ""}],
        })
        entities, notes = _parse_extraction_response(body)
        assert len(entities) == 1
        assert notes == entities

    def test_accused_notes_only_response_leaks_into_entities_via_shared_fallback(self):
        # Mirror image of the above: requesting "entities" when only
        # "accused_notes" is present falls through to the same bare-array
        # scan and returns the accused_notes list as "entities" too.
        body = json.dumps({"accused_notes": [{"name": "ख", "notes": "पद"}]})
        entities, notes = _parse_extraction_response(body)
        assert len(notes) == 1
        assert entities == notes

    def test_neither_key_yields_two_empty_lists(self):
        entities, notes = _parse_extraction_response('{"other": "value"}')
        assert entities == []
        assert notes == []

    def test_unparseable_text_yields_two_empty_lists(self):
        entities, notes = _parse_extraction_response("not json at all")
        assert entities == []
        assert notes == []


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


class _FakeUsage:
    def __init__(self):
        self.calls = 0

    def as_dict(self):
        return {"by_provider": []}


@pytest.fixture
def patched_fetch_markdown(monkeypatch):
    import casework.common.materials as m

    def fake_fetch(link, timeout=60):
        return {
            "https://x/press.md": "साझा भण्डार सहकारीमा अनियमितता भएको छ। "
                                   "गोपाल बहादुर श्रेष्ठविरुद्ध मुद्दा दर्ता भएको छ।",
            "https://x/court.md": "अदालतको आदेशमा ठहर खण्ड उल्लेख छ।",
            "https://x/press2.md": "प्रेस विज्ञप्तिको सामग्री।",
            "https://x/court2.md": "अदालतको आदेशको सामग्री।",
            "https://x/empty.md": "",
            "https://x/press5.md": "पहिल्यै भरिएको मुद्दाको प्रेस विज्ञप्ति।",
        }.get(link, "")

    monkeypatch.setattr(m, "fetch_markdown", fake_fetch)


def _run_main(monkeypatch, api, invoke_text_stub, argv):
    """Drive `main()` end to end with a stubbed API and a stubbed LLM call."""
    monkeypatch.setattr(ere, "build_api", lambda args: api)
    monkeypatch.setattr(ere, "bootstrap", lambda *a, **k: None)

    fake_llm_invoke = types.ModuleType("llm.invoke")
    fake_llm_invoke.invoke_text = invoke_text_stub

    fake_llm_usage = types.ModuleType("llm.usage")
    fake_llm_usage.UsageAccumulator = _FakeUsage
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


ENTITY_RESPONSE = json.dumps({
    "entities": [
        {"entity_name": "साझा भण्डार सहकारी", "relationship_type": "related",
         "notes": "ठेक्का प्राप्त गर्ने संस्था"},
        {"entity_name": "सुर्खेत जिल्ला", "relationship_type": "location", "notes": ""},
    ],
    "accused_notes": [
        {"name": "गोपाल बहादुर श्रेष्ठ", "notes": "तत्कालीन अध्यक्ष"},
    ],
})


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
    # that the case proceeds all the way to resolution (no search result is
    # configured, so the one 'related' extraction is a no-match and the one
    # 'location' extraction is a review -- neither is a new write, hence the
    # plan is a NOOP, but it is a NOOP reached AFTER the LLM ran, not the
    # pre-LLM skip -- `_StubApi` cannot stand in here any more because a
    # 'related' extraction now genuinely reaches `api.search_entities`).
    api = _SearchStubApi([PRESS_CASE_ALREADY_POPULATED])
    stub = _call_tracking_stub(ENTITY_RESPONSE)
    report = _run_main(
        monkeypatch, api, invoke_text_stub=stub, argv=["--force", "--dry-run"])
    assert len(stub.calls) == 1
    assert report.rows[0]["status"] == "already"
    assert report.rows[0]["reason"] == "1 for review, 1 no match"


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
         "type": "related", "notes": "ठेक्का प्राप्त गर्ने संस्था"}]

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
         "relationship_type": "related", "notes": "ठेक्का प्राप्त गर्ने संस्था"}]
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


def test_both_present_case_reaches_the_llm_with_both_sections(
    monkeypatch, patched_fetch_markdown
):
    seen = {}

    def stub(**kw):
        seen.update(kw)
        return ENTITY_RESPONSE

    api = _StubApi([BOTH_CASE])
    _run_main(monkeypatch, api, invoke_text_stub=stub, argv=["--dry-run"])
    assert "--- PRESS RELEASE ---" in seen["content"]
    assert "--- COURT ORDER ---" in seen["content"]


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
    assert "TOTAL entities that WOULD bind to cases (dry run, nothing written): 0" in out
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
    response = json.dumps({
        "entities": [
            {"entity_name": "साझा भण्डार सहकारी", "relationship_type": "related",
             "notes": "ठेक्का प्राप्त गर्ने संस्था"},
            {"entity_name": "अंकुर खत्री", "relationship_type": "related",
             "notes": "घुस लेनदेनमा सहयोग"},
        ],
        "accused_notes": [],
    })
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
    assert "TOTAL entities bound to cases: 1" in out
    assert "TOTAL already bound (nothing to write): 1" in out
    assert "TOTAL reported for human review: 0" in out
    assert "TOTAL with no NES match: 0" in out


def test_invalid_relationship_type_is_excluded_from_extracted_count(
    monkeypatch, patched_fetch_markdown, capsys
):
    # entity_name present but relationship_type is neither "location" nor
    # "related" (e.g. a stray "accused") -- donor's own filter
    # (`rel_type not in ("location", "related")`) drops it; this port's
    # `valid_items` must too.
    response = json.dumps({
        "entities": [
            {"entity_name": "गोपाल बहादुर", "relationship_type": "accused", "notes": "x"},
            {"entity_name": "सुर्खेत जिल्ला", "relationship_type": "location", "notes": ""},
        ],
        "accused_notes": [],
    })
    api = _StubApi([PRESS_ONLY_CASE])
    _run_main(monkeypatch, api, invoke_text_stub=lambda **kw: response,
              argv=["--dry-run"])
    out = capsys.readouterr().out
    assert "TOTAL entities extracted across all cases: 1" in out


def test_accused_notes_only_response_yields_zero_valid_entities_end_to_end(
    monkeypatch, patched_fetch_markdown, capsys
):
    # Exercises the shared-parser leak (see TestParseExtractionResponse) end
    # to end: with only accused_notes in the response, entities_data leaks
    # the same array, but main()'s valid_items filter (entity_name +
    # relationship_type required) rejects the leaked accused-note dicts, so
    # extraction still correctly reports 0 entities.
    response = json.dumps({"accused_notes": [{"name": "गोपाल", "notes": "अध्यक्ष"}]})
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
# 2. A `location`-typed extracted item must never be bound, regardless of what
#    `resolve()` would say -- it goes straight to `plan.review` before any
#    search happens, since binding scope is `related`-only by design.
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
    current = [{"nes_id": ANKUR_IRI, "relationship_type": "accused", "notes": "मूल"}]
    merged = merge_entity_binds(
        current, [{"nes_id": ANKUR_IRI, "relationship_type": "related", "notes": "नयाँ"}])
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
    frozen fixture documents look exactly like this and all 25 still BIND.
    """

    def __init__(self, cases, search_results=None, documents=None):
        super().__init__(cases)
        self._search = search_results or {}
        self._documents = documents or {}
        self.etag = 'W/"abc123"'
        self.search_calls = []
        self.get_entity_calls = []

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


def test_plan_buckets_review_and_nomatch_separately():
    anish_a = {"id": "https://jawafdehi.org/entity/person/anish-shrestha-219986",
               "title": {"ne": "अनिष श्रेष्‍ठ"}, "score": 182.17}
    anish_b = {"id": "https://jawafdehi.org/entity/person/anish-shrestha-285096",
               "title": {"ne": "अनिष श्रेष्ठ"}, "score": 182.17}
    case = {"slug": "case-z", "state": "DRAFT", "entities": []}
    api = _SearchStubApi([case], {"अनिष श्रेष्ठ": [anish_a, anish_b],
                                  "खगेन्द्र पराजुली": []})
    plan = plan_case_entities(api, case, 'W/"e"', [
        {"entity_name": "अनिष श्रेष्ठ", "relationship_type": "related", "notes": "क"},
        {"entity_name": "खगेन्द्र पराजुली", "relationship_type": "related", "notes": "ख"}])
    assert plan.action == "NOOP"
    assert len(plan.review) == 1
    assert len(plan.nomatch) == 1


# --- Correction 1: the document veto (Task 5's `apply_document_veto`) -----


def test_plan_downgrades_a_bind_to_review_when_the_document_is_an_election_record():
    # A real person, correctly named, with a search-payload score above
    # MIN_BIND_SCORE -- but the second read (the document) shows it is an
    # Election Commission candidate/ward-head record, not confirmed as the
    # case subject. `resolve()` alone would BIND this; the plan must not.
    case = {"slug": "case-elect", "state": "DRAFT", "entities": []}
    election_doc = {"identifier": [{"propertyID": "ecn-candidate-id", "value": "12345"}]}
    api = _SearchStubApi(
        [case], {"अंकुर खत्री": [ANKUR_CANDIDATE]}, documents={ANKUR_IRI: election_doc})
    plan = plan_case_entities(api, case, 'W/"e"', [
        {"entity_name": "अंकुर खत्री", "relationship_type": "related", "notes": "क"}])

    assert plan.action == "NOOP"
    assert plan.patch_items == []
    assert plan.bound == []
    assert len(plan.review) == 1
    name, decision = plan.review[0]
    assert name == "अंकुर खत्री"
    assert decision.nes_id is None
    assert "Election Commission" in decision.reason
    assert api.get_entity_calls == [ANKUR_IRI]


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
    name, decision = plan.review[0]
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


# --- Correction 2: a location-typed item never binds ---------------------


def test_plan_sends_a_location_item_straight_to_review_without_searching():
    # 7 of 33 extracted names in a live smoke run came back
    # relationship_type="location"; 9 of the 39 labelled bind targets are
    # location/* entities. The design scopes binding to related persons and
    # organisations only, so a location item must go to plan.review before
    # any search happens -- no wasted request.
    case = {"slug": "case-loc", "state": "DRAFT", "entities": []}
    api = _SearchStubApi([case])  # no search_results configured at all
    plan = plan_case_entities(api, case, 'W/"e"', [
        {"entity_name": "सुर्खेत जिल्ला", "relationship_type": "location", "notes": ""}])

    assert plan.action == "NOOP"
    assert plan.patch_items == []
    assert plan.bound == []
    assert len(plan.review) == 1
    name, decision = plan.review[0]
    assert name == "सुर्खेत जिल्ला"
    assert decision.nes_id is None
    assert "location" in decision.reason
    assert "scope" in decision.reason
    assert api.search_calls == []
    assert api.get_entity_calls == []


def test_plan_still_binds_a_related_item_alongside_a_skipped_location_item():
    # A mixed extraction: the location item is filtered out, the related item
    # still resolves and binds -- one out-of-scope item must not sink the rest
    # of the plan.
    case = {"slug": "case-mixed", "state": "DRAFT", "entities": []}
    api = _SearchStubApi([case], {"अंकुर खत्री": [ANKUR_CANDIDATE]})
    plan = plan_case_entities(api, case, 'W/"e"', [
        {"entity_name": "सुर्खेत जिल्ला", "relationship_type": "location", "notes": ""},
        {"entity_name": "अंकुर खत्री", "relationship_type": "related", "notes": "ख"}])

    assert plan.action == "WOULD_PATCH"
    assert [i["nes_id"] for i in plan.patch_items] == [ANKUR_IRI]
    assert len(plan.review) == 1
    assert len(plan.bound) == 1


# --------------------------------------------------------------------------
# Fix round 1 -- review response. Seven items; this section covers the six
# that land in this module (the ETag question's write-side enforcement is
# Task 7's, per the ruling; this module only surfaces its absence).
# --------------------------------------------------------------------------


# --- Item 1 (critical): the location gate must be an allow-list ----------


@pytest.mark.parametrize("relationship_type", ["Location", " location ", "", "organization"])
def test_plan_allow_lists_related_and_reviews_everything_else(relationship_type):
    # The gate used to be `item.get("relationship_type") == "location"` -- an
    # exact-string DENY-list -- while the extraction filter one function away
    # (`main()`'s `valid_items`) uses `.lower() in ("location", "related")`.
    # An LLM that capitalises the field ("Location") slipped past the deny-
    # list straight through resolve() to a real BIND -- reproduced by the
    # reviewer end to end. Inverted to an ALLOW-list: only a trimmed,
    # lowercased "related" is ever searched; "Location", padded whitespace,
    # an empty string, and an unrecognised value ("organization") all review
    # without spending a search request, exactly like a bare "location" does.
    case = {"slug": "case-scope", "state": "DRAFT", "entities": []}
    api = _SearchStubApi([case], {"अंकुर खत्री": [ANKUR_CANDIDATE]})
    plan = plan_case_entities(api, case, 'W/"e"', [
        {"entity_name": "अंकुर खत्री", "relationship_type": relationship_type,
         "notes": "क"}])

    assert plan.action == "NOOP"
    assert plan.patch_items == []
    assert plan.bound == []
    assert len(plan.review) == 1
    name, decision = plan.review[0]
    assert name == "अंकुर खत्री"
    assert decision.nes_id is None
    assert "scope" in decision.reason
    assert api.search_calls == []
    assert api.get_entity_calls == []


def test_plan_treats_a_missing_relationship_type_as_out_of_scope():
    # The allow-list's other half: a missing key must fail the same way as an
    # explicit "location" -- previously this was "safe" only because a filter
    # in the unrelated `main()` function happened to drop such items first, a
    # coupling this planner should not have to rely on.
    case = {"slug": "case-missing-type", "state": "DRAFT", "entities": []}
    api = _SearchStubApi([case], {"अंकुर खत्री": [ANKUR_CANDIDATE]})
    plan = plan_case_entities(api, case, 'W/"e"', [
        {"entity_name": "अंकुर खत्री", "notes": "क"}])  # no relationship_type at all

    assert plan.action == "NOOP"
    assert len(plan.review) == 1
    assert api.search_calls == []


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
    _, decision = plan.review[0]
    assert "502 Bad Gateway from api.example" in decision.reason


# --- Item 5 (important, new): a truncated candidate list must not bind ---


def test_plan_downgrades_a_bind_to_review_when_the_candidate_list_hit_the_search_cap():
    # `search_entities` itself documents that hitting its page cap means a
    # same-name tie may extend past the window it returned -- so the
    # ambiguity veto's premise (every tied candidate was seen) does not hold.
    # A BIND built on a capped-out list must not survive.
    cap = ENTITY_SEARCH_PAGE_SIZE * ENTITY_SEARCH_MAX_PAGES
    filler = [
        {"id": f"https://jawafdehi.org/entity/person/filler-{i:03d}-aaaaaa",
         "title": {"ne": "फरक नाम"}, "score": 1.0}
        for i in range(cap - 1)
    ]
    candidates = [*filler, ANKUR_CANDIDATE]
    assert len(candidates) == cap

    case = {"slug": "case-truncated", "state": "DRAFT", "entities": []}
    api = _SearchStubApi([case], {"अंकुर खत्री": candidates})
    plan = plan_case_entities(api, case, 'W/"e"', [
        {"entity_name": "अंकुर खत्री", "relationship_type": "related", "notes": "क"}])

    assert plan.action == "NOOP"
    assert plan.patch_items == []
    assert plan.bound == []
    assert len(plan.review) == 1
    name, decision = plan.review[0]
    assert decision.nes_id is None
    assert "cap" in decision.reason


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

    rows = [("जिल्ला शिक्षा कार्यालय, दाङ", "case-a", d()),
            ("जिल्ला शिक्षा कार्यालय, दाङ", "case-b", d()),
            ("गुल्बा कोरी", "case-c", d())]
    out = tmp_path / "run.nomatch.md"
    write_nomatch_report(out, rows)
    text = out.read_text(encoding="utf-8")
    assert text.index("जिल्ला शिक्षा कार्यालय, दाङ") < text.index("गुल्बा कोरी")
    assert "2" in text.splitlines()[text.splitlines().index(
        next(line for line in text.splitlines() if "दाङ" in line))]


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
        ("जिल्ला शिक्षा कार्यालय, दाङ", "case-a", weak),
        ("जिल्ला शिक्षा कार्यालय, दाङ", "case-b", strong),
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
    plan = plan_case_entities(api, case, 'W/"e"', extracted_items)

    summary = plan_summary(plan, extracted_items)
    assert summary == {
        "extracted": 3, "bound": 1, "review": 1, "nomatch": 1, "already_bound": 0,
    }


# --------------------------------------------------------------------------
# Task 8, fix round 1 -- the two refusal paths, the write-then-report order,
# and the report files `main()` actually writes.
# --------------------------------------------------------------------------


def _report_files():
    logger = logging.getLogger("casework.entities")
    return ere.report_paths(logger._casework_run_paths)


THREE_WAY_RESPONSE = json.dumps({
    "entities": [
        {"entity_name": "अंकुर खत्री", "relationship_type": "related", "notes": "क"},
        {"entity_name": "अनिष श्रेष्ठ", "relationship_type": "related", "notes": "ख"},
        {"entity_name": "खगेन्द्र पराजुली", "relationship_type": "related", "notes": "ग"},
    ],
    "accused_notes": [],
})

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
    assert "TOTAL entities bound to cases: 0" in out
    assert api.replace_list_calls == []
    assert Path(_report_files()["binds"]).read_text(encoding="utf-8") == ""
    statuses = [r["status"] for r in report.rows if r["slug"] == "case-write-fails"]
    assert "error" in statuses


def test_main_writes_the_three_report_files_with_the_right_rows(
    monkeypatch, patched_fetch_markdown
):
    # Nothing else pins which rows land in which file: swapping `bind_rows` for
    # `review_rows` at the `write_jsonl` calls passed the whole suite before
    # this test existed.
    case = dict(PRESS_ONLY_CASE, slug="case-three-way", entities=[])
    api = _SearchStubApi([case], THREE_WAY_SEARCH)
    _run_main(monkeypatch, api, invoke_text_stub=lambda **kw: THREE_WAY_RESPONSE,
              argv=["--apply"])

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
    assert [b["written"] for b in binds] == [False]
    assert api.replace_list_calls == []
