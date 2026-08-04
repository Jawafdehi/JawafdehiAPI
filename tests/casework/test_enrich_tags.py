"""Tests for the DB-free standalone tags enricher (casework/enrich_tags.py).

`enrich_tags.py` is rules-first: `classify_case_rules` runs unconditionally from
case METADATA ONLY (title/key_allegations/court_cases/description) -- unlike
`enrich_missing_bigo.py` or the other three ported enrichers, it never touches
`evidence`/materials at all, and `bigo` is optional context rather than a gate
(`_detect_amount_tier` returns `None`, not an error, when `bigo` is `None`).
The LLM only tops up when the rule pass produced fewer than 5 tags, at the
CHEAP tier (the only one of the five ported enrichers that is not "premium"),
`max_tokens=256`. A hard floor of `["CIAA", "Corruption"]` is guaranteed by
`merge_tags` even if everything else is filtered out by `validate_tags`.

The `TestDonorFidelity` class below re-derives the controlled tag vocabulary
and keyword maps directly from the donor at commit `0321a85` (via `git show`
+ `ast.literal_eval`, not by trusting this file's own transcription) and
asserts byte-identical equality against this module's constants -- an
invented or reordered vocabulary would silently change classifications
without failing any test that only spot-checks a few tag names.
"""
import ast
import json
import logging
import subprocess
import sys
import types
from pathlib import Path

import pytest

from casework import enrich_tags as et
from casework.enrich_tags import (
    build_llm_classification_prompt,
    classify_case_rules,
    merge_tags,
    parse_llm_response,
    validate_tags,
)
from tests.casework.fakes import FakeUsage

REPO_ROOT = Path(__file__).resolve().parents[2]
DONOR_COMMIT = "0321a85"


def _donor_source() -> str:
    proc = subprocess.run(
        ["git", "show", f"{DONOR_COMMIT}:casework/enrich_tags.py"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        pytest.skip(
            f"donor commit {DONOR_COMMIT} not in local history "
            "(shallow clone?); fidelity check needs full git history")
    return proc.stdout


def _donor_constants() -> dict:
    """Extract top-level constant assignments from the donor source via AST
    (never `exec`/`import` it -- the donor's own imports no longer resolve
    against the refactored `casework.common` package)."""
    wanted = {
        "SECTOR_TAGS", "CORRUPTION_TYPE_TAGS", "REGION_TAGS", "CONTEXT_TAGS",
        "SECTOR_KEYWORDS", "CORRUPTION_TYPE_KEYWORDS", "REGION_KEYWORDS",
    }
    tree = ast.parse(_donor_source())
    found = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and target.id in wanted:
                found[target.id] = ast.literal_eval(node.value)
    return found


@pytest.fixture(scope="module")
def donor():
    return _donor_constants()


class TestDonorFidelity:
    """Byte-for-byte pins against the donor at commit 0321a85 -- NOT this
    module's own transcription. A single altered/dropped/reordered keyword
    or tag name would flip real classifications with no other test noticing."""

    def test_sector_tags_match_donor(self, donor):
        assert et.SECTOR_TAGS == donor["SECTOR_TAGS"]

    def test_corruption_type_tags_match_donor(self, donor):
        assert et.CORRUPTION_TYPE_TAGS == donor["CORRUPTION_TYPE_TAGS"]

    def test_region_tags_match_donor(self, donor):
        assert et.REGION_TAGS == donor["REGION_TAGS"]

    def test_context_tags_match_donor(self, donor):
        assert et.CONTEXT_TAGS == donor["CONTEXT_TAGS"]

    def test_sector_keywords_match_donor(self, donor):
        assert et.SECTOR_KEYWORDS == donor["SECTOR_KEYWORDS"]

    def test_corruption_type_keywords_match_donor(self, donor):
        assert et.CORRUPTION_TYPE_KEYWORDS == donor["CORRUPTION_TYPE_KEYWORDS"]

    def test_region_keywords_match_donor(self, donor):
        assert et.REGION_KEYWORDS == donor["REGION_KEYWORDS"]

    def test_valid_tags_frozenset_is_union_of_the_four_lists(self, donor):
        expected = frozenset(
            donor["SECTOR_TAGS"] + donor["CORRUPTION_TYPE_TAGS"]
            + donor["REGION_TAGS"] + donor["CONTEXT_TAGS"]
        )
        assert et._VALID_TAGS == expected


# --------------------------------------------------------------------------
# classify_case_rules -- brief's own suggested test used an invented
# `source_text=` kwarg that the donor's `classify_case_rules(case)` does not
# accept (single positional `case` dict only -- see task-14a-report.md).
# These use the real donor signature.
# --------------------------------------------------------------------------


def test_amount_tier_requires_bigo():
    tags = classify_case_rules({"slug": "x", "bigo": None})
    assert not any(t.lower().startswith("amount") for t in tags)
    assert not any(t.startswith("~") or t == "Under 1 Hazar" for t in tags)


def test_amount_tier_present_when_bigo_given():
    tags = classify_case_rules({"slug": "x", "bigo": 50_000})
    assert "~50 Hazar" in tags


def test_ciaa_and_corruption_always_included_even_for_a_bare_case():
    tags = classify_case_rules({"slug": "x"})
    assert set(tags) >= {"CIAA", "Corruption"}


def test_sector_keyword_match_from_title():
    tags = classify_case_rules({"title": "Kathmandu Municipality land scam"})
    assert "Local Government" in tags


def test_corruption_type_keyword_match_from_title():
    tags = classify_case_rules({"title": "Bribery case against ward chair"})
    assert "Bribery" in tags


def test_region_keyword_match_from_title():
    tags = classify_case_rules({"title": "Pokhara municipal contract fraud"})
    assert "Gandaki" in tags


def test_sector_matches_capped_at_three():
    # health + education + finance + agriculture -> 4 sector hits, capped to 3
    case = {"title": "health education finance agriculture scandal"}
    tags = classify_case_rules(case)
    sector_hits = [t for t in tags if t in et.SECTOR_TAGS]
    assert len(sector_hits) == 3


def test_corruption_type_matches_capped_at_three():
    case = {"title": "bribery embezzlement forgery nepotism case"}
    tags = classify_case_rules(case)
    hits = [t for t in tags if t in et.CORRUPTION_TYPE_TAGS]
    assert len(hits) == 3


def test_region_matches_capped_at_two():
    case = {"title": "Kathmandu Pokhara Janakpur corruption ring"}
    tags = classify_case_rules(case)
    hits = [t for t in tags if t in et.REGION_TAGS]
    assert len(hits) == 2


def test_keyword_matching_is_case_insensitive():
    tags_upper = classify_case_rules({"title": "BRIBERY case"})
    tags_lower = classify_case_rules({"title": "bribery case"})
    assert "Bribery" in tags_upper
    assert "Bribery" in tags_lower


def test_devanagari_keyword_matches_as_a_substring_not_a_word_boundary():
    # Non-ASCII keywords are matched as bare substrings (`kw in text`), NOT
    # through the `\b...\w*\b` regex used for ASCII keywords. This matters
    # specifically when the keyword is embedded MID-word (not merely as a
    # word PREFIX, which a trailing `\w*` would also satisfy under the regex
    # path -- a prefix-only fixture does not discriminate the two
    # approaches; verified by mutation, see task-14a-report.md). "बैंक"
    # (bank) embedded inside "राजबैंकबाट" (no word boundary immediately
    # before "बैंक") matches under substring containment but would NOT match
    # `\bबैंक\w*\b`, since `\b` requires a non-word character immediately
    # before the "ब". This is the donor's actual (permissive) behavior for
    # non-ASCII keywords, not an invented stricter rule.
    case = {"title": "राजबैंकबाट रकम अपचलन भयो"}
    tags = classify_case_rules(case)
    assert "Banking" in tags


def test_key_allegations_and_description_feed_keyword_matching():
    case = {"key_allegations": ["Took a bribe from a contractor"]}
    assert "Bribery" in classify_case_rules(case)
    case2 = {"description": "Illegal property acquisition scheme"}
    assert "Illegal Property Acquisition" in classify_case_rules(case2)


def test_court_cases_string_entries_feed_keyword_matching():
    case = {"court_cases": ["Bribery scandal reference"]}
    assert "Bribery" in classify_case_rules(case)


def test_non_string_court_cases_entries_are_ignored_not_fatal():
    case = {"court_cases": [{"not": "a string"}], "title": ""}
    # Must not raise -- classify_case_rules still returns the CIAA/Corruption
    # floor tags for a case whose court_cases entries are malformed.
    tags = classify_case_rules(case)
    assert set(tags) >= {"CIAA", "Corruption"}


def test_rule_tags_are_deduplicated_preserving_first_occurrence_order():
    # "bribery" appears in both the title and an allegation; classify_case_rules
    # must not double it.
    case = {
        "title": "Bribery case",
        "key_allegations": ["bribery of an officer"],
    }
    tags = classify_case_rules(case)
    assert tags.count("Bribery") == 1


# --------------------------------------------------------------------------
# _detect_court_context -- donor concern (see module docstring / report):
# the "special:"/"supreme:" colon-prefix check appears stale against the
# `/courtcase/<court>/<number>` IRI shape used elsewhere in this pipeline
# (`casework.common.select`). Pinned here exactly as ported: the donor's
# literal check, not a guessed fix.
# --------------------------------------------------------------------------


def test_special_court_tag_fires_on_donors_colon_prefix_form():
    case = {"court_cases": ["special:081-cr-0098"]}
    assert "Special Court" in classify_case_rules(case)


def test_supreme_court_tag_fires_on_donors_colon_prefix_form():
    case = {"court_cases": ["supreme:081-cr-0098"]}
    assert "Supreme Court" in classify_case_rules(case)


def test_special_court_tag_does_not_fire_on_the_current_iri_shape():
    # Pins the concern: real court_cases entries in this project's OWN
    # fixtures (e.g. test_enrich_missing_bigo.py's PRESS_CASE_READY) look
    # like "https://jawafdehi.org/courtcase/special/081-cr-0098", which does
    # NOT start with "special:" -- so this ported-verbatim donor check does
    # not tag it "Special Court" even though it plainly IS a special court
    # case. CIAA/Corruption are unaffected (unconditional).
    case = {"court_cases": ["https://jawafdehi.org/courtcase/special/081-cr-0098"]}
    tags = classify_case_rules(case)
    assert "Special Court" not in tags
    assert set(tags) >= {"CIAA", "Corruption"}


# --------------------------------------------------------------------------
# _detect_amount_tier
# --------------------------------------------------------------------------


class TestDetectAmountTier:
    def test_none_yields_none(self):
        assert et._detect_amount_tier(None) is None

    def test_zero_yields_under_1_hazar(self):
        # Only `bigo is None` is special-cased -- 0 is falsy but not None,
        # so it still renders a tier string rather than being treated as
        # "no bigo".
        assert et._detect_amount_tier(0) == "Under 1 Hazar"

    def test_small_amount_yields_under_1_hazar(self):
        assert et._detect_amount_tier(500) == "Under 1 Hazar"

    def test_hazar_tier(self):
        assert et._detect_amount_tier(50_000) == "~50 Hazar"

    def test_lakh_and_hazar_tier(self):
        assert et._detect_amount_tier(450_000) == "~4 Lakh 50 Hazar"

    def test_crore_and_lakh_tier_hazar_suppressed(self):
        assert et._detect_amount_tier(45_000_000) == "~4 Crore 50 Lakh"

    def test_arab_and_crore_tier_lakh_and_hazar_suppressed(self):
        assert et._detect_amount_tier(1_470_000_000) == "~1 Arab 47 Crore"

    def test_string_bigo_is_coerced_not_crashed(self):
        # DRF serialises a DecimalField to a string by default, so the case API
        # can hand bigo back as "1470000000" / "5000000000.00", not an int. The
        # "never raises" contract must hold: coerce, don't crash on `//`.
        assert et._detect_amount_tier("1470000000") == "~1 Arab 47 Crore"
        assert et._detect_amount_tier("5000000000.00") == et._detect_amount_tier(5_000_000_000)
        assert et._detect_amount_tier(4.5e7) == "~4 Crore 50 Lakh"

    def test_uncoercible_bigo_returns_none(self):
        assert et._detect_amount_tier("not-a-number") is None
        assert et._detect_amount_tier("") is None


# --------------------------------------------------------------------------
# validate_tags
# --------------------------------------------------------------------------


class TestValidateTags:
    def test_invalid_tag_filtered_out(self):
        assert validate_tags(["Not A Real Tag", "CIAA"]) == ["CIAA"]

    def test_amount_tier_tag_always_passes_through(self):
        assert validate_tags(["~4 Crore 50 Lakh"]) == ["~4 Crore 50 Lakh"]

    def test_under_1_hazar_passes_through(self):
        assert validate_tags(["Under 1 Hazar"]) == ["Under 1 Hazar"]

    def test_dedup_preserves_first_occurrence(self):
        assert validate_tags(["CIAA", "Corruption", "CIAA"]) == ["CIAA", "Corruption"]

    def test_empty_in_empty_out(self):
        assert validate_tags([]) == []


# --------------------------------------------------------------------------
# merge_tags -- brief's own suggested tests, using the donor-derived floor
# and dedup semantics (see enrich_tags.merge_tags docstring).
# --------------------------------------------------------------------------


class TestMergeTags:
    def test_hard_floor_tags_always_present(self):
        assert set(merge_tags([], [])) >= {"CIAA", "Corruption"}

    def test_rule_tags_are_deduplicated(self):
        assert merge_tags(["CIAA", "Bribery"], ["Bribery"]).count("Bribery") == 1

    def test_rule_tags_ordered_before_new_llm_tags(self):
        merged = merge_tags(["CIAA", "Corruption"], ["Bribery", "Kathmandu"])
        assert merged == ["CIAA", "Corruption", "Bribery", "Kathmandu"]

    def test_invalid_llm_tags_are_filtered_out(self):
        merged = merge_tags(["CIAA", "Corruption"], ["Not A Real Tag"])
        assert merged == ["CIAA", "Corruption"]

    def test_floor_applied_when_everything_is_invalid(self):
        merged = merge_tags(["Bogus"], ["AlsoBogus"])
        assert merged == ["CIAA", "Corruption"]

    def test_amount_tier_tag_survives_merge(self):
        merged = merge_tags(["CIAA", "Corruption", "~50 Hazar"], [])
        assert "~50 Hazar" in merged


# --------------------------------------------------------------------------
# parse_llm_response
# --------------------------------------------------------------------------


class TestParseLlmResponse:
    def test_parses_plain_json_array(self):
        body = '["CIAA", "Corruption", "Bribery"]'
        assert parse_llm_response(body) == ["CIAA", "Corruption", "Bribery"]

    def test_parses_fenced_json_array(self):
        body = '```json\n["CIAA", "Corruption"]\n```'
        assert parse_llm_response(body) == ["CIAA", "Corruption"]

    def test_returns_empty_list_when_no_array_found(self):
        assert parse_llm_response("no brackets here at all") == []

    def test_rejects_array_with_non_string_elements(self):
        # `[1, 2, 3]` matches the bracket regex and parses as valid JSON, but
        # fails the `all(isinstance(t, str))` guard, so it must not be
        # returned; falling through leaves no other match -> [].
        assert parse_llm_response("[1, 2, 3]") == []

    def test_falls_through_to_a_later_valid_match(self):
        # The first bracket-looking span is a non-list-of-strings JSON array;
        # the second is a valid tag array. finditer must not stop at the
        # first (invalid) match.
        body = "ignore [1, 2, 3] but use [\"CIAA\", \"Corruption\"]"
        assert parse_llm_response(body) == ["CIAA", "Corruption"]

    def test_strips_leading_fence_without_language_tag(self):
        body = '```\n["CIAA"]\n```'
        assert parse_llm_response(body) == ["CIAA"]


# --------------------------------------------------------------------------
# build_llm_classification_prompt
# --------------------------------------------------------------------------


class TestBuildLlmClassificationPrompt:
    def test_includes_bigo_when_present(self):
        prompt = build_llm_classification_prompt({"title": "x", "bigo": 5_000_000})
        assert "NPR 5,000,000" in prompt

    def test_omits_bigo_when_none(self):
        prompt = build_llm_classification_prompt({"title": "x", "bigo": None})
        assert "Bigo" not in prompt

    def test_omits_bigo_when_absent(self):
        prompt = build_llm_classification_prompt({"title": "x"})
        assert "Bigo" not in prompt

    def test_lists_key_allegations(self):
        prompt = build_llm_classification_prompt(
            {"title": "x", "key_allegations": ["अभियोग एक", "अभियोग दुई"]})
        assert "अभियोग एक" in prompt
        assert "अभियोग दुई" in prompt

    def test_includes_selection_instructions_and_floor(self):
        prompt = build_llm_classification_prompt({"title": "x"})
        assert "Always include: CIAA, Corruption" in prompt
        assert "Return ONLY a JSON array" in prompt


# --------------------------------------------------------------------------
# main() -- integration over a stubbed API + LLM
# --------------------------------------------------------------------------

# len(rule_tags) == 5: Local Government (municipality) + Bribery (bribe) +
# Kathmandu (region) + CIAA + Corruption -- NOT < 5, so the LLM must not be
# invoked at all.
CASE_FIVE_RULE_TAGS = {
    "slug": "case-five-rule-tags",
    "title": "Kathmandu municipality officer took a bribe",
    "state": "DRAFT",
    "bigo": None,
    "tags": [],
    "court_cases": [],
    "key_allegations": [],
    "description": "",
}

# No keyword hits at all -> rule_tags == ["CIAA", "Corruption"] (len 2 < 5),
# so the LLM top-up attempt must fire.
CASE_NEEDS_LLM_TOPUP = {
    "slug": "case-needs-llm",
    "title": "अख्तियारले मुद्दा दायर गर्यो",
    "state": "DRAFT",
    "bigo": None,
    "tags": [],
    "court_cases": [],
    "key_allegations": [],
    "description": "",
}

CASE_ALREADY_TAGGED = {
    "slug": "case-already-tagged",
    "title": "पहिल्यै ट्याग गरिएको मुद्दा",
    "state": "DRAFT",
    "bigo": None,
    "tags": ["CIAA", "Corruption"],
    "court_cases": [],
    "key_allegations": [],
    "description": "",
}

CASE_NO_EVIDENCE_AT_ALL = {
    "slug": "case-no-evidence",
    "title": "कुनै प्रमाण नभएको मुद्दा",
    "state": "DRAFT",
    "bigo": None,
    "tags": [],
    "court_cases": [],
    "key_allegations": [],
    "description": "",
    "evidence": [],
}


class _StubApi:
    def __init__(self, cases):
        # Shallow-copy so `patch_field` mutations to one test's fixture dict
        # never leak into a later test that reuses the same module-level
        # object (see test_enrich_missing_bigo.py's identical rationale).
        self._cases = {c["slug"]: dict(c) for c in cases}
        self.patched = []

    def iter_cases(self, params=None, timeout=60):
        yield from self._cases.values()

    def get_case(self, slug, timeout=60):
        return self._cases[slug]

    def patch_field(self, slug, field, value, timeout=60):
        self.patched.append((slug, field, value))
        self._cases[slug][field] = value
        return {}


def _run_main(monkeypatch, cases, invoke_text_stub, argv):
    """Drive `main()` end to end with a stubbed API and a stubbed LLM call.

    Mirrors `test_enrich_missing_bigo.py::_run_main`: `invoke_text` and
    `UsageAccumulator` are imported INSIDE `main()` (after bootstrap), so
    they're fake via `sys.modules`.
    """
    api = _StubApi(cases)
    monkeypatch.setattr(et, "build_api", lambda args: api)
    monkeypatch.setattr(et, "bootstrap", lambda *a, **k: None)

    fake_llm_invoke = types.ModuleType("llm.invoke")
    fake_llm_invoke.invoke_text = invoke_text_stub

    fake_llm_usage = types.ModuleType("llm.usage")
    fake_llm_usage.UsageAccumulator = FakeUsage
    fake_llm_usage.render_usage_table = lambda by_provider, title=None: ""

    monkeypatch.setitem(sys.modules, "llm.invoke", fake_llm_invoke)
    monkeypatch.setitem(sys.modules, "llm.usage", fake_llm_usage)

    report = et.main(argv)
    return api, report


def _call_tracking_stub(response="[]"):
    """A stub that records invocations instead of raising.

    IMPORTANT: `enrich_tags.main()` deliberately wraps the LLM call in a
    broad `except Exception` (an LLM failure must not abort the case -- see
    module docstring). A stub that `raise`s to assert "must not be called"
    is therefore USELESS here: the exception gets silently caught and
    counted as `"llm-error"`, the test never sees it, and the run proceeds
    exactly as if the call had legitimately failed and fallen back. Every
    "LLM must not be called" assertion in this file MUST check
    `stub.calls == []` explicitly instead of relying on a raise to
    propagate -- this was caught by mutation testing (see task-14a-report.md
    -- removing the `len(rule_tags) < 5` guard entirely still passed the
    original raise-based test).
    """
    calls = []

    def stub(**kw):
        calls.append(kw)
        return response

    stub.calls = calls
    return stub


def test_already_tagged_case_is_skipped_without_calling_llm(monkeypatch):
    stub = _call_tracking_stub()
    api, report = _run_main(
        monkeypatch, [CASE_ALREADY_TAGGED],
        invoke_text_stub=stub, argv=["--dry-run"],
    )
    assert report.rows[0]["status"] == "already"
    assert api.patched == []
    assert stub.calls == []


def test_force_reruns_an_already_tagged_case(monkeypatch):
    response = json.dumps(["CIAA", "Corruption", "Bribery"])
    api, report = _run_main(
        monkeypatch, [CASE_ALREADY_TAGGED],
        invoke_text_stub=lambda **kw: response, argv=["--force", "--apply"],
    )
    assert report.rows[-1]["status"] == "enriched"
    assert api.patched


def test_five_or_more_rule_tags_skips_llm_entirely(monkeypatch):
    stub = _call_tracking_stub()
    api, report = _run_main(
        monkeypatch, [CASE_FIVE_RULE_TAGS],
        invoke_text_stub=stub, argv=["--apply"],
    )
    assert report.rows[-1]["status"] == "enriched"
    slug, field, value = api.patched[0]
    assert field == "tags"
    assert set(value) >= {"CIAA", "Corruption", "Local Government", "Bribery", "Kathmandu"}
    assert stub.calls == []


def test_llm_tops_up_when_fewer_than_five_rule_tags(monkeypatch):
    response = json.dumps(["CIAA", "Corruption", "Health", "Bagmati"])
    seen = {}

    def stub(**kw):
        seen.update(kw)
        return response

    api, report = _run_main(
        monkeypatch, [CASE_NEEDS_LLM_TOPUP],
        invoke_text_stub=stub, argv=["--apply"],
    )
    assert report.rows[-1]["status"] == "enriched"
    _, _, value = api.patched[0]
    assert "Health" in value
    assert "Bagmati" in value
    # Rules-first ordering: CIAA/Corruption (from rules) still precede the
    # LLM-only additions in the merged list.
    assert value.index("CIAA") < value.index("Health")
    # Tier bookkeeping: the LLM genuinely contributed new tags, so the run
    # report must say so (not silently stay "rule_based").
    assert "tier=metadata_llm" in report.rows[-1]["reason"]


def test_llm_invoked_with_cheap_tier_and_256_max_tokens(monkeypatch):
    """Pins the donor's `tier="cheap"`, `max_tokens=256` (enrich_tags.py:1087-1088)
    -- the only one of the five ported enrichers NOT on the premium tier."""
    seen = {}

    def stub(**kw):
        seen.update(kw)
        return json.dumps(["CIAA", "Corruption"])

    _run_main(monkeypatch, [CASE_NEEDS_LLM_TOPUP], invoke_text_stub=stub, argv=["--apply"])
    assert seen["tier"] == "cheap"
    assert seen["max_tokens"] == 256


def test_llm_failure_falls_back_to_rule_tags_and_case_is_still_enriched(monkeypatch):
    """The donor's `stats["cases_llm_error"]` is tracked independently of
    `stats["cases_enriched"]` -- an LLM exception must NOT abort the case;
    it must still be classified (rule-only) and PATCHed."""
    def stub(**kw):
        raise RuntimeError("LLM provider unavailable")

    api, report = _run_main(
        monkeypatch, [CASE_NEEDS_LLM_TOPUP], invoke_text_stub=stub, argv=["--apply"],
    )
    statuses = [r["status"] for r in report.rows]
    assert "llm-error" in statuses
    assert "enriched" in statuses
    assert api.patched, "case must still be PATCHed with rule-only tags"
    _, _, value = api.patched[0]
    assert set(value) >= {"CIAA", "Corruption"}


def test_llm_returning_no_new_tags_keeps_tier_rule_based(monkeypatch):
    # LLM returns tags that are already all present in rule_tags -> no new
    # tags -> tier stays "rule_based", not "metadata_llm".
    response = json.dumps(["CIAA", "Corruption"])
    api, report = _run_main(
        monkeypatch, [CASE_NEEDS_LLM_TOPUP],
        invoke_text_stub=lambda **kw: response, argv=["--apply"],
    )
    assert "tier=rule_based" in report.rows[-1]["reason"]


def test_no_llm_flag_skips_llm_even_with_few_rule_tags(monkeypatch):
    stub = _call_tracking_stub()
    api, report = _run_main(
        monkeypatch, [CASE_NEEDS_LLM_TOPUP],
        invoke_text_stub=stub, argv=["--no-llm", "--apply"],
    )
    assert report.rows[-1]["status"] == "enriched"
    assert stub.calls == []


def test_dry_run_does_not_patch(monkeypatch):
    response = json.dumps(["CIAA", "Corruption", "Health"])
    api, report = _run_main(
        monkeypatch, [CASE_NEEDS_LLM_TOPUP],
        invoke_text_stub=lambda **kw: response, argv=["--dry-run"],
    )
    assert report.rows[-1]["status"] == "would-enrich"
    assert api.patched == []


def test_apply_patches_the_final_tags(monkeypatch):
    response = json.dumps(["CIAA", "Corruption", "Health"])
    api, report = _run_main(
        monkeypatch, [CASE_NEEDS_LLM_TOPUP],
        invoke_text_stub=lambda **kw: response, argv=["--apply"],
    )
    assert report.rows[-1]["status"] == "enriched"
    assert api.patched
    slug, field, value = api.patched[0]
    assert slug == "case-needs-llm"
    assert field == "tags"
    assert "Health" in value


def test_case_with_no_evidence_at_all_is_still_processed_no_unmet(monkeypatch):
    # tags requires no material -- unmet_prerequisites(STAGE, case) must be
    # [] even for a case with zero bound evidence, so it must not be
    # reported as "unmet" (which would be the over-gating failure mode this
    # task explicitly warns about).
    api, report = _run_main(
        monkeypatch, [CASE_NO_EVIDENCE_AT_ALL],
        invoke_text_stub=_call_tracking_stub(), argv=["--apply"],
    )
    statuses = {r["status"] for r in report.rows}
    assert "unmet" not in statuses
    assert "enriched" in statuses


# --------------------------------------------------------------------------
# Task PP2 -- run-logging events file (see test_enrich_missing_bigo.py's
# identical block for the rationale; `conftest.py`'s autouse
# `_isolate_casework_run_logs` fixture keeps these out of the real repo
# `work/enricher-runs/`).
# --------------------------------------------------------------------------


def _events_path():
    logger = logging.getLogger("casework.tags")
    return logger._casework_run_paths["events"]


def _read_events(path):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines()]


def test_events_file_covers_start_extract_write_on_apply_happy_path(monkeypatch, tmp_path):
    response = json.dumps(["CIAA", "Corruption", "Health"])
    _run_main(
        monkeypatch, [CASE_NEEDS_LLM_TOPUP],
        invoke_text_stub=lambda **kw: response, argv=["--apply"],
    )

    rows = _read_events(_events_path())
    assert rows

    required_keys = {"ts", "run_id", "stage", "slug", "step", "status", "detail", "elapsed_ms"}
    for row in rows:
        assert required_keys <= set(row.keys())
        assert row["stage"] == "tags"

    steps_and_statuses = {(r["step"], r["status"]) for r in rows}
    assert ("start", "start") in steps_and_statuses
    assert ("extract", "ok") in steps_and_statuses
    assert ("write", "enriched") in steps_and_statuses


def test_events_file_records_would_enrich_under_dry_run(monkeypatch, tmp_path):
    response = json.dumps(["CIAA", "Corruption", "Health"])
    _run_main(
        monkeypatch, [CASE_NEEDS_LLM_TOPUP],
        invoke_text_stub=lambda **kw: response, argv=["--dry-run"],
    )

    rows = _read_events(_events_path())
    steps_and_statuses = {(r["step"], r["status"]) for r in rows}
    assert ("write", "would-enrich") in steps_and_statuses
    assert ("write", "enriched") not in steps_and_statuses
