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
import urllib.error
from pathlib import Path

import pytest

from casework import enrich_related_entities as ere
from casework.common.api import CandidateList, ENTITY_SEARCH_MAX_PAGES, ENTITY_SEARCH_PAGE_SIZE
from casework.court_record import CHARGED
from casework.enrich_related_entities import (
    PROMOTED_PREFIX,
    RELATIONSHIP_TYPES,
    _build_content_parts,
    _enforce_prompt_budget,
    _parse_extraction_response,
    _truncate_court_order,
    _truncate_press_release,
    current_entity_binds,
    is_promoted,
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

    # The prompt is the ONE donor constant this port deliberately diverges from,
    # because the enricher now binds every section the case API accepts and the
    # donor prompt could only ever emit two of them. Byte-equality is replaced by
    # two narrower pins: the parts that must not drift, and the exact divergence.
    # Everything else in this class stays byte-for-byte.
    def test_system_prompt_keeps_the_donor_parts_that_must_not_drift(self, donor):
        # The location rules and the accused-notes contract are unchanged from the
        # donor -- those drive extraction quality and prompt budgeting, and a
        # silent edit to them is the failure this class exists to catch.
        for anchor in ("PART 1 — LOCATION ENTITIES",
                       "DO NOT extract accused home addresses",
                       'The entity_name should include context in the format: '
                       '"Organisation/Activity - Location"',
                       "PART 3 — ACCUSED NOTES",
                       "Only include primary accused persons. Keep notes under 80 chars."):
            assert anchor in donor["SYSTEM_PROMPT"], "anchor is not donor text"
            assert anchor in ere.SYSTEM_PROMPT

    def test_system_prompt_offers_every_section_the_binder_can_write(self):
        # Without this, widening `plan_case_entities` to all nine sections is dead
        # code: the LLM never emits anything but the two the donor asked for.
        # Asserted against the prompt's own output-format line so the two cannot
        # drift apart.
        offered = {"location", "related", "accused", "alleged", "witness"}
        format_line = next(
            line for line in ere.SYSTEM_PROMPT.splitlines()
            if line.strip().startswith('"relationship_type"'))
        for section in offered:
            assert f'"{section}"' in format_line
            assert section in RELATIONSHIP_TYPES, (
                f"the prompt offers {section!r} but the binder would refuse it")

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
    # that the case proceeds all the way to resolution. No search result is
    # configured for either extracted name, so BOTH are no-matches -- including
    # the 'location' one, which now gets searched like any other section instead
    # of being refused before the request. Neither is a new write, hence a NOOP,
    # but a NOOP reached AFTER the LLM ran rather than by the pre-LLM skip.
    api = _SearchStubApi([PRESS_CASE_ALREADY_POPULATED])
    stub = _call_tracking_stub(ENTITY_RESPONSE)
    report = _run_main(
        monkeypatch, api, invoke_text_stub=stub, argv=["--force", "--dry-run"])
    assert len(stub.calls) == 1
    assert report.rows[0]["status"] == "already"
    assert report.rows[0]["reason"] == "0 for review, 2 no match"


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
    # The donor dropped anything that was not "location" or "related" here, and so
    # did this port until the binder learned every section. Both of these now
    # count: "accused" is a real section, and an UNKNOWN one is counted too, because
    # it reaches the planner and is recorded there as a review row. What must still
    # be dropped is an item with no name -- the planner skips those without
    # recording them anywhere, so counting one would corrupt `plan_summary`'s
    # already-bound subtraction.
    response = json.dumps({
        "entities": [
            {"entity_name": "गोपाल बहादुर", "relationship_type": "accused", "notes": "x"},
            {"entity_name": "सुर्खेत जिल्ला", "relationship_type": "location", "notes": ""},
            {"entity_name": "बुद्धि प्रसाद", "relationship_type": "organization",
             "notes": "unknown section, still counted"},
            {"entity_name": "   ", "relationship_type": "related", "notes": "no name"},
        ],
        "accused_notes": [],
    })
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
    frozen fixture documents look exactly like this and 24 of the 25 still
    BIND. The exception is the `मालपोत कार्यालय` bucket, which the structural
    unqualified-institution veto (`92dc4db`) now holds at REVIEW before this
    document is ever fetched.
    """

    def __init__(self, cases, search_results=None, documents=None, parties=None):
        super().__init__(cases)
        self._search = search_results or {}
        self._documents = documents or {}
        # Keyed "<court>/<number>", mirroring the real endpoint. Defaults to
        # empty so every pre-existing test keeps its old behaviour: a case with
        # no `court_cases` never reaches this at all.
        self._parties = parties or {}
        self.etag = 'W/"abc123"'
        self.search_calls = []
        self.get_entity_calls = []
        self.court_calls = []

    def search_entities(self, query, **kw):
        self.search_calls.append(query)
        return self._search.get(query, [])

    def get_court_case_entities(self, court, number, timeout=60):
        key = f"{court}/{number}"
        self.court_calls.append(key)
        configured = self._parties.get(key, [])
        if isinstance(configured, BaseException):
            raise configured
        return configured

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
    # same-name entities resolves to the first by the `(-score, nes_id)` sort and
    # binds; a name with NO candidate stays in nomatch, because there is nothing
    # to promote and this module may never create an NES entity.
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
    assert len(plan.bound) == 1
    assert plan.review == []
    assert len(plan.nomatch) == 1
    _name, decision, _notes = plan.bound[0]
    assert decision.nes_id == anish_a["id"]      # first by the deterministic sort
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
    name, decision = plan.review[0]
    assert name == "अंकुर खत्री"
    assert decision.nes_id is None
    assert "Election Commission" in decision.reason
    assert api.get_entity_calls == [ANKUR_IRI]


def test_permissive_binds_an_election_record_and_records_the_overridden_veto():
    # The default mode overrides this veto -- that is the requested behaviour --
    # but the bind must carry WHY, so the run's binds.jsonl can be filtered back
    # down to exactly the judgement calls.
    case = {"slug": "case-elect", "state": "DRAFT", "entities": []}
    election_doc = {"identifier": [{"propertyID": "ecn-candidate-id", "value": "12345"}]}
    api = _SearchStubApi(
        [case], {"अंकुर खत्री": [ANKUR_CANDIDATE]}, documents={ANKUR_IRI: election_doc})
    plan = plan_case_entities(api, case, 'W/"e"', [
        {"entity_name": "अंकुर खत्री", "relationship_type": "related", "notes": "क"}])

    assert plan.action == "WOULD_PATCH"
    assert [i["nes_id"] for i in plan.patch_items] == [ANKUR_IRI]
    assert plan.review == []
    _name, decision, _notes = plan.bound[0]
    assert decision.nes_id == ANKUR_IRI
    assert is_promoted(decision)
    assert "Election Commission" in decision.reason


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


SURKHET_IRI = "https://jawafdehi.org/entity/location/district/surkhet"
SURKHET_CANDIDATE = {"id": SURKHET_IRI, "title": {"ne": "सुर्खेत जिल्ला"},
                     "score": 190.0}


def test_a_location_item_binds_into_the_location_section():
    # 7 of 33 extracted names in a live smoke run came back
    # relationship_type="location", and every one of them used to be refused
    # before searching. They now bind, into `location` -- NOT into `related`:
    # the section comes from the extraction's own relationship_type, so a
    # district does not get filed as a related party.
    case = {"slug": "case-loc", "state": "DRAFT", "entities": []}
    api = _SearchStubApi([case], {"सुर्खेत जिल्ला": [SURKHET_CANDIDATE]},
                         documents={SURKHET_IRI: {"identifier": None}})
    plan = plan_case_entities(api, case, 'W/"e"', [
        {"entity_name": "सुर्खेत जिल्ला", "relationship_type": "location", "notes": ""}])

    assert plan.action == "WOULD_PATCH"
    assert plan.patch_items == [{"nes_id": SURKHET_IRI,
                                 "relationship_type": "location", "notes": ""}]
    assert len(plan.bound) == 1
    assert plan.review == []
    # `outcome` is legal only on an accused bind -- a location must not carry one.
    assert "outcome" not in plan.patch_items[0]


def test_a_location_and_a_related_item_each_bind_into_their_own_section():
    # A mixed extraction lands in two different sections from one pass. This is
    # the behaviour the whole change exists for: bind everything that matched,
    # each into the section it was extracted under.
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
        SURKHET_IRI: "location", ANKUR_IRI: "related"}
    assert plan.review == []
    assert len(plan.bound) == 2


def test_an_accused_extraction_binds_as_accused_and_carries_the_charged_outcome():
    # `accused` is the one section with an extra requirement: the DB's
    # `outcome_only_on_accused` CHECK makes `outcome` legal here and nowhere
    # else, and every case in this corpus is a Special Court `-CR-` case, so
    # 'charged' is true by construction.
    case = {"slug": "case-acc", "state": "DRAFT", "entities": []}
    api = _SearchStubApi([case], {"अंकुर खत्री": [ANKUR_CANDIDATE]},
                         documents={ANKUR_IRI: {"identifier": None}})
    plan = plan_case_entities(api, case, 'W/"e"', [
        {"entity_name": "अंकुर खत्री", "relationship_type": "accused", "notes": "क"}])

    assert plan.action == "WOULD_PATCH"
    assert plan.patch_items == [{"nes_id": ANKUR_IRI, "relationship_type": "accused",
                                 "outcome": CHARGED, "notes": "क"}]


# --------------------------------------------------------------------------
# Fix round 1 -- review response. Seven items; this section covers the six
# that land in this module (the ETag question's write-side enforcement is
# Task 7's, per the ruling; this module only surfaces its absence).
# --------------------------------------------------------------------------


# --- Item 1 (critical): the location gate must be an allow-list ----------


@pytest.mark.parametrize("relationship_type,expected", [
    ("Location", "location"),      # casing is normalised, not refused
    (" location ", "location"),    # so is padding
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


@pytest.mark.parametrize("relationship_type", ["", "organization", "Related party", None])
def test_a_section_the_api_does_not_accept_reviews_without_searching(relationship_type):
    # There is no section to bind these into, and guessing one would file the
    # entity under a relationship nobody asserted. Refused BEFORE any search, so
    # a malformed extraction costs no request.
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
    _, decision = plan.review[0]
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
    _name, decision, _notes = plan.bound[0]
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
    assert [n for n, _, _ in plan.bound] == ["अंकुर खत्री"]


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
    plan = plan_case_entities(api, case, 'W/"e"', extracted_items, strict=True)

    summary = plan_summary(plan, extracted_items)
    # Exact equality on purpose: the point is that the four extraction counts
    # reconcile with nothing left over. Strict mode, so the split really is
    # one-of-each. The accused counts are zero because this case cites no court
    # reference, which is exactly what should keep them out of the
    # `already_bound` subtraction.
    assert summary == {
        "extracted": 3, "bound": 1, "review": 1, "nomatch": 1, "already_bound": 0,
        "accused_bound": 0, "accused_review": 0, "accused_nomatch": 0,
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

    assert plan_summary(plan, extracted_items) == {
        "extracted": 3, "bound": 2, "review": 0, "nomatch": 1, "already_bound": 0,
        "accused_bound": 0, "accused_review": 0, "accused_nomatch": 0,
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
    # Two rows in the default mode: the clean match, plus the promoted ambiguity.
    # Both must be marked unwritten -- a promoted bind is still only a prediction
    # in a dry run.
    assert [b["written"] for b in binds] == [False, False]
    assert api.replace_list_calls == []
    assert [b["reason"].startswith(PROMOTED_PREFIX) for b in binds] == [False, True]


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
    assert "TOTAL entities that WOULD bind to cases (dry run, nothing written): 0" in out
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
# Accused binds. The names come from the case's own NGM court record
# (`casework.court_record`), never from the LLM extraction, and they resolve
# through the SAME search/resolve/veto path `related` uses.
#
# They are tracked in their own plan lists on purpose: `plan_summary` derives
# `already_bound` by subtracting bound/review/nomatch from the EXTRACTED count,
# so folding court-record names into those lists would drive it negative.
# --------------------------------------------------------------------------

KAMALA_IRI = "https://jawafdehi.org/entity/person/kamala-thapa-7c1e44"
KAMAL_IRI = "https://jawafdehi.org/entity/person/kamala-thapa-9f2b01"
COURT_CASE = {
    "slug": "case-y", "state": "DRAFT", "entities": [],
    "court_cases": ["https://jawafdehi.org/courtcase/special/080-cr-0111"],
}


def _kamala_candidate(iri, ne):
    return {"id": iri, "title": {"ne": ne}, "score": 190.0}


def test_a_court_record_defendant_is_bound_as_accused_and_charged():
    api = _SearchStubApi(
        [COURT_CASE],
        {"कमला थापा": [_kamala_candidate(KAMALA_IRI, "कमला थापा")]},
        parties={"special/080-cr-0111": [
            {"side": "plaintiff", "name": "नेपाल सरकार"},
            {"side": "defendant", "name": "कमला थापा"},
        ]})
    plan = plan_case_entities(api, COURT_CASE, 'W/"e"', [])

    assert plan.action == "WOULD_PATCH"
    assert plan.patch_items == [{
        "nes_id": KAMALA_IRI, "relationship_type": "accused",
        "outcome": "charged", "notes": "",
    }]
    assert [name for name, _ in plan.accused_bound] == ["कमला थापा"]


def test_an_ambiguous_defendant_lands_in_accused_review_under_strict():
    """कमल vs कमला is the pair this whole guard exists for. Under strict, two
    entities above threshold must refuse, into the ACCUSED review bucket -- not
    the extraction one -- and the refusal must not corrupt plan_summary."""
    api = _SearchStubApi(
        [COURT_CASE],
        {"कमला थापा": [_kamala_candidate(KAMALA_IRI, "कमला थापा"),
                       _kamala_candidate(KAMAL_IRI, "कमला थापा")]},
        parties={"special/080-cr-0111": [
            {"side": "defendant", "name": "कमला थापा"}]})
    plan = plan_case_entities(api, COURT_CASE, 'W/"e"', [], strict=True)

    assert plan.patch_items == []
    assert plan.action == "NOOP"
    assert len(plan.accused_review) == 1
    assert plan.review == []          # extraction lists stay untouched
    assert plan.nomatch == []


def test_an_ambiguous_defendant_is_promoted_into_an_accused_bind_by_default():
    """The same कमल/कमला pair in the default mode: it binds, to the first by the
    deterministic sort, and the promotion is recorded on the accused decision so
    the accused rows are auditable exactly like the extracted ones."""
    api = _SearchStubApi(
        [COURT_CASE],
        {"कमला थापा": [_kamala_candidate(KAMALA_IRI, "कमला थापा"),
                       _kamala_candidate(KAMAL_IRI, "कमला थापा")]},
        documents={KAMALA_IRI: {"identifier": None},
                   KAMAL_IRI: {"identifier": None}},
        parties={"special/080-cr-0111": [
            {"side": "defendant", "name": "कमला थापा"}]})
    plan = plan_case_entities(api, COURT_CASE, 'W/"e"', [])

    assert plan.action == "WOULD_PATCH"
    assert len(plan.accused_bound) == 1
    assert plan.accused_review == []
    _name, decision = plan.accused_bound[0]
    assert decision.nes_id == min(KAMALA_IRI, KAMAL_IRI)
    assert is_promoted(decision)
    assert plan.patch_items[0]["relationship_type"] == "accused"
    assert plan.patch_items[0]["outcome"] == CHARGED


def test_accused_never_disturbs_the_already_bound_arithmetic():
    case = dict(COURT_CASE, entities=[])
    api = _SearchStubApi(
        [case],
        {"अंकुर खत्री": [ANKUR_CANDIDATE],
         "कमला थापा": [_kamala_candidate(KAMALA_IRI, "कमला थापा")]},
        parties={"special/080-cr-0111": [
            {"side": "defendant", "name": "कमला थापा"}]})
    items = [{"entity_name": "अंकुर खत्री", "relationship_type": "related",
              "notes": "क"}]
    plan = plan_case_entities(api, case, 'W/"e"', items)
    summary = plan_summary(plan, items)

    assert summary["extracted"] == 1
    assert summary["bound"] == 1
    assert summary["already_bound"] == 0     # never negative
    assert summary["accused_bound"] == 1


def test_a_defendant_already_bound_on_the_case_is_not_re_added():
    case = dict(COURT_CASE, entities=[
        {"nes_id": KAMALA_IRI, "type": "accused", "outcome": "convicted",
         "notes": "तत्कालीन अध्यक्ष"}])
    api = _SearchStubApi(
        [case],
        {"कमला थापा": [_kamala_candidate(KAMALA_IRI, "कमला थापा")]},
        parties={"special/080-cr-0111": [
            {"side": "defendant", "name": "कमला थापा"}]})
    plan = plan_case_entities(api, case, 'W/"e"', [])

    assert plan.action == "NOOP"
    assert plan.accused_bound == []


def test_an_unreadable_court_reference_is_recorded_not_raised():
    err = urllib.error.HTTPError("u", 404, "Not Found", {}, None)
    api = _SearchStubApi([COURT_CASE], {},
                         parties={"special/080-cr-0111": err})
    plan = plan_case_entities(api, COURT_CASE, 'W/"e"', [])

    assert plan.action == "NOOP"
    assert any("404" in line for line in plan.court_skips)


def test_a_case_with_no_court_reference_spends_no_search_request():
    case = {"slug": "case-z", "state": "DRAFT", "entities": []}
    api = _SearchStubApi([case], {})
    plan = plan_case_entities(api, case, 'W/"e"', [])

    assert api.court_calls == []
    assert api.search_calls == []
    assert any("no court reference" in line for line in plan.court_skips)
