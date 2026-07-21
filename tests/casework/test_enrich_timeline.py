"""Tests for the DB-free standalone timeline enricher (casework/enrich_timeline.py).

`enrich_timeline.py` extracts a case's factual timeline from CIAA press-release
and court-order markdown (plus best-effort NGM hearing context), at the premium
tier with a `convert_date` tool loop, and writes exactly ONE field:
`api.patch_field(slug, "timeline", entries)` (donor `enrich_timeline.py:358`).

BRIEF-VS-DONOR DIFFERENCE (see module docstring for the full writeup): the
task-14 brief's Step 2 test asked for a
`validate_timeline_items(items) -> (ok, bad_items)` helper that rejects a
`date_bs` failing the shape regex `^\\d{4}-\\d{2}-\\d{2}$`. Neither the function
nor that client-side regex check exists anywhere in the donor's history --
`git log --all -p -- casework/enrich_timeline.py` never mentions
`validate_timeline_items`, and the donor's real `_clean_entry` (ported here
unchanged) validates ONLY the AD `date`/`end_date` via `is_valid_iso_date`;
`date_bs`/`end_date_bs` pass through as opaque strings with no shape check at
all. That regex DOES exist in this codebase, but server-side, in
`cases/caseworker_serializers.py::TimelineItemSerializer._BS_DATE_RE` -- the
case PATCH endpoint's own validation, unrelated to this task. This test file
does NOT implement `validate_timeline_items` (it would be an invented function
with no donor basis) and instead pins the donor's real, more permissive
behavior directly (`TestCleanEntryDateHandling::test_malformed_bs_shape_is_kept_verbatim`).

The `TestDonorFidelity` class re-derives every prompt constant and pinned
threshold directly from the donor at commit `0321a85` (via `git show` +
`ast`, not by trusting this file's own transcription) and asserts equality --
a drifted clause changes LLM behavior with zero other test failures.
"""
import ast
import json
import logging
import re
import subprocess
import sys
import types
from pathlib import Path

import pytest

from casework import enrich_timeline as et
from casework.common.llm import tier_for
from casework.enrich_timeline import (
    _assemble_source_text,
    _clean_entry,
    _clamp,
    _extract_timeline,
    _get_ngm_data,
    _parse_timeline_response,
    _special_case_number,
    convert_date,
    convert_date_tool,
    summarize_verdict,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DONOR_COMMIT = "0321a85"


def _donor_source(path: str = "casework/enrich_timeline.py") -> str:
    proc = subprocess.run(
        ["git", "show", f"{DONOR_COMMIT}:{path}"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        pytest.skip(
            f"donor commit {DONOR_COMMIT} not in local history "
            "(shallow clone?); fidelity check needs full git history")
    return proc.stdout


def _literal_assign(tree: ast.AST, name: str):
    """AST node for the RHS of a top-level `name = ...` assignment, or None."""
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and target.id == name:
                return node.value
    return None


def _literal_const(tree: ast.AST, name: str):
    """literal_eval a plain-literal top-level assignment (e.g. a triple-quoted string)."""
    node = _literal_assign(tree, name)
    assert node is not None, f"{name} not found as a top-level assignment"
    return ast.literal_eval(node)


def _env_int_default(tree: ast.AST, name: str) -> int:
    """Extract the literal default int from `NAME = env_int("ENV", DEFAULT)`."""
    node = _literal_assign(tree, name)
    assert node is not None, f"{name} not found as a top-level assignment"
    assert isinstance(node, ast.Call) and len(node.args) >= 2, (
        f"{name} is not an env_int(...) call in the donor source"
    )
    return ast.literal_eval(node.args[1])


def _source_segment(tree: ast.AST, source: str, name: str) -> str:
    """Raw source text of a top-level assignment's RHS (works for f-strings,
    which `ast.literal_eval` cannot evaluate)."""
    node = _literal_assign(tree, name)
    assert node is not None, f"{name} not found as a top-level assignment"
    segment = ast.get_source_segment(source, node)
    assert segment is not None
    return segment


@pytest.fixture(scope="module")
def donor_timeline_source() -> str:
    return _donor_source("casework/enrich_timeline.py")


@pytest.fixture(scope="module")
def donor_common_source() -> str:
    return _donor_source("casework/common.py")


@pytest.fixture(scope="module")
def donor_timeline_tree(donor_timeline_source):
    return ast.parse(donor_timeline_source)


@pytest.fixture(scope="module")
def donor_common_tree(donor_common_source):
    return ast.parse(donor_common_source)


@pytest.fixture(scope="module")
def shipped_source() -> str:
    return Path(et.__file__).read_text()


@pytest.fixture(scope="module")
def shipped_tree(shipped_source):
    return ast.parse(shipped_source)


class TestDonorFidelity:
    """Byte-for-byte pins against the donor at commit 0321a85 -- NOT this
    module's own transcription. A drifted clause is the highest-consequence
    silent failure available in these files: it changes LLM behavior with
    zero test failures anywhere else."""

    # -- prompt constants (casework/enrich_timeline.py) ---------------------

    def test_extraction_system_prompt_matches_donor(self, donor_timeline_tree):
        assert et.EXTRACTION_SYSTEM_PROMPT == _literal_const(
            donor_timeline_tree, "EXTRACTION_SYSTEM_PROMPT")

    def test_extraction_user_prompt_matches_donor(self, donor_timeline_tree):
        assert et.EXTRACTION_USER_PROMPT == _literal_const(
            donor_timeline_tree, "EXTRACTION_USER_PROMPT")

    # -- prompt constant (casework/common.py -- shared with enrich_description) --

    def test_verdict_summary_system_prompt_matches_donor(
        self, donor_common_tree, donor_common_source, shipped_tree, shipped_source
    ):
        # An f-string (interpolates VERDICT_SUMMARY_TARGET) -- ast.literal_eval
        # cannot evaluate a JoinedStr node, so pin the raw SOURCE TEXT of the
        # assignment instead, which is at least as strong a check (it also
        # catches whitespace-only drift) and needs no evaluation.
        donor_segment = _source_segment(
            donor_common_tree, donor_common_source, "VERDICT_SUMMARY_SYSTEM_PROMPT")
        shipped_segment = _source_segment(
            shipped_tree, shipped_source, "VERDICT_SUMMARY_SYSTEM_PROMPT")
        assert shipped_segment == donor_segment

    # -- numeric thresholds --------------------------------------------------

    def test_timeline_source_chars_matches_donor_default(self, donor_timeline_tree):
        assert et.TIMELINE_SOURCE_CHARS == _env_int_default(
            donor_timeline_tree, "TIMELINE_SOURCE_CHARS")

    def test_timeline_max_tokens_matches_donor_default(self, donor_timeline_tree):
        assert et.TIMELINE_MAX_TOKENS == _env_int_default(
            donor_timeline_tree, "TIMELINE_MAX_TOKENS")

    def test_verdict_summary_trigger_matches_donor_default(self, donor_common_tree):
        assert et.VERDICT_SUMMARY_TRIGGER == _env_int_default(
            donor_common_tree, "VERDICT_SUMMARY_TRIGGER")

    def test_verdict_summary_target_matches_donor_default(self, donor_common_tree):
        assert et.VERDICT_SUMMARY_TARGET == _env_int_default(
            donor_common_tree, "VERDICT_SUMMARY_TARGET")

    def test_verdict_summary_max_tokens_matches_donor_default(self, donor_common_tree):
        assert et.VERDICT_SUMMARY_MAX_TOKENS == _env_int_default(
            donor_common_tree, "VERDICT_SUMMARY_MAX_TOKENS")

    def test_verdict_summary_chunk_chars_matches_donor_default(self, donor_common_tree):
        assert et.VERDICT_SUMMARY_CHUNK_CHARS == _env_int_default(
            donor_common_tree, "VERDICT_SUMMARY_CHUNK_CHARS")

    # -- structural pins ------------------------------------------------------

    def test_donor_writes_exactly_one_field_via_patch_field(self, donor_timeline_source):
        assert donor_timeline_source.count("api.patch_field(") == 1
        assert 'patch_field(case_slug, "timeline"' in donor_timeline_source

    def test_donor_uses_premium_tier_exactly_once(self, donor_timeline_source):
        # enrich_timeline.py:518 -- the ONLY tier= literal in this file (the
        # verdict summariser's tier="premium" lives in the separate common.py).
        assert donor_timeline_source.count('tier="premium"') == 1

    def test_donor_max_iterations_is_30(self, donor_timeline_source):
        assert "max_iterations=30" in donor_timeline_source

    def test_tier_for_timeline_is_premium(self):
        # Cross-checks casework/common/llm.py's TIERS table against the donor
        # pin above, the same "premium" this file's _extract_timeline uses.
        assert tier_for("timeline") == "premium"

    def test_donor_never_defines_validate_timeline_items(self, donor_timeline_source):
        # Pins the brief-vs-donor finding: the brief's Step 2 test imports a
        # `validate_timeline_items` helper that the donor never had.
        assert "validate_timeline_items" not in donor_timeline_source

    def test_donor_has_no_bs_date_shape_validation(self, donor_timeline_source):
        # The donor never regex-validates date_bs/end_date_bs -- no re.compile,
        # no _BS_DATE_RE, anywhere in this file.
        assert "re.compile" not in donor_timeline_source
        assert "_BS_DATE_RE" not in donor_timeline_source


# --------------------------------------------------------------------------
# convert_date -- the highest-risk piece of logic in this file
# --------------------------------------------------------------------------


class TestConvertDate:
    def test_ad_to_bs_round_trips_through_the_nepali_package(self):
        # Cross-checked directly against `nepali.datetime.nepalidate` (not a
        # fabricated pair): 2024-01-15 AD <-> 2080-10-01 BS.
        result = convert_date(["2024-01-15"], mode="ad_to_bs")
        assert result == {"2024-01-15": "2080-10-01"}

    def test_bs_to_ad_round_trips_through_the_nepali_package(self):
        result = convert_date(["2080-10-01"], mode="bs_to_ad")
        assert result == {"2080-10-01": "2024-01-15"}

    def test_batches_multiple_dates_in_one_call(self):
        result = convert_date(["2024-01-15", "2024-06-01"], mode="ad_to_bs")
        assert set(result.keys()) == {"2024-01-15", "2024-06-01"}
        assert result["2024-01-15"] == "2080-10-01"

    def test_devanagari_digits_are_normalised_before_conversion(self):
        # CIAA source dates are written with Devanagari numerals.
        result = convert_date(["२०८०-१०-०१"], mode="bs_to_ad")
        assert result["२०८०-१०-०१"] == "2024-01-15"

    def test_slash_separator_is_normalised_to_dash(self):
        result = convert_date(["2080/10/01"], mode="bs_to_ad")
        assert result["2080/10/01"] == "2024-01-15"

    def test_invalid_mode_raises(self):
        with pytest.raises(ValueError):
            convert_date(["2024-01-15"], mode="sideways")

    def test_non_list_dates_raises(self):
        with pytest.raises(ValueError):
            convert_date("2024-01-15", mode="ad_to_bs")

    def test_non_string_item_is_an_error_entry_not_a_raise(self):
        result = convert_date([12345], mode="ad_to_bs")
        assert result["12345"].startswith("Error:")

    def test_malformed_shape_is_an_error_entry_not_a_raise(self):
        result = convert_date(["2024-01"], mode="ad_to_bs")
        assert result["2024-01"].startswith("Error:")

    def test_out_of_range_date_is_an_error_entry_not_a_raise(self):
        # 2024-02-30 does not exist in either calendar.
        result = convert_date(["2024-02-30"], mode="ad_to_bs")
        assert result["2024-02-30"].startswith("Error:")

    def test_convert_date_tool_wraps_the_function(self):
        tool = convert_date_tool()
        assert tool.name == "convert_date"
        assert tool.run is convert_date
        assert tool.input_schema["required"] == ["dates", "mode"]


# --------------------------------------------------------------------------
# _clean_entry -- deliberate date-input-space enumeration
# --------------------------------------------------------------------------


class TestCleanEntryDateHandling:
    def test_valid_iso_date_is_kept(self):
        entry = _clean_entry({"date": "2024-01-15", "title": "उजुरी दर्ता"})
        assert entry == {"date": "2024-01-15", "title": "उजुरी दर्ता"}

    def test_valid_bs_date_is_kept_alongside_ad_date(self):
        entry = _clean_entry(
            {"date": "2024-01-15", "date_bs": "2080-10-01", "title": "फैसला"})
        assert entry["date"] == "2024-01-15"
        assert entry["date_bs"] == "2080-10-01"

    # Production-hardening (2026-07-21): _clean_entry now NORMALISES date_bs /
    # end_date_bs toward the server's shape (slash->dash, Devanagari->ASCII),
    # because the case PATCH endpoint's TimelineItemSerializer._BS_DATE_RE
    # (^\d{4}-\d{2}-\d{2}$) 422s the WHOLE timeline on a slash-format date_bs,
    # and the A/B run proved haiku emits slashes on some cases. This mirrors the
    # normalisation convert_date already applies; it does not add a rejecting
    # regex (so the donor-source shape-validation pin above still holds).
    _BS_SHAPE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

    def test_slash_date_bs_is_normalised_to_dash(self):
        entry = _clean_entry(
            {"date": "2024-01-15", "date_bs": "2080/10/01", "title": "फैसला"})
        assert entry is not None
        assert entry["date_bs"] == "2080-10-01"
        # Cross-check: the normalised value now satisfies the server regex that
        # was rejecting the slash form.
        assert self._BS_SHAPE.match(entry["date_bs"])

    def test_devanagari_date_bs_is_normalised(self):
        entry = _clean_entry(
            {"date": "2024-01-15", "date_bs": "२०८०/१०/०१", "title": "फैसला"})
        assert entry["date_bs"] == "2080-10-01"

    def test_slash_end_date_bs_is_normalised(self):
        entry = _clean_entry({
            "date": "2024-01-15", "end_date": "2024-06-01",
            "date_bs": "2080/10/01", "end_date_bs": "2081/02/19",
            "title": "अवधि"})
        assert entry["date_bs"] == "2080-10-01"
        assert entry["end_date_bs"] == "2081-02-19"

    def test_already_dash_date_bs_is_unchanged(self):
        entry = _clean_entry(
            {"date": "2024-01-15", "date_bs": "2080-10-01", "title": "फैसला"})
        assert entry["date_bs"] == "2080-10-01"

    def test_garbage_bs_string_without_slashes_is_still_kept(self):
        # Normalisation only touches separators/digits; true garbage (no slash,
        # no Devanagari) is unchanged. The chosen fix is normalise-not-reject,
        # so such a value would still 422 server-side -- acceptable: haiku emits
        # slash/Devanagari forms, not free-text, for date_bs.
        entry = _clean_entry(
            {"date": "2024-01-15", "date_bs": "not-a-date-at-all", "title": "फैसला"})
        assert entry is not None
        assert entry["date_bs"] == "not-a-date-at-all"

    def test_malformed_ad_date_is_rejected(self):
        assert _clean_entry({"date": "2024-13-40", "title": "पेसी"}) is None

    def test_absent_date_is_rejected(self):
        assert _clean_entry({"title": "पेसी"}) is None

    def test_blank_date_is_rejected(self):
        assert _clean_entry({"date": "   ", "title": "पेसी"}) is None

    def test_partial_year_only_date_is_rejected(self):
        assert _clean_entry({"date": "2024", "title": "पेसी"}) is None

    def test_out_of_range_ad_date_is_rejected(self):
        # 2024-02-30 -- no such day.
        assert _clean_entry({"date": "2024-02-30", "title": "पेसी"}) is None

    def test_missing_title_is_rejected(self):
        assert _clean_entry({"date": "2024-01-15"}) is None

    def test_title_falls_back_to_event_then_name(self):
        assert _clean_entry({"date": "2024-01-15", "event": "फैसला"})["title"] == "फैसला"
        assert _clean_entry({"date": "2024-01-15", "name": "फैसला"})["title"] == "फैसला"

    def test_end_date_before_date_is_dropped_but_entry_kept(self):
        entry = _clean_entry(
            {"date": "2024-01-15", "end_date": "2024-01-01", "title": "अवधि"})
        assert entry is not None
        assert "end_date" not in entry

    def test_invalid_end_date_is_dropped_but_entry_kept(self):
        entry = _clean_entry(
            {"date": "2024-01-15", "end_date": "not-a-date", "title": "अवधि"})
        assert entry is not None
        assert "end_date" not in entry

    def test_valid_end_date_after_date_is_kept_with_bs(self):
        entry = _clean_entry({
            "date": "2024-01-15", "end_date": "2024-06-01",
            "date_bs": "2080-10-01", "end_date_bs": "2081-02-19",
            "title": "जाँच अवधि",
        })
        assert entry["end_date"] == "2024-06-01"
        assert entry["end_date_bs"] == "2081-02-19"

    def test_end_date_bs_is_dropped_when_end_date_itself_is_dropped(self):
        entry = _clean_entry({
            "date": "2024-01-15", "end_date": "not-a-date",
            "end_date_bs": "2080-10-01", "title": "अवधि",
        })
        assert "end_date_bs" not in entry

    def test_description_falls_back_to_desc_then_detail(self):
        assert _clean_entry(
            {"date": "2024-01-15", "title": "x", "desc": "विवरण"}
        )["description"] == "विवरण"
        assert _clean_entry(
            {"date": "2024-01-15", "title": "x", "detail": "विवरण"}
        )["description"] == "विवरण"

    def test_blank_description_is_omitted_not_empty_string(self):
        entry = _clean_entry({"date": "2024-01-15", "title": "x", "description": "   "})
        assert "description" not in entry


# --------------------------------------------------------------------------
# _parse_timeline_response
# --------------------------------------------------------------------------


class TestParseTimelineResponse:
    def test_parses_bare_json_array(self):
        body = json.dumps([{"date": "2024-01-15", "title": "क"}])
        assert _parse_timeline_response(body) == [{"date": "2024-01-15", "title": "क"}]

    def test_parses_wrapped_timeline_key(self):
        body = json.dumps({"timeline": [{"date": "2024-01-15", "title": "क"}]})
        assert _parse_timeline_response(body) == [{"date": "2024-01-15", "title": "क"}]

    def test_sorts_entries_chronologically(self):
        body = json.dumps([
            {"date": "2024-06-01", "title": "पछिल्लो"},
            {"date": "2020-01-01", "title": "पहिलो"},
        ])
        result = _parse_timeline_response(body)
        assert [e["date"] for e in result] == ["2020-01-01", "2024-06-01"]

    def test_drops_invalid_entries_keeps_valid_ones(self):
        body = json.dumps([
            {"date": "2024-01-15", "title": "राम्रो"},
            {"date": "not-a-date", "title": "नराम्रो"},
            "not even a dict",
        ])
        result = _parse_timeline_response(body)
        assert result == [{"date": "2024-01-15", "title": "राम्रो"}]

    def test_returns_none_when_all_entries_invalid(self):
        body = json.dumps([{"date": "bad", "title": "x"}])
        assert _parse_timeline_response(body) is None

    def test_returns_none_for_unparseable_text(self):
        assert _parse_timeline_response("not json at all") is None


# --------------------------------------------------------------------------
# _clamp
# --------------------------------------------------------------------------


class TestClamp:
    def test_short_text_is_not_truncated(self):
        assert _clamp("hello", 100, "court order") == "hello"

    def test_long_text_is_truncated_to_limit(self):
        assert len(_clamp("x" * 200, 100, "court order")) == 100

    def test_zero_limit_means_no_limit(self):
        text = "x" * 500
        assert _clamp(text, 0, "court order") == text

    def test_none_text_becomes_empty_string(self):
        assert _clamp(None, 100, "court order") == ""


# --------------------------------------------------------------------------
# summarize_verdict
# --------------------------------------------------------------------------


class TestSummarizeVerdict:
    def test_empty_text_returns_none(self):
        assert summarize_verdict("", invoke_text=lambda **kw: "x", usage=None) is None

    def test_single_chunk_returns_stripped_result(self):
        result = summarize_verdict(
            "फैसला पाठ", invoke_text=lambda **kw: "  सारांश  ", usage=None)
        assert result == "सारांश"

    def test_provider_failure_returns_none(self):
        def stub(**kw):
            raise RuntimeError("provider down")

        assert summarize_verdict("फैसला", invoke_text=stub, usage=None) is None

    def test_multi_chunk_is_concatenated_with_original_part_index(self):
        # `chunk = max(20000, VERDICT_SUMMARY_CHUNK_CHARS)` floors the chunk
        # size at 20000 regardless of the constant, so forcing a multi-chunk
        # split needs real length (> VERDICT_SUMMARY_CHUNK_CHARS), not a
        # monkeypatched constant. 160000 chars over the 150000-char default
        # chunk size yields exactly 2 parts.
        text = "क" * 160000
        calls = []

        def stub(**kw):
            calls.append(kw["content"])
            return f"सारांश-{len(calls)}"

        result = summarize_verdict(text, invoke_text=stub, usage=None)
        assert len(calls) == 2
        assert result == "[खण्ड 1/2]\nसारांश-1\n\n[खण्ड 2/2]\nसारांश-2"

    def test_multi_chunk_keeps_original_index_when_a_middle_chunk_fails(self):
        # 300001 chars over a 150000-char chunk size yields exactly 3 parts.
        text = "क" * 300001

        def stub(**kw):
            if "part 2 of 3" in kw["content"]:
                raise RuntimeError("mid-chunk failure")
            return "ठीक छ"

        result = summarize_verdict(text, invoke_text=stub, usage=None)
        # Part 2 failed and is skipped -- the survivors keep their ORIGINAL
        # खण्ड numbers (1/3, 3/3), never renumbered to (1/2, 2/2).
        assert "[खण्ड 1/3]" in result
        assert "[खण्ड 3/3]" in result
        assert "[खण्ड 2/3]" not in result


# --------------------------------------------------------------------------
# _assemble_source_text -- priority ordering + court-order summarisation
# --------------------------------------------------------------------------


class TestAssembleSourceText:
    def test_court_text_under_threshold_is_not_summarised(self):
        calls = []

        def stub(**kw):
            calls.append(kw)
            return "should not be called"

        result = _assemble_source_text("छोटो फैसला।", "", invoke_text=stub, usage=None)
        assert calls == []
        assert "[COURT_ORDER]" in result
        assert "छोटो फैसला।" in result

    def test_court_text_over_threshold_is_summarised(self):
        long_court_text = "क" * 13000  # > min(VERDICT_SUMMARY_TRIGGER, budget)=12000
        calls = []

        def stub(**kw):
            calls.append(kw)
            return "सारांशित फैसला"

        result = _assemble_source_text(
            long_court_text, "", invoke_text=stub, usage=None)
        assert len(calls) == 1
        assert "COURT_ORDER (फैसला सारांश)" in result
        assert "सारांशित फैसला" in result
        assert long_court_text not in result

    def test_summary_failure_falls_back_to_truncated_head(self):
        long_court_text = "क" * 13000

        def stub(**kw):
            return ""  # falsy -- summarize_verdict returns None

        result = _assemble_source_text(
            long_court_text, "", invoke_text=stub, usage=None)
        assert "[COURT_ORDER]" in result
        assert "फैसला सारांश" not in result
        # Fell back to a head clamp at VERDICT_SUMMARY_TARGET chars.
        assert long_court_text[: et.VERDICT_SUMMARY_TARGET] in result

    def test_court_text_precedes_press_text(self):
        result = _assemble_source_text(
            "अदालतको आदेश", "प्रेस विज्ञप्ति", invoke_text=lambda **kw: "x", usage=None)
        assert result.index("[COURT_ORDER]") < result.index("[PRESS_RELEASE]")

    def test_press_only_when_no_court_text(self):
        result = _assemble_source_text("", "प्रेस विज्ञप्ति", invoke_text=None, usage=None)
        assert "[COURT_ORDER]" not in result
        assert "[PRESS_RELEASE]" in result

    def test_both_empty_returns_empty_string(self):
        assert _assemble_source_text("", "", invoke_text=None, usage=None) == ""

    def test_budget_exhausted_drops_press_text(self):
        result = _assemble_source_text(
            "क" * et.TIMELINE_SOURCE_CHARS, "प्रेस विज्ञप्ति",
            invoke_text=lambda **kw: "क" * et.TIMELINE_SOURCE_CHARS, usage=None,
        )
        assert "[PRESS_RELEASE]" not in result


# --------------------------------------------------------------------------
# _extract_timeline -- tier/max_tokens/max_iterations/tool pin
# --------------------------------------------------------------------------


def test_extract_timeline_uses_premium_tier_and_pinned_call_shape():
    """Pins the donor's tier="premium"/max_tokens/max_iterations=30 arguments
    (enrich_timeline.py:517-520)."""
    seen = {}

    def stub(**kw):
        seen.update(kw)
        return json.dumps([{"date": "2024-01-15", "title": "क"}])

    class _Usage:
        calls = 0

    result = _extract_timeline(
        source_text_="स्रोत पाठ",
        case_title="टेस्ट मुद्दा",
        invoke_with_tools=stub,
        usage=_Usage(),
    )
    assert result == [{"date": "2024-01-15", "title": "क"}]
    assert seen["tier"] == "premium"
    assert seen["max_tokens"] == et.TIMELINE_MAX_TOKENS
    assert seen["max_iterations"] == 30
    assert seen["system"] == et.EXTRACTION_SYSTEM_PROMPT
    assert len(seen["tools"]) == 1
    assert seen["tools"][0].name == "convert_date"


def test_extract_timeline_prompt_includes_title_and_source_text():
    seen = {}

    def stub(**kw):
        seen.update(kw)
        return json.dumps([{"date": "2024-01-15", "title": "क"}])

    _extract_timeline(
        source_text_="काठमाडौं महानगरपालिकाको ठेक्का सम्बन्धी फैसला",
        case_title="काठमाडौं महानगरपालिका मुद्दा",
        invoke_with_tools=stub,
        usage=None,
    )
    assert "काठमाडौं महानगरपालिका मुद्दा" in seen["content"]
    assert "काठमाडौं महानगरपालिकाको ठेक्का सम्बन्धी फैसला" in seen["content"]


def test_extract_timeline_prompt_includes_ngm_section_when_present():
    seen = {}

    def stub(**kw):
        seen.update(kw)
        return json.dumps([{"date": "2024-01-15", "title": "क"}])

    _extract_timeline(
        source_text_="स्रोत",
        case_title="मुद्दा",
        invoke_with_tools=stub,
        usage=None,
        ngm_data={"registration_date_ad": "2024-01-01", "hearings": []},
    )
    assert "NGM STRUCTURED HEARING DATA" in seen["content"]
    assert "2024-01-01" in seen["content"]


# --------------------------------------------------------------------------
# _special_case_number / _get_ngm_data -- donor-faithful colon-prefix
# selection, DEAD ON CURRENT DATA BY DESIGN (see module docstring, concern 2).
#
# Real `court_cases` values are full courtcase IRIs
# (e.g. "https://jawafdehi.org/courtcase/special/081-cr-0091"), never the
# donor's colon-prefixed "special:NNN-CR-NNNN" shape -- measured 0 of 109
# colon-prefixed against the local seeded DB (2026-07-19). This section pins
# that the NGM path is INERT against real full-IRI data, not that it "works":
# a case whose court_cases are full IRIs must get NO NGM section, exactly
# like the donor's own (never-fixed) behavior.
# --------------------------------------------------------------------------


class TestSpecialCaseNumber:
    def test_extracts_ref_from_donor_colon_prefixed_string(self):
        # The ONLY shape the donor's selector ever matched.
        case = {"court_cases": ["special:081-cr-0098"]}
        assert _special_case_number(case) == "081-cr-0098"

    def test_real_full_iri_court_cases_never_match_donor_selector(self):
        # THE central pin for this revert: real court_cases are full IRIs,
        # not colon-prefixed -- the donor's selector must NOT match them.
        case = {"court_cases": ["https://jawafdehi.org/courtcase/special/081-cr-0098"]}
        assert _special_case_number(case) is None

    def test_returns_none_when_no_court_cases_at_all(self):
        assert _special_case_number({}) is None

    def test_ignores_non_string_entries(self):
        case = {"court_cases": [123, None]}
        assert _special_case_number(case) is None


class _NgmStubApi:
    def __init__(self, response=None, fail=False):
        self.response = response
        self.fail = fail
        self.requested_paths = []

    def get(self, path, params=None, timeout=60):
        self.requested_paths.append(path)
        if self.fail:
            raise RuntimeError("NGM endpoint down")
        return self.response


class TestGetNgmData:
    def test_returns_none_without_a_special_court_ref(self):
        api = _NgmStubApi()
        assert _get_ngm_data({}, api) is None
        assert api.requested_paths == []

    def test_real_full_iri_case_gets_no_ngm_section_at_all(self):
        # THE donor-faithful pin required by the revert: a case whose
        # court_cases are full IRIs (the only shape real data ever has) must
        # get NO NGM data -- and must not even attempt an HTTP call, since
        # the donor's own selector never matches it either.
        api = _NgmStubApi(response={"registration_date_ad": "2023-01-01"})
        case = {"court_cases": ["https://jawafdehi.org/courtcase/special/081-cr-0098"]}
        assert _get_ngm_data(case, api) is None
        assert api.requested_paths == []

    def test_donor_colon_prefixed_ref_calls_the_donor_endpoint_and_returns_data(self):
        api = _NgmStubApi(response={
            "registration_date_ad": "2023-01-01", "case_status": "PENDING",
        })
        case = {"court_cases": ["special:081-cr-0098"]}
        data = _get_ngm_data(case, api)
        assert data == {"registration_date_ad": "2023-01-01", "case_status": "PENDING"}
        assert api.requested_paths == ["/ngm/court_case/special:081-cr-0098"]

    def test_query_failure_returns_none(self):
        api = _NgmStubApi(fail=True)
        case = {"court_cases": ["special:081-cr-0098"]}
        assert _get_ngm_data(case, api) is None

    def test_error_shaped_response_returns_none(self):
        api = _NgmStubApi(response={"error": "not found"})
        case = {"court_cases": ["special:081-cr-0098"]}
        assert _get_ngm_data(case, api) is None

    def test_non_dict_response_returns_none(self):
        api = _NgmStubApi(response=["not", "a", "dict"])
        case = {"court_cases": ["special:081-cr-0098"]}
        assert _get_ngm_data(case, api) is None


# --------------------------------------------------------------------------
# main() -- integration over a stubbed API + LLM
# --------------------------------------------------------------------------

CASE_UNCONVERTED = {
    "slug": "case-unconverted",
    "title": "अख्तियारले मुद्दा दायर गर्यो",
    "state": "DRAFT",
    "timeline": [],
    "evidence": [
        {"material_iri": "https://jawafdehi.org/material/ciaa/press_releases/1",
         "material": {"material_type": "press_release", "urls": [
             {"link": "https://x/1.pdf", "role": "RAW"}]}},
    ],
}

CASE_READY_PRESS_ONLY = {
    "slug": "case-press-only",
    "title": "काठमाडौं महानगरपालिका भ्रष्टाचार मुद्दा",
    "state": "DRAFT",
    "timeline": [],
    "evidence": [
        {"material_iri": "https://jawafdehi.org/material/ciaa/press_releases/2",
         "material": {"material_type": "press_release", "urls": [
             {"link": "https://x/2.md", "role": "MARKDOWN"}]}},
    ],
}

CASE_READY_COURT_ONLY = {
    "slug": "case-court-only",
    "title": "विशेष अदालतको फैसला मुद्दा",
    "state": "DRAFT",
    "timeline": [],
    "evidence": [
        {"material_iri": "https://jawafdehi.org/material/ngm/court_orders/1",
         "material": {"material_type": "court_order", "urls": [
             {"link": "https://x/court.md", "role": "MARKDOWN"}]}},
    ],
}

CASE_ALREADY_POPULATED = {
    "slug": "case-populated",
    "title": "पहिल्यै समयरेखा तोकिएको मुद्दा",
    "state": "DRAFT",
    "timeline": [{"date": "2024-01-01", "title": "पुरानो प्रविष्टि"}],
    "evidence": [
        {"material_iri": "https://jawafdehi.org/material/ciaa/press_releases/3",
         "material": {"material_type": "press_release", "urls": [
             {"link": "https://x/3.md", "role": "MARKDOWN"}]}},
    ],
}

CASE_LLM_DECLINES = {
    "slug": "case-declines",
    "title": "अस्पष्ट मुद्दा",
    "state": "DRAFT",
    "timeline": [],
    "evidence": [
        {"material_iri": "https://jawafdehi.org/material/ciaa/press_releases/4",
         "material": {"material_type": "press_release", "urls": [
             {"link": "https://x/4.md", "role": "MARKDOWN"}]}},
    ],
}

# Distinct LIST-shaped vs DETAIL-shaped titles, to pin the donor-preserved
# behavior: the LLM prompt's case_title comes from the LIST case dict
# captured BEFORE the detail fetch, never from the detail response.
CASE_TITLE_DIVERGES_LIST = {
    "slug": "case-title-diverges",
    "title": "सूची शीर्षक",
    "state": "DRAFT",
    "timeline": [],
    "evidence": [
        {"material_iri": "https://jawafdehi.org/material/ciaa/press_releases/5",
         "material": {"material_type": "press_release", "urls": [
             {"link": "https://x/5.md", "role": "MARKDOWN"}]}},
    ],
}
CASE_TITLE_DIVERGES_DETAIL = dict(CASE_TITLE_DIVERGES_LIST, title="विवरण शीर्षक")

# The real LIST endpoint returns `material: null` on every evidence entry
# (only DETAIL resolves it -- see casework/common/materials.py).
CASE_READY_LIST_SHAPE = {
    "slug": "case-press-only",
    "title": "काठमाडौं महानगरपालिका भ्रष्टाचार मुद्दा",
    "state": "DRAFT",
    "timeline": [],
    "evidence": [
        {"material_iri": "https://jawafdehi.org/material/ciaa/press_releases/2",
         "material": None},
    ],
}


class _StubApi:
    def __init__(self, cases, detail_overrides=None, fail_detail_for=(), ngm=None):
        self._cases = {c["slug"]: dict(c) for c in cases}
        self._detail_overrides = detail_overrides or {}
        self._fail_detail_for = set(fail_detail_for)
        self._ngm = ngm or {}
        self.patched = []

    def iter_cases(self, params=None, timeout=60):
        yield from self._cases.values()

    def get_case(self, slug, timeout=60):
        if slug in self._fail_detail_for:
            raise RuntimeError(f"simulated detail-fetch failure for {slug}")
        if slug in self._detail_overrides:
            return self._detail_overrides[slug]
        return self._cases[slug]

    def get(self, path, params=None, timeout=60):
        if path in self._ngm:
            return self._ngm[path]
        raise RuntimeError(f"no NGM stub configured for {path}")

    def patch_field(self, slug, field, value, timeout=60):
        self.patched.append((slug, field, value))
        self._cases[slug][field] = value
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
            "https://x/2.md": "काठमाडौं महानगरपालिकाको ठेक्कामा भ्रष्टाचार भएको छ ।",
            "https://x/3.md": "पहिल्यै प्रविष्टि भएको मुद्दा।",
            "https://x/4.md": "अस्पष्ट प्रेस विज्ञप्ति।",
            "https://x/5.md": "प्रेस विज्ञप्ति सामग्री।",
            "https://x/court.md": "विशेष अदालतको फैसला। प्रतिवादीलाई दोषी ठहर गरियो।",
        }.get(link, "")

    monkeypatch.setattr(m, "fetch_markdown", fake_fetch)


def _run_main(monkeypatch, api, invoke_text_stub, invoke_with_tools_stub, argv):
    """Drive `main()` end to end with a stubbed API and stubbed LLM calls.

    `invoke_text`/`invoke_with_tools` and `UsageAccumulator` are imported
    INSIDE `main()` (after bootstrap), so they're faked out via `sys.modules`
    rather than `monkeypatch.setattr(et, ...)` -- mirrors
    test_enrich_allegations.py/test_enrich_missing_bigo.py.
    """
    monkeypatch.setattr(et, "build_api", lambda args: api)
    monkeypatch.setattr(et, "bootstrap", lambda *a, **k: None)

    fake_llm_invoke = types.ModuleType("llm.invoke")
    fake_llm_invoke.invoke_text = invoke_text_stub
    fake_llm_invoke.invoke_with_tools = invoke_with_tools_stub

    fake_llm_usage = types.ModuleType("llm.usage")
    fake_llm_usage.UsageAccumulator = _FakeUsage
    fake_llm_usage.render_usage_table = lambda by_provider, title=None: ""

    monkeypatch.setitem(sys.modules, "llm.invoke", fake_llm_invoke)
    monkeypatch.setitem(sys.modules, "llm.usage", fake_llm_usage)

    return et.main(argv)


def _call_tracking_tools_stub(response=None):
    """A stub that records invocations instead of raising.

    IMPORTANT: `enrich_timeline.main()` wraps the LLM call in a narrow
    `except Exception` around `_extract_timeline` only -- an "LLM must not be
    called" assertion MUST check `stub.calls == []` explicitly rather than
    relying on a raise to propagate as a test failure, since a raise from a
    case that legitimately reaches the LLM call would be swallowed and
    counted as an "error" status instead of failing the test loudly.
    """
    if response is None:
        response = json.dumps([{"date": "2024-01-15", "title": "क"}])
    calls = []

    def stub(**kw):
        calls.append(kw)
        return response

    stub.calls = calls
    return stub


def test_unmet_prerequisite_is_recorded_not_silently_skipped(
    monkeypatch, patched_fetch_markdown
):
    api = _StubApi([CASE_UNCONVERTED])
    tools_stub = _call_tracking_tools_stub()
    report = _run_main(
        monkeypatch, api, invoke_text_stub=lambda **kw: "",
        invoke_with_tools_stub=tools_stub, argv=["--dry-run"],
    )
    assert report.rows[0]["status"] == "unmet"
    assert report.rows[0]["reason"]
    assert tools_stub.calls == []


def test_already_populated_case_is_skipped_without_calling_llm(
    monkeypatch, patched_fetch_markdown
):
    api = _StubApi([CASE_ALREADY_POPULATED])
    tools_stub = _call_tracking_tools_stub()
    report = _run_main(
        monkeypatch, api, invoke_text_stub=lambda **kw: "",
        invoke_with_tools_stub=tools_stub, argv=["--dry-run"],
    )
    assert report.rows[0]["status"] == "already"
    assert api.patched == []
    assert tools_stub.calls == []


def test_force_reruns_an_already_populated_case(monkeypatch, patched_fetch_markdown):
    response = json.dumps([{"date": "2024-02-01", "title": "नयाँ प्रविष्टि"}])
    api = _StubApi([CASE_ALREADY_POPULATED])
    report = _run_main(
        monkeypatch, api, invoke_text_stub=lambda **kw: "",
        invoke_with_tools_stub=lambda **kw: response,
        argv=["--force", "--apply"],
    )
    assert report.rows[0]["status"] == "enriched"
    assert api.patched == [
        ("case-populated", "timeline", [{"date": "2024-02-01", "title": "नयाँ प्रविष्टि"}])]


def test_dry_run_extracts_but_does_not_patch(monkeypatch, patched_fetch_markdown):
    response = json.dumps([{"date": "2024-01-15", "title": "क"}])
    api = _StubApi([CASE_READY_PRESS_ONLY])
    report = _run_main(
        monkeypatch, api, invoke_text_stub=lambda **kw: "",
        invoke_with_tools_stub=lambda **kw: response, argv=["--dry-run"],
    )
    assert report.rows[0]["status"] == "would-enrich"
    assert api.patched == []


def test_apply_patches_timeline_field_only(monkeypatch, patched_fetch_markdown):
    response = json.dumps([{"date": "2024-01-15", "title": "क"}])
    api = _StubApi([CASE_READY_PRESS_ONLY, CASE_ALREADY_POPULATED])
    _run_main(
        monkeypatch, api, invoke_text_stub=lambda **kw: "",
        invoke_with_tools_stub=lambda **kw: response,
        argv=["--force", "--apply"],
    )
    fields = {field for _, field, _ in api.patched}
    assert fields == {"timeline"}


def test_court_only_case_is_processed_via_the_court_types_bucket(
    monkeypatch, patched_fetch_markdown
):
    seen = {}

    def tools_stub(**kw):
        seen.update(kw)
        return json.dumps([{"date": "2024-01-15", "title": "फैसला"}])

    api = _StubApi([CASE_READY_COURT_ONLY])
    report = _run_main(
        monkeypatch, api, invoke_text_stub=lambda **kw: "",
        invoke_with_tools_stub=tools_stub, argv=["--apply"],
    )
    assert report.rows[0]["status"] == "enriched"
    assert "फैसला" in seen["content"]


def test_llm_declining_is_recorded_as_skipped_not_enriched(
    monkeypatch, patched_fetch_markdown
):
    # LLM returns an empty JSON array -- parse_extraction_response finds
    # nothing usable, and the case must be recorded as "skipped", never
    # silently treated as "enriched" with an empty list.
    response = json.dumps([])
    api = _StubApi([CASE_LLM_DECLINES])
    report = _run_main(
        monkeypatch, api, invoke_text_stub=lambda **kw: "",
        invoke_with_tools_stub=lambda **kw: response, argv=["--apply"],
    )
    assert report.rows[0]["status"] == "skipped"
    assert api.patched == []


def test_llm_extraction_failure_is_recorded_as_error_not_enriched(
    monkeypatch, patched_fetch_markdown
):
    def tools_stub(**kw):
        raise RuntimeError("LLM provider unavailable")

    api = _StubApi([CASE_READY_PRESS_ONLY])
    report = _run_main(
        monkeypatch, api, invoke_text_stub=lambda **kw: "",
        invoke_with_tools_stub=tools_stub, argv=["--apply"],
    )
    assert report.rows[0]["status"] == "error"
    assert api.patched == []


def test_llm_invoked_with_premium_tier_end_to_end(monkeypatch, patched_fetch_markdown):
    seen_tiers = []

    def tools_stub(**kw):
        seen_tiers.append(kw.get("tier"))
        return json.dumps([{"date": "2024-01-15", "title": "क"}])

    api = _StubApi([CASE_READY_PRESS_ONLY])
    _run_main(
        monkeypatch, api, invoke_text_stub=lambda **kw: "",
        invoke_with_tools_stub=tools_stub, argv=["--apply"],
    )
    assert seen_tiers == ["premium"]


def test_detail_fetch_failure_falls_back_to_summary_case_not_a_crash(
    monkeypatch, patched_fetch_markdown
):
    api = _StubApi([CASE_READY_LIST_SHAPE], fail_detail_for={"case-press-only"})
    tools_stub = _call_tracking_tools_stub()
    report = _run_main(
        monkeypatch, api, invoke_text_stub=lambda **kw: "",
        invoke_with_tools_stub=tools_stub, argv=["--dry-run"],
    )
    assert report.rows[0]["status"] == "unmet"
    assert tools_stub.calls == []
    assert api.patched == []


def test_prompt_case_title_comes_from_list_case_not_detail(
    monkeypatch, patched_fetch_markdown
):
    seen = {}

    def tools_stub(**kw):
        seen.update(kw)
        return json.dumps([{"date": "2024-01-15", "title": "क"}])

    api = _StubApi(
        [CASE_TITLE_DIVERGES_LIST],
        detail_overrides={"case-title-diverges": CASE_TITLE_DIVERGES_DETAIL},
    )
    _run_main(
        monkeypatch, api, invoke_text_stub=lambda **kw: "",
        invoke_with_tools_stub=tools_stub, argv=["--apply"],
    )
    assert "सूची शीर्षक" in seen["content"]
    assert "विवरण शीर्षक" not in seen["content"]


def test_entries_are_sorted_chronologically_before_patch(
    monkeypatch, patched_fetch_markdown
):
    response = json.dumps([
        {"date": "2024-06-01", "title": "पछिल्लो"},
        {"date": "2020-01-01", "title": "पहिलो"},
    ])
    api = _StubApi([CASE_READY_PRESS_ONLY])
    _run_main(
        monkeypatch, api, invoke_text_stub=lambda **kw: "",
        invoke_with_tools_stub=lambda **kw: response, argv=["--apply"],
    )
    _, _, entries = api.patched[0]
    assert [e["date"] for e in entries] == ["2020-01-01", "2024-06-01"]


# --------------------------------------------------------------------------
# Task PP2 -- run-logging events file (see test_enrich_missing_bigo.py's
# identical block for the rationale; `conftest.py`'s autouse
# `_isolate_casework_run_logs` fixture keeps these out of the real repo
# `work/enricher-runs/`).
# --------------------------------------------------------------------------


def _events_path():
    logger = logging.getLogger("casework.timeline")
    return logger._casework_run_paths["events"]


def _read_events(path):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines()]


def test_events_file_covers_start_extract_write_on_apply_happy_path(
    monkeypatch, patched_fetch_markdown, tmp_path
):
    response = json.dumps([{"date": "2024-01-15", "title": "क"}])
    api = _StubApi([CASE_READY_PRESS_ONLY])
    _run_main(
        monkeypatch, api, invoke_text_stub=lambda **kw: "",
        invoke_with_tools_stub=lambda **kw: response, argv=["--apply"],
    )

    rows = _read_events(_events_path())
    assert rows

    required_keys = {"ts", "run_id", "stage", "slug", "step", "status", "detail", "elapsed_ms"}
    for row in rows:
        assert required_keys <= set(row.keys())
        assert row["stage"] == "timeline"

    steps_and_statuses = {(r["step"], r["status"]) for r in rows}
    assert ("start", "start") in steps_and_statuses
    assert ("extract", "ok") in steps_and_statuses
    assert ("write", "enriched") in steps_and_statuses


def test_events_file_records_would_enrich_under_dry_run(
    monkeypatch, patched_fetch_markdown, tmp_path
):
    response = json.dumps([{"date": "2024-01-15", "title": "क"}])
    api = _StubApi([CASE_READY_PRESS_ONLY])
    _run_main(
        monkeypatch, api, invoke_text_stub=lambda **kw: "",
        invoke_with_tools_stub=lambda **kw: response, argv=["--dry-run"],
    )

    rows = _read_events(_events_path())
    steps_and_statuses = {(r["step"], r["status"]) for r in rows}
    assert ("write", "would-enrich") in steps_and_statuses
    assert ("write", "enriched") not in steps_and_statuses
