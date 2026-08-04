"""Tests for the DB-free standalone related-entities enricher
(casework/enrich_related_entities.py). EXTRACTION ONLY.

ARCHITECTURE FINDING (see module docstring for the full writeup, escalated and
confirmed with the dispatcher before any code was written): the donor
(0321a85) writes entities via `api.create_entity(display_name=name, nes_id="")`
-- a method that does not exist on this branch's `CaseworkApi` -- producing a
flat `{"entity": id, ...}` shape. The CURRENT schema
(`cases/caseworker_serializers.py::EntityPatchItemSerializer`) requires
`{"nes_id": <canonical NES @id IRI>, "relationship_type", "outcome"?, "notes"}`
and explicitly has "no display-name fallback". Turning an LLM-extracted name
into a confirmed `nes_id` needs a matching/confidence design with no donor
precedent -- wrongly binding a person to a corruption case by a bad fuzzy
match is a defamation risk, not a data-quality nit, and deserves its own
design. Per explicit instruction, this port does NOT build that resolver. It
ports the donor's LLM extraction faithfully and `main()` never calls
`api.patch_field` or `api.replace_list` -- this is asserted directly
(`test_main_never_writes_anything_via_patch_field_or_replace_list`).

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
from casework.enrich_related_entities import (
    _build_content_parts,
    _enforce_prompt_budget,
    _parse_extraction_response,
    _truncate_court_order,
    _truncate_press_release,
)
from tests.casework.fakes import FakeUsage

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
    "entities": [
        {"nes_id": "https://nes.jawafdehi.org/entity/1",
         "relationship_type": "related", "notes": "पहिल्यै बाँधिएको"},
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
    # that the case proceeds all the way to extraction.
    api = _StubApi([PRESS_CASE_ALREADY_POPULATED])
    stub = _call_tracking_stub(ENTITY_RESPONSE)
    report = _run_main(
        monkeypatch, api, invoke_text_stub=stub, argv=["--force", "--dry-run"])
    assert len(stub.calls) == 1
    assert report.rows[0]["status"] == "extracted-unbound"


def test_press_only_case_reaches_the_llm(monkeypatch, patched_fetch_markdown):
    stub = _call_tracking_stub(ENTITY_RESPONSE)
    api = _StubApi([PRESS_ONLY_CASE])
    report = _run_main(monkeypatch, api, invoke_text_stub=stub, argv=["--dry-run"])
    assert len(stub.calls) == 1
    assert report.rows[0]["status"] == "extracted-unbound"


def test_court_only_case_reaches_the_llm(monkeypatch, patched_fetch_markdown):
    stub = _call_tracking_stub(ENTITY_RESPONSE)
    api = _StubApi([COURT_ONLY_CASE])
    report = _run_main(monkeypatch, api, invoke_text_stub=stub, argv=["--dry-run"])
    assert len(stub.calls) == 1
    assert report.rows[0]["status"] == "extracted-unbound"


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


def test_main_never_writes_anything_via_patch_field_or_replace_list(
    monkeypatch, patched_fetch_markdown
):
    # THE central guarantee of this port: no matter how many cases extract
    # real entities, /entities is never PATCHed and replace_list is never
    # called -- with --apply, which is the flag that enables writes on every
    # sibling enricher.
    api = _StubApi([PRESS_ONLY_CASE, COURT_ONLY_CASE, BOTH_CASE])
    _run_main(monkeypatch, api, invoke_text_stub=lambda **kw: ENTITY_RESPONSE,
              argv=["--apply"])
    assert api.patch_calls == []
    assert api.replace_list_calls == []


def test_invalid_relationship_type_is_excluded_from_extracted_count(
    monkeypatch, patched_fetch_markdown
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
    report = _run_main(monkeypatch, api, invoke_text_stub=lambda **kw: response,
                        argv=["--dry-run"])
    assert "1 entities" in report.rows[0]["reason"]


def test_accused_notes_only_response_yields_zero_valid_entities_end_to_end(
    monkeypatch, patched_fetch_markdown
):
    # Exercises the shared-parser leak (see TestParseExtractionResponse) end
    # to end: with only accused_notes in the response, entities_data leaks
    # the same array, but main()'s valid_items filter (entity_name +
    # relationship_type required) rejects the leaked accused-note dicts, so
    # extraction still correctly reports 0 entities.
    response = json.dumps({"accused_notes": [{"name": "गोपाल", "notes": "अध्यक्ष"}]})
    api = _StubApi([PRESS_ONLY_CASE])
    report = _run_main(monkeypatch, api, invoke_text_stub=lambda **kw: response,
                        argv=["--dry-run"])
    assert report.rows[0]["status"] == "extracted-unbound"
    assert "0 entities" in report.rows[0]["reason"]
    assert "1 accused_notes" in report.rows[0]["reason"]


def test_summary_surfaces_zero_bound_count_unconditionally(
    monkeypatch, patched_fetch_markdown, capsys
):
    # Requirement: a run that extracted entities and bound zero must say so
    # PLAINLY in the run summary, not just in per-case logs.
    api = _StubApi([PRESS_ONLY_CASE])
    _run_main(monkeypatch, api, invoke_text_stub=lambda **kw: ENTITY_RESPONSE,
              argv=["--apply"])
    out = capsys.readouterr().out
    assert "TOTAL entities bound to cases: 0" in out
    assert "TOTAL entities extracted across all cases: 2" in out
    assert "TOTAL accused notes extracted: 1" in out


# --------------------------------------------------------------------------
# Task PP2 -- run-logging events file (see test_enrich_missing_bigo.py's
# identical block for the rationale; `conftest.py`'s autouse
# `_isolate_casework_run_logs` fixture keeps these out of the real repo
# `work/enricher-runs/`). This module is EXTRACTION ONLY -- there is no
# `write` step to check; `start`/`extract` are this file's ceiling.
# --------------------------------------------------------------------------


def _events_path():
    logger = logging.getLogger("casework.entities")
    return logger._casework_run_paths["events"]


def _read_events(path):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines()]


def test_events_file_covers_start_and_extract_happy_path(
    monkeypatch, patched_fetch_markdown, tmp_path
):
    api = _StubApi([PRESS_ONLY_CASE])
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
    # Never a write event -- this module never patches anything.
    assert not any(r["step"] == "write" for r in rows)
