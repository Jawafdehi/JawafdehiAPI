"""Tests for the DB-free standalone allegations enricher (casework/enrich_allegations.py).

`enrich_allegations.py` extracts 2-3 self-contained Nepali allegation sentences
from a case's CIAA press-release markdown, at the premium tier, and writes ONLY
`key_allegations` (`api.patch_field(slug, "key_allegations", allegations)`).

`missing_details` is NOT this stage's field -- it belongs to
`casework/enrich_description.py`, which has the verdict in hand. This stage only
ever reads the press release. `test_only_key_allegations_field_is_ever_patched`
pins that.

The `TestDonorFidelity` class re-derives `SYSTEM_PROMPT` / `USER_PROMPT_TEMPLATE`
directly from the donor at commit `0321a85` (via `git show` + `ast.literal_eval`,
not by trusting this file's own transcription) and asserts byte-identical
equality -- a drifted clause changes LLM behavior with zero other test failures.
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

from casework import enrich_allegations as ea
from casework.enrich_allegations import (
    _append_acquittal_line,
    _clamp,
    _extract_allegations,
    _hedge,
    _parse_allegations_response,
)
from tests.casework.fakes import FakeUsage

REPO_ROOT = Path(__file__).resolve().parents[2]
DONOR_COMMIT = "0321a85"

# The only intended divergence from the donor prompts: the reviewer's
# `tone.hedge_key_allegations` rule (work/slug-fix/enricher-fix-rules.json).
# Stated as substitutions so the byte-pin above still catches every OTHER drift.
SYSTEM_SUBS = [
    (
        "10. Follow the established Jawafdehi allegation style (see examples below)",
        "10. Follow the established Jawafdehi allegation style (see examples below)\n"
        '11. End with the charge marker "भन्ने आरोप छ।" — a participle clause closed'
        " by that phrase, so the sentence reads as the CIAA's claim and not as a"
        " finding of fact",
    ),
    (
        '- End allegations with attribution phrases such as "उल्लेख छ", '
        '"भनिएको छ", "जनाइएको छ", "देखिन्छ", or "आरोप छ"',
        '- End allegations with source-attribution phrases such as "उल्लेख छ", '
        '"भनिएको छ", "जनाइएको छ", or "देखिन्छ" — these attribute the sentence to '
        "the document rather than to the charge",
    ),
    ('गरेको।"', 'गरेको भन्ने आरोप छ।"'),
    ('पुर्याएको।"', 'पुर्याएको भन्ने आरोप छ।"'),
    ('लिएको।"', 'लिएको भन्ने आरोप छ।"'),
]

USER_SUBS = [
    (
        '- Do not end any allegation with attribution wording such as "उल्लेख छ", '
        '"भनिएको छ", "जनाइएको छ", "देखिन्छ", or "आरोप छ"',
        '- End every allegation with "भन्ने आरोप छ।" — write the act as a participle '
        'clause and close with that phrase, as in "…गरेको भन्ने आरोप छ।"\n'
        '- Do not use source-attribution wording such as "उल्लेख छ", "भनिएको छ", '
        '"जनाइएको छ", or "देखिन्छ"',
    ),
]


def _apply(text: str, subs) -> str:
    for old, new in subs:
        assert old in text, f"donor text no longer contains: {old[:40]}…"
        text = text.replace(old, new)
    return text


def _donor_source() -> str:
    proc = subprocess.run(
        ["git", "show", f"{DONOR_COMMIT}:casework/enrich_allegations.py"],
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
    wanted = {"SYSTEM_PROMPT", "USER_PROMPT_TEMPLATE"}
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
    module's own transcription. A drifted clause is the highest-consequence
    silent failure available in these files: it changes LLM behavior with
    zero test failures anywhere else."""

    def test_system_prompt_is_donor_plus_the_charge_marker_rule(self, donor):
        assert ea.SYSTEM_PROMPT == _apply(donor["SYSTEM_PROMPT"], SYSTEM_SUBS)

    def test_user_prompt_is_donor_plus_the_charge_marker_rule(self, donor):
        assert ea.USER_PROMPT_TEMPLATE == _apply(
            donor["USER_PROMPT_TEMPLATE"], USER_SUBS)

    def test_every_reference_example_carries_the_charge_marker(self):
        block = ea.SYSTEM_PROMPT.split("REFERENCE EXAMPLES", 1)[1]
        examples = re.findall(r'"([^"]+)"', block, re.S)
        assert len(examples) == 4
        for text in examples:
            assert text.rstrip().endswith(ea.HEDGE.strip())

    def test_the_charge_marker_is_not_banned_as_attribution_wording(self):
        for prompt in (ea.SYSTEM_PROMPT, ea.USER_PROMPT_TEMPLATE):
            banlines = [ln for ln in prompt.splitlines() if "उल्लेख छ" in ln]
            assert banlines
            for line in banlines:
                assert '"आरोप छ"' not in line

    def test_donor_never_mentions_missing_details(self):
        # Pins the brief-vs-donor finding: the donor source itself never
        # references missing_details, in either prompt constant or code.
        assert "missing_details" not in _donor_source()

    def test_donor_writes_exactly_one_field_via_patch_field(self):
        # The donor's only `api.patch_field` call names key_allegations.
        assert _donor_source().count("api.patch_field(") == 1
        assert 'patch_field(case_slug, "key_allegations"' in _donor_source()


# --------------------------------------------------------------------------
# _parse_allegations_response
# --------------------------------------------------------------------------


class TestHedge:
    """`tone.hedge_key_allegations` -- a bare declarative ('…गरेको।') reads as an
    established fact, so it closes with the CIAA's charge instead. Proven on 10
    cases / 30 allegations, 2026-08-13 (work/slug-fix/tone-fixes.jsonl)."""

    def test_matra_participle_gets_the_charge_marker(self):
        assert _hedge("गैरकानूनी सम्पत्ति आर्जन गरेको।") == (
            "गैरकानूनी सम्पत्ति आर्जन गरेको भन्ने आरोप छ।")

    def test_independent_vowel_participle_gets_the_charge_marker(self):
        # A /ेको।$/ pattern misses this form and 6 others like it.
        assert _hedge("राजस्व लुकाएको।") == "राजस्व लुकाएको भन्ने आरोप छ।"

    def test_marker_is_not_applied_twice(self):
        already = "रकम हिनामिना गरेको भन्ने आरोप छ।"
        assert _hedge(already) == already

    def test_an_existing_abhiyog_dabi_phrasing_is_left_alone(self):
        already = "यो अभियोग दाबी विशेष अदालतमा पेस भएको छ।"
        assert _hedge(already) == already

    def test_a_space_before_the_danda_does_not_defeat_the_match(self):
        assert _hedge("रकम हिनामिना गरेको ।") == (
            "रकम हिनामिना गरेको भन्ने आरोप छ।")

    def test_an_ascii_full_stop_is_also_a_terminator(self):
        assert _hedge("रकम हिनामिना गरेको.") == (
            "रकम हिनामिना गरेको भन्ने आरोप छ।")

    def test_an_unterminated_participle_is_still_hedged(self):
        assert _hedge("रकम हिनामिना गरेको") == (
            "रकम हिनामिना गरेको भन्ने आरोप छ।")

    def test_a_guilt_asserting_perfect_is_hedged(self):
        # "निजले घुस लिएको छ।" asserts guilt as plainly as "…लिएको।" does. The
        # copula is absorbed rather than kept -- HEDGE carries its own छ.
        assert _hedge("निजले घुस लिएको छ।") == (
            "निजले घुस लिएको भन्ने आरोप छ।")

    def test_a_neutral_perfect_is_hedged_too(self):
        # The deliberate cost of hedging the perfect: a neutral statement about
        # the charge sheet is marked as a claim as well. It stays TRUE (the
        # बिगो is the CIAA's own figure), and the alternative leaves the
        # guilt-asserting perfect above unqualified -- the exact harm the rule
        # exists to prevent. A regex cannot tell the two apart.
        assert _hedge("बिगो रु. ५ करोड कायम भएको छ।") == (
            "बिगो रु. ५ करोड कायम भएको भन्ने आरोप छ।")

    def test_a_genitive_ko_is_not_mistaken_for_a_participle(self):
        # "सरकारको" is a genitive, not a participle; suffixing it yields
        # "…सरकारको भन्ने आरोप छ।", which is not Nepali.
        plain = "सो रकम नेपाल सरकारको।"
        assert _hedge(plain) == plain

    def test_non_participle_ending_is_skipped_not_force_suffixed(self):
        # Force-suffixing a non-participle produces ungrammatical Nepali.
        plain = "यो रकम नेपाल सरकारको सम्पत्ति हो।"
        assert _hedge(plain) == plain

    def test_trailing_whitespace_does_not_defeat_the_match(self):
        assert _hedge("पद दुरुपयोग गरेको।  \n") == (
            "पद दुरुपयोग गरेको भन्ने आरोप छ।")


def _accused(name, outcome):
    return {"nes_id": f"https://jawafdehi.org/entity/person/{name}",
            "display_name": name, "type": "accused", "outcome": outcome}


# Canonical court-case @id IRIs, the only reference form Case.court_cases holds
# (cases.validators.validate_court_cases).
SPECIAL_IRI = "https://jawafdehi.org/courtcase/special/080-cr-0111"
SUPREME_IRI = "https://jawafdehi.org/courtcase/supreme/080-cr-0111"
HIGH_COURT_IRI = "https://jawafdehi.org/courtcase/janakpurhc/080-cr-0111"


class TestAppendAcquittalLine:
    """`tone.append_acquittal_line` -- key_allegations renders standalone on some
    surfaces, so on a case where the court cleared every BOUND accused the field
    alone reads as an unqualified guilt narrative. The verdict is read from the
    accused binds' `outcome`, never from the title or the prose. Binds are not
    guaranteed complete, so the run ledger carries the bind count the decision
    was made on."""

    ALLEGATIONS = ["गैरकानूनी सम्पत्ति आर्जन गरेको भन्ने आरोप छ।"]

    def test_sole_acquitted_defendant_gets_the_singular_line(self):
        detail = {"entities": [_accused("राम", "acquitted")]}
        out, reason = _append_acquittal_line(detail, list(self.ALLEGATIONS))
        assert reason == "appended"
        assert len(out) == 2
        assert out[:1] == self.ALLEGATIONS
        assert "प्रतिवादीलाई आरोपित कसुरबाट सफाइ दिने ठहर गरेको छ।" in out[1]
        assert "प्रतिवादीहरूलाई" not in out[1]

    def test_several_acquitted_defendants_get_the_plural_line(self):
        detail = {"entities": [_accused("राम", "acquitted"),
                               _accused("श्याम", "acquitted")]}
        out, _ = _append_acquittal_line(detail, list(self.ALLEGATIONS))
        assert "प्रतिवादीहरूलाई आरोपित कसुरबाट सफाइ दिने ठहर गरेको छ।" in out[1]

    def test_the_line_names_the_ciaa_claim_and_the_special_court(self):
        detail = {"entities": [_accused("राम", "acquitted")]}
        line = _append_acquittal_line(detail, list(self.ALLEGATIONS))[0][1]
        assert line.startswith("माथि उल्लिखित कुराहरू अख्तियार दुरुपयोग अनुसन्धान आयोगको अभियोग दाबी हुन्;")
        assert "विशेष अदालतले उक्त दाबी पुग्न नसकी" in line

    def test_a_mixed_verdict_is_left_alone(self):
        # A partial conviction: appending a blanket acquittal would be false.
        detail = {"entities": [_accused("राम", "acquitted"),
                               _accused("श्याम", "convicted")]}
        assert _append_acquittal_line(detail, list(self.ALLEGATIONS)) == (
            self.ALLEGATIONS, "not-unanimous")

    def test_an_undecided_case_is_left_alone(self):
        # 4 of the 10 cases the rule was proven on sat at `charged`.
        detail = {"entities": [_accused("राम", "charged")]}
        assert _append_acquittal_line(detail, list(self.ALLEGATIONS)) == (
            self.ALLEGATIONS, "not-unanimous")

    def test_a_missing_outcome_is_left_alone(self):
        detail = {"entities": [{"display_name": "राम", "type": "accused"}]}
        assert _append_acquittal_line(detail, list(self.ALLEGATIONS)) == (
            self.ALLEGATIONS, "not-unanimous")

    def test_an_abated_co_defendant_blocks_the_line(self):
        detail = {"entities": [_accused("राम", "acquitted"),
                               _accused("श्याम", "abated")]}
        assert _append_acquittal_line(detail, list(self.ALLEGATIONS)) == (
            self.ALLEGATIONS, "not-unanimous")

    def test_a_case_with_no_accused_bind_is_left_alone(self):
        detail = {"entities": [{"display_name": "झापा", "type": "location"}]}
        assert _append_acquittal_line(detail, list(self.ALLEGATIONS)) == (
            self.ALLEGATIONS, "no-accused-bind")

    def test_an_outcome_on_a_non_accused_row_does_not_count(self):
        # `outcome` is meaningful only on an accused bind (cases.models
        # RelationshipOutcome); a stray value elsewhere must not decide a verdict.
        detail = {"entities": [_accused("राम", "charged"),
                               dict(_accused("संस्था", "acquitted"), type="related")]}
        assert _append_acquittal_line(detail, list(self.ALLEGATIONS)) == (
            self.ALLEGATIONS, "not-unanimous")

    def test_list_shaped_detail_without_entities_is_left_alone(self):
        # No binds, no verdict, no line -- whatever shape the case arrived in.
        assert _append_acquittal_line({}, list(self.ALLEGATIONS)) == (
            self.ALLEGATIONS, "no-accused-bind")

    def test_an_existing_safai_entry_is_not_duplicated(self):
        detail = {"entities": [_accused("राम", "acquitted")]}
        already = list(self.ALLEGATIONS) + ["अदालतले सफाइ दिएको छ।"]
        assert _append_acquittal_line(detail, list(already)) == (
            already, "already-stated")

    def test_the_other_safai_spelling_also_blocks_the_line(self):
        detail = {"entities": [_accused("राम", "acquitted")]}
        already = list(self.ALLEGATIONS) + ["अदालतले सफाई दिएको छ।"]
        assert _append_acquittal_line(detail, list(already)) == (
            already, "already-stated")

    def test_a_sanitation_contract_does_not_block_the_line(self):
        # `सफाइ` is a substring of `सरसफाइ` (sanitation), a routine CIAA
        # contract subject. A bare-morpheme guard silently suppressed the line
        # on exactly the acquitted cases the rule was written for.
        detail = {"entities": [_accused("राम", "acquitted")]}
        allegations = ["नगरपालिकाको सरसफाइ ठेक्कामा अनियमितता गरेको भन्ने आरोप छ।"]
        out, reason = _append_acquittal_line(detail, list(allegations))
        assert reason == "appended"
        assert len(out) == 2

    def test_running_twice_appends_only_once(self):
        detail = {"entities": [_accused("राम", "acquitted")]}
        once, _ = _append_acquittal_line(detail, list(self.ALLEGATIONS))
        assert _append_acquittal_line(detail, list(once)) == (once, "already-stated")

    def test_a_supreme_court_reference_blocks_the_special_court_line(self):
        # `outcome` is set from *a* primary court order, which on an appealed
        # case can be the Supreme Court's. Naming विशेष अदालत would then state
        # the opposite of what that court ruled.
        detail = {"entities": [_accused("राम", "acquitted")],
                  "court_cases": [SPECIAL_IRI, SUPREME_IRI]}
        assert _append_acquittal_line(detail, list(self.ALLEGATIONS)) == (
            self.ALLEGATIONS, "other-court:supreme")

    def test_a_high_court_reference_blocks_the_line_too(self):
        detail = {"entities": [_accused("राम", "acquitted")],
                  "court_cases": [HIGH_COURT_IRI]}
        assert _append_acquittal_line(detail, list(self.ALLEGATIONS)) == (
            self.ALLEGATIONS, "other-court:janakpurhc")

    def test_a_special_court_only_reference_still_gets_the_line(self):
        detail = {"entities": [_accused("राम", "acquitted")],
                  "court_cases": [SPECIAL_IRI]}
        out, reason = _append_acquittal_line(detail, list(self.ALLEGATIONS))
        assert reason == "appended"
        assert len(out) == 2

    def test_a_malformed_court_reference_is_ignored(self):
        detail = {"entities": [_accused("राम", "acquitted")],
                  "court_cases": ["special:080-CR-0111", None, 7]}
        out, reason = _append_acquittal_line(detail, list(self.ALLEGATIONS))
        assert reason == "appended"
        assert len(out) == 2

    def test_a_malformed_entity_row_does_not_crash_the_case(self):
        detail = {"entities": ["not-a-dict", _accused("राम", "acquitted")]}
        out, _ = _append_acquittal_line(detail, list(self.ALLEGATIONS))
        assert len(out) == 2

    def test_the_input_list_is_not_mutated(self):
        detail = {"entities": [_accused("राम", "acquitted")]}
        allegations = list(self.ALLEGATIONS)
        _append_acquittal_line(detail, allegations)
        assert allegations == self.ALLEGATIONS


class TestParseAllegationsResponse:
    def test_parses_wrapped_json_object(self):
        body = json.dumps({"allegations": ["पहिलो आरोप।", "दोस्रो आरोप।"]})
        assert _parse_allegations_response(body) == ["पहिलो आरोप।", "दोस्रो आरोप।"]

    def test_caps_at_three_allegations(self):
        body = json.dumps({"allegations": ["एक", "दुई", "तीन", "चार", "पाँच"]})
        assert _parse_allegations_response(body) == ["एक", "दुई", "तीन"]

    def test_filters_blank_and_non_string_entries(self):
        body = json.dumps({"allegations": ["वैध आरोप", "   ", "", 42, None]})
        assert _parse_allegations_response(body) == ["वैध आरोप"]

    def test_returns_none_when_key_absent(self):
        # No "allegations" key AND no bare JSON array anywhere in the text --
        # parse_extraction_response's array-scan fallback has nothing to
        # find either, so this must fall all the way through to None.
        assert _parse_allegations_response('{"other": "value"}') is None

    def test_returns_none_when_all_entries_filtered_out(self):
        body = json.dumps({"allegations": ["   ", ""]})
        assert _parse_allegations_response(body) is None

    def test_returns_none_for_unparseable_text(self):
        assert _parse_allegations_response("not json at all") is None

    def test_strips_whitespace_from_each_allegation(self):
        body = json.dumps({"allegations": ["  आरोप एक  "]})
        assert _parse_allegations_response(body) == ["आरोप एक"]

    def test_bare_declarative_allegations_are_hedged_on_the_way_out(self):
        body = json.dumps({"allegations": ["सार्वजनिक सम्पत्ति हानि नोक्सानी पुर्याएको।"]})
        assert _parse_allegations_response(body) == [
            "सार्वजनिक सम्पत्ति हानि नोक्सानी पुर्याएको भन्ने आरोप छ।"]

    def test_fenced_json_is_parsed(self):
        body = (
            "Here you go:\n```json\n"
            '{"allegations": ["पहिलो आरोप।"]}'
            "\n```\n"
        )
        assert _parse_allegations_response(body) == ["पहिलो आरोप।"]


# --------------------------------------------------------------------------
# _clamp
# --------------------------------------------------------------------------


class TestClamp:
    def test_short_text_is_not_truncated(self, capsys):
        assert _clamp("hello", 100, "press release") == "hello"

    def test_long_text_is_truncated_to_limit(self, capsys):
        text = "x" * 200
        result = _clamp(text, 100, "press release")
        assert len(result) == 100

    def test_zero_limit_means_no_limit(self):
        text = "x" * 500
        assert _clamp(text, 0, "press release") == text

    def test_none_text_becomes_empty_string(self):
        assert _clamp(None, 100, "press release") == ""


# --------------------------------------------------------------------------
# _extract_allegations -- tier/max_tokens pin
# --------------------------------------------------------------------------


def test_extract_allegations_uses_premium_tier_and_2000_max_tokens():
    """Pins the donor's `tier="premium"` argument (enrich_allegations.py:350)."""
    seen = {}

    def stub(**kw):
        seen.update(kw)
        return json.dumps({"allegations": ["आरोप एक।"]})

    class _Usage:
        calls = 0

    result = _extract_allegations(
        press_release_text="प्रेस विज्ञप्ति।",
        case_title="टेस्ट मुद्दा",
        bigo="रु 1,000",
        invoke_text=stub,
        usage=_Usage(),
    )
    assert result == ["आरोप एक।"]
    assert seen["tier"] == "premium"
    assert seen["max_tokens"] == 2000
    assert seen["system"] == ea.SYSTEM_PROMPT


def test_extract_allegations_prompt_includes_title_and_bigo():
    seen = {}

    def stub(**kw):
        seen.update(kw)
        return json.dumps({"allegations": ["आरोप एक।"]})

    class _Usage:
        calls = 0

    _extract_allegations(
        press_release_text="स्रोत पाठ",
        case_title="काठमाडौं महानगरपालिका मुद्दा",
        bigo="रु 5,000,000",
        invoke_text=stub,
        usage=_Usage(),
    )
    assert "काठमाडौं महानगरपालिका मुद्दा" in seen["content"]
    assert "रु 5,000,000" in seen["content"]
    assert "स्रोत पाठ" in seen["content"]


# --------------------------------------------------------------------------
# main() -- integration over a stubbed API + LLM
# --------------------------------------------------------------------------

PRESS_CASE_UNCONVERTED = {
    "slug": "case-unconverted",
    "title": "अख्तियारले मुद्दा दायर गर्यो",
    "state": "DRAFT",
    "bigo": None,
    "key_allegations": [],
    "evidence": [
        {"material_iri": "https://jawafdehi.org/material/ciaa/press_releases/1",
         "material": {"material_type": "press_release", "urls": [
             {"link": "https://x/1.pdf", "role": "RAW"}]}},
    ],
}

PRESS_CASE_READY = {
    "slug": "case-ready",
    "title": "काठमाडौं महानगरपालिका भ्रष्टाचार मुद्दा",
    "state": "DRAFT",
    "bigo": 10403941,
    "key_allegations": [],
    "evidence": [
        {"material_iri": "https://jawafdehi.org/material/ciaa/press_releases/2",
         "material": {"material_type": "press_release", "urls": [
             {"link": "https://x/2.md", "role": "MARKDOWN"}]}},
    ],
}

PRESS_CASE_ALREADY_POPULATED = {
    "slug": "case-populated",
    "title": "पहिल्यै आरोप तोकिएको मुद्दा",
    "state": "DRAFT",
    "bigo": 5000000,
    "key_allegations": ["पहिल्यै रहेको आरोप।"],
    "evidence": [
        {"material_iri": "https://jawafdehi.org/material/ciaa/press_releases/3",
         "material": {"material_type": "press_release", "urls": [
             {"link": "https://x/3.md", "role": "MARKDOWN"}]}},
    ],
}

PRESS_CASE_LLM_DECLINES = {
    "slug": "case-declines",
    "title": "अस्पष्ट प्रेस विज्ञप्ति मुद्दा",
    "state": "DRAFT",
    "bigo": None,
    "key_allegations": [],
    "evidence": [
        {"material_iri": "https://jawafdehi.org/material/ciaa/press_releases/4",
         "material": {"material_type": "press_release", "urls": [
             {"link": "https://x/4.md", "role": "MARKDOWN"}]}},
    ],
}

# Distinct LIST-shaped vs DETAIL-shaped titles, to pin the donor-preserved
# behavior: the LLM prompt's case_title comes from the LIST case dict
# captured BEFORE the detail fetch, never from the detail response.
PRESS_CASE_TITLE_DIVERGES_LIST = {
    "slug": "case-title-diverges",
    "title": "सूची शीर्षक",
    "state": "DRAFT",
    "bigo": None,
    "key_allegations": [],
    "evidence": [
        {"material_iri": "https://jawafdehi.org/material/ciaa/press_releases/5",
         "material": {"material_type": "press_release", "urls": [
             {"link": "https://x/5.md", "role": "MARKDOWN"}]}},
    ],
}
PRESS_CASE_TITLE_DIVERGES_DETAIL = dict(
    PRESS_CASE_TITLE_DIVERGES_LIST, title="विवरण शीर्षक",
)

# The real LIST endpoint returns `material: null` on every evidence entry
# (only DETAIL resolves it -- see casework/common/materials.py). Used to
# exercise the donor-preserved get_case-failure fallback honestly: falling
# back to a case object that never resolves material must surface as an
# "unmet" reason, not silently succeed because the test fixture happened to
# carry resolved material in the "list" copy too.
PRESS_CASE_READY_LIST_SHAPE = {
    "slug": "case-ready",
    "title": "काठमाडौं महानगरपालिका भ्रष्टाचार मुद्दा",
    "state": "DRAFT",
    "bigo": 10403941,
    "key_allegations": [],
    "evidence": [
        {"material_iri": "https://jawafdehi.org/material/ciaa/press_releases/2",
         "material": None},
    ],
}


class _StubApi:
    def __init__(self, cases, detail_overrides=None, fail_detail_for=()):
        # Shallow-copy so `patch_field` mutations to one test's fixture dict
        # never leak into a later test that reuses the same module-level
        # object (see test_enrich_missing_bigo.py's identical rationale).
        self._cases = {c["slug"]: dict(c) for c in cases}
        self._detail_overrides = detail_overrides or {}
        self._fail_detail_for = set(fail_detail_for)
        self.patched = []

    def iter_cases(self, params=None, timeout=60):
        yield from self._cases.values()

    def get_case(self, slug, timeout=60):
        if slug in self._fail_detail_for:
            raise RuntimeError(f"simulated detail-fetch failure for {slug}")
        if slug in self._detail_overrides:
            return self._detail_overrides[slug]
        return self._cases[slug]

    def patch_field(self, slug, field, value, timeout=60):
        self.patched.append((slug, field, value))
        self._cases[slug][field] = value
        return {}


@pytest.fixture
def patched_fetch_markdown(monkeypatch):
    import casework.common.materials as m

    def fake_fetch(link, timeout=60):
        return {
            "https://x/2.md": "काठमाडौं महानगरपालिकाको ठेक्कामा भ्रष्टाचार भएको छ ।",
            "https://x/3.md": "पहिल्यै आरोप लागेको मुद्दा।",
            "https://x/4.md": "अस्पष्ट प्रेस विज्ञप्ति।",
            "https://x/5.md": "प्रेस विज्ञप्ति सामग्री।",
        }.get(link, "")

    monkeypatch.setattr(m, "fetch_markdown", fake_fetch)


def _run_main(monkeypatch, api, invoke_text_stub, argv):
    """Drive `main()` end to end with a stubbed API and a stubbed LLM call.

    `invoke_text` and `UsageAccumulator` are imported INSIDE `main()` (after
    bootstrap), so they're faked out via `sys.modules` rather than
    `monkeypatch.setattr(ea, ...)` -- mirrors
    test_enrich_missing_bigo.py/test_enrich_tags.py.
    """
    monkeypatch.setattr(ea, "build_api", lambda args: api)
    monkeypatch.setattr(ea, "bootstrap", lambda *a, **k: None)

    fake_llm_invoke = types.ModuleType("llm.invoke")
    fake_llm_invoke.invoke_text = invoke_text_stub

    fake_llm_usage = types.ModuleType("llm.usage")
    fake_llm_usage.UsageAccumulator = FakeUsage
    fake_llm_usage.render_usage_table = lambda by_provider, title=None: ""

    monkeypatch.setitem(sys.modules, "llm.invoke", fake_llm_invoke)
    monkeypatch.setitem(sys.modules, "llm.usage", fake_llm_usage)

    report = ea.main(argv)
    return report


def _call_tracking_stub(response=None):
    """A stub that records invocations instead of raising.

    IMPORTANT: `enrich_allegations.main()` wraps the LLM call in a narrow
    `except Exception` around `_extract_allegations` only -- an "LLM must not
    be called" assertion MUST check `stub.calls == []` explicitly rather
    than relying on a raise to propagate as a test failure, since a raise
    from a case that legitimately reaches the LLM call would be swallowed
    and counted as an "error" status instead of failing the test loudly.
    """
    if response is None:
        response = json.dumps({"allegations": ["आरोप एक।"]})
    calls = []

    def stub(**kw):
        calls.append(kw)
        return response

    stub.calls = calls
    return stub


def test_unmet_prerequisite_is_recorded_not_silently_skipped(
    monkeypatch, patched_fetch_markdown
):
    api = _StubApi([PRESS_CASE_UNCONVERTED])
    stub = _call_tracking_stub()
    report = _run_main(monkeypatch, api, invoke_text_stub=stub, argv=["--dry-run"])
    statuses = {r["status"] for r in report.rows}
    assert "unmet" in statuses
    assert report.rows[0]["status"] == "unmet"
    assert report.rows[0]["reason"]  # a real reason string, never blank
    assert stub.calls == []


def test_already_populated_case_is_skipped_without_calling_llm(
    monkeypatch, patched_fetch_markdown
):
    api = _StubApi([PRESS_CASE_ALREADY_POPULATED])
    stub = _call_tracking_stub()
    report = _run_main(monkeypatch, api, invoke_text_stub=stub, argv=["--dry-run"])
    assert report.rows[0]["status"] == "already"
    assert api.patched == []
    assert stub.calls == []


def test_force_reruns_an_already_populated_case(monkeypatch, patched_fetch_markdown):
    response = json.dumps({"allegations": ["नयाँ आरोप।"]})
    api = _StubApi([PRESS_CASE_ALREADY_POPULATED])
    report = _run_main(
        monkeypatch, api, invoke_text_stub=lambda **kw: response,
        argv=["--force", "--apply"],
    )
    assert report.rows[0]["status"] == "enriched"
    assert api.patched == [("case-populated", "key_allegations", ["नयाँ आरोप।"])]


def test_dry_run_extracts_but_does_not_patch(monkeypatch, patched_fetch_markdown):
    response = json.dumps({"allegations": ["नयाँ आरोप।"]})
    api = _StubApi([PRESS_CASE_READY])
    report = _run_main(
        monkeypatch, api, invoke_text_stub=lambda **kw: response, argv=["--dry-run"],
    )
    assert report.rows[0]["status"] == "would-enrich"
    assert api.patched == []


def test_apply_patches_key_allegations(monkeypatch, patched_fetch_markdown):
    response = json.dumps({"allegations": ["पहिलो आरोप।", "दोस्रो आरोप।"]})
    api = _StubApi([PRESS_CASE_READY])
    report = _run_main(
        monkeypatch, api, invoke_text_stub=lambda **kw: response, argv=["--apply"],
    )
    assert report.rows[0]["status"] == "enriched"
    assert api.patched == [
        ("case-ready", "key_allegations", ["पहिलो आरोप।", "दोस्रो आरोप।"])]


# An acquitted-on-all-counts case, entity binds included. Only the DETAIL
# response carries `entities`, which is what `_append_acquittal_line` reads.
PRESS_CASE_ACQUITTED_DETAIL = dict(
    PRESS_CASE_READY,
    entities=[
        {"nes_id": "https://jawafdehi.org/entity/person/hem-raj-bista",
         "display_name": "हेमराज बिष्ट", "type": "accused", "outcome": "acquitted"},
        {"nes_id": "https://jawafdehi.org/entity/district/jhapa-np0104",
         "display_name": "झापा", "type": "location"},
    ],
)

PRESS_CASE_CONVICTED_DETAIL = dict(
    PRESS_CASE_READY,
    entities=[
        {"nes_id": "https://jawafdehi.org/entity/person/hem-raj-bista",
         "display_name": "हेमराज बिष्ट", "type": "accused", "outcome": "convicted"},
    ],
)

# The same acquittal, on a case that also reached the Supreme Court: `outcome`
# may have been set from the appeal order, so the Special Court line is refused.
PRESS_CASE_APPEALED_DETAIL = dict(
    PRESS_CASE_ACQUITTED_DETAIL,
    court_cases=["https://jawafdehi.org/courtcase/special/080-cr-0111",
                 "https://jawafdehi.org/courtcase/supreme/080-cr-0111"],
)


def test_apply_appends_the_acquittal_line_when_the_court_cleared_everyone(
    monkeypatch, patched_fetch_markdown
):
    response = json.dumps({"allegations": ["पहिलो आरोप गरेको।"]})
    api = _StubApi(
        [PRESS_CASE_READY],
        detail_overrides={"case-ready": PRESS_CASE_ACQUITTED_DETAIL},
    )
    _run_main(
        monkeypatch, api, invoke_text_stub=lambda **kw: response, argv=["--apply"],
    )
    (_, _, patched), = api.patched
    assert patched == [
        "पहिलो आरोप गरेको भन्ने आरोप छ।",
        "माथि उल्लिखित कुराहरू अख्तियार दुरुपयोग अनुसन्धान आयोगको अभियोग दाबी हुन्; "
        "विशेष अदालतले उक्त दाबी पुग्न नसकी प्रतिवादीलाई आरोपित कसुरबाट सफाइ दिने "
        "ठहर गरेको छ।",
    ]


def test_apply_does_not_append_the_acquittal_line_on_a_conviction(
    monkeypatch, patched_fetch_markdown
):
    response = json.dumps({"allegations": ["पहिलो आरोप गरेको।"]})
    api = _StubApi(
        [PRESS_CASE_READY],
        detail_overrides={"case-ready": PRESS_CASE_CONVICTED_DETAIL},
    )
    _run_main(
        monkeypatch, api, invoke_text_stub=lambda **kw: response, argv=["--apply"],
    )
    (_, _, patched), = api.patched
    assert patched == ["पहिलो आरोप गरेको भन्ने आरोप छ।"]


def test_dry_run_reports_the_acquittal_line_it_would_write(
    monkeypatch, patched_fetch_markdown
):
    # The dry-run ledger is how a run is audited before --apply, so the line has
    # to be visible there, not added later on the write path.
    response = json.dumps({"allegations": ["पहिलो आरोप गरेको।"]})
    api = _StubApi(
        [PRESS_CASE_READY],
        detail_overrides={"case-ready": PRESS_CASE_ACQUITTED_DETAIL},
    )
    report = _run_main(
        monkeypatch, api, invoke_text_stub=lambda **kw: response, argv=["--dry-run"],
    )
    assert report.rows[0]["status"] == "would-enrich"
    assert "सफाइ दिने ठहर गरेको छ।" in report.rows[0]["reason"]
    assert api.patched == []


def test_only_key_allegations_field_is_ever_patched(monkeypatch, patched_fetch_markdown):
    # Pins the brief-vs-donor finding directly: no matter how many cases run,
    # the only field name that ever appears in a PATCH is key_allegations --
    # never missing_details.
    response = json.dumps({"allegations": ["पहिलो आरोप।"]})
    api = _StubApi([PRESS_CASE_READY, PRESS_CASE_ALREADY_POPULATED])
    _run_main(
        monkeypatch, api, invoke_text_stub=lambda **kw: response,
        argv=["--force", "--apply"],
    )
    fields = {field for _, field, _ in api.patched}
    assert fields == {"key_allegations"}
    assert "missing_details" not in fields


def test_llm_declining_is_recorded_as_skipped_not_enriched(
    monkeypatch, patched_fetch_markdown
):
    # LLM returns a JSON object without the "allegations" key at all --
    # parse_extraction_response returns None, and the case must be recorded
    # as "skipped", not silently treated as "enriched" with an empty list.
    response = json.dumps({"other": "no allegations key"})
    api = _StubApi([PRESS_CASE_LLM_DECLINES])
    report = _run_main(
        monkeypatch, api, invoke_text_stub=lambda **kw: response, argv=["--apply"],
    )
    assert report.rows[0]["status"] == "skipped"
    assert api.patched == []


def test_llm_extraction_failure_is_recorded_as_error_not_enriched(
    monkeypatch, patched_fetch_markdown
):
    def stub(**kw):
        raise RuntimeError("LLM provider unavailable")

    api = _StubApi([PRESS_CASE_READY])
    report = _run_main(monkeypatch, api, invoke_text_stub=stub, argv=["--apply"])
    assert report.rows[0]["status"] == "error"
    assert api.patched == []


def test_llm_invoked_with_premium_tier_end_to_end(monkeypatch, patched_fetch_markdown):
    """Pins the donor's `tier="premium"` argument (enrich_allegations.py:350)."""
    seen_tiers = []

    def stub(**kw):
        seen_tiers.append(kw.get("tier"))
        return json.dumps({"allegations": ["आरोप एक।"]})

    api = _StubApi([PRESS_CASE_READY])
    _run_main(monkeypatch, api, invoke_text_stub=stub, argv=["--apply"])
    assert seen_tiers == ["premium"]


def test_detail_fetch_failure_falls_back_to_summary_case_not_a_crash(
    monkeypatch, patched_fetch_markdown
):
    # Donor-preserved: a detail-fetch failure does not abort the case -- the
    # donor fell back to the LIST-shaped `case` dict. The LIST shape here
    # never resolves `material` (see materials.py), so it must surface as an
    # "unmet" reason, never a crash and never a silently fabricated result.
    api = _StubApi([PRESS_CASE_READY_LIST_SHAPE], fail_detail_for={"case-ready"})
    stub = _call_tracking_stub()
    report = _run_main(monkeypatch, api, invoke_text_stub=stub, argv=["--dry-run"])
    assert report.rows[0]["status"] == "unmet"
    assert stub.calls == []
    assert api.patched == []


def test_detail_fetch_success_does_not_hit_the_fallback_path(
    monkeypatch, patched_fetch_markdown
):
    # Sanity complement to the fallback test above: when get_case succeeds,
    # the resolved DETAIL case is used and the case is processed normally
    # (not treated as unmet), proving the fallback only fires on failure.
    api = _StubApi([PRESS_CASE_READY])
    response = json.dumps({"allegations": ["आरोप एक।"]})
    report = _run_main(
        monkeypatch, api, invoke_text_stub=lambda **kw: response, argv=["--dry-run"],
    )
    assert report.rows[0]["status"] == "would-enrich"


def test_prompt_case_title_comes_from_list_case_not_detail(
    monkeypatch, patched_fetch_markdown
):
    # Donor-preserved: `_process_case`'s `title` is captured from the
    # LIST-shaped `case` dict BEFORE the detail fetch and passed to
    # `_extract_allegations` as-is -- never re-read from `detail`.
    seen = {}

    def stub(**kw):
        seen.update(kw)
        return json.dumps({"allegations": ["आरोप एक।"]})

    api = _StubApi(
        [PRESS_CASE_TITLE_DIVERGES_LIST],
        detail_overrides={"case-title-diverges": PRESS_CASE_TITLE_DIVERGES_DETAIL},
    )
    _run_main(monkeypatch, api, invoke_text_stub=stub, argv=["--apply"])
    assert "सूची शीर्षक" in seen["content"]
    assert "विवरण शीर्षक" not in seen["content"]


def test_bigo_display_uses_devanagari_format_when_present(
    monkeypatch, patched_fetch_markdown
):
    seen = {}

    def stub(**kw):
        seen.update(kw)
        return json.dumps({"allegations": ["आरोप एक।"]})

    api = _StubApi([PRESS_CASE_READY])
    _run_main(monkeypatch, api, invoke_text_stub=stub, argv=["--apply"])
    assert "रु 10,403,941" in seen["content"]


def test_bigo_display_falls_back_to_placeholder_when_absent(
    monkeypatch, patched_fetch_markdown
):
    seen = {}

    def stub(**kw):
        seen.update(kw)
        return json.dumps({"allegations": ["आरोप एक।"]})

    api = _StubApi([PRESS_CASE_LLM_DECLINES])
    _run_main(monkeypatch, api, invoke_text_stub=stub, argv=["--apply"])
    assert "उल्लेख छैन" in seen["content"]


# --------------------------------------------------------------------------
# Task PP2 -- run-logging events file (see test_enrich_missing_bigo.py's
# identical block for the rationale; `conftest.py`'s autouse
# `_isolate_casework_run_logs` fixture keeps these out of the real repo
# `work/enricher-runs/`).
# --------------------------------------------------------------------------


def _events_path():
    logger = logging.getLogger("casework.allegations")
    return logger._casework_run_paths["events"]


def _read_events(path):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines()]


def test_events_file_covers_start_extract_write_on_apply_happy_path(
    monkeypatch, patched_fetch_markdown, tmp_path
):
    response = json.dumps({"allegations": ["आरोप एक।"]})
    api = _StubApi([PRESS_CASE_READY])
    _run_main(monkeypatch, api, invoke_text_stub=lambda **kw: response, argv=["--apply"])

    rows = _read_events(_events_path())
    assert rows

    required_keys = {"ts", "run_id", "stage", "slug", "step", "status", "detail", "elapsed_ms"}
    for row in rows:
        assert required_keys <= set(row.keys())
        assert row["stage"] == "allegations"

    steps_and_statuses = {(r["step"], r["status"]) for r in rows}
    assert ("start", "start") in steps_and_statuses
    assert ("extract", "ok") in steps_and_statuses
    assert ("write", "enriched") in steps_and_statuses


def test_events_file_records_would_enrich_under_dry_run(
    monkeypatch, patched_fetch_markdown, tmp_path
):
    response = json.dumps({"allegations": ["आरोप एक।"]})
    api = _StubApi([PRESS_CASE_READY])
    _run_main(monkeypatch, api, invoke_text_stub=lambda **kw: response, argv=["--dry-run"])

    rows = _read_events(_events_path())
    steps_and_statuses = {(r["step"], r["status"]) for r in rows}
    assert ("write", "would-enrich") in steps_and_statuses
    assert ("write", "enriched") not in steps_and_statuses


def test_extract_event_records_the_bind_count_the_verdict_was_read_from(
    monkeypatch, patched_fetch_markdown, tmp_path
):
    # Accused binds are not guaranteed complete -- a case can carry fewer binds
    # than it has defendants (unresolved or held names never get bound). The
    # unanimity test can only see the binds, so the ledger records how many it
    # saw, and an operator auditing a dry run can spot the gap.
    response = json.dumps({"allegations": ["पहिलो आरोप गरेको।"]})
    api = _StubApi(
        [PRESS_CASE_READY],
        detail_overrides={"case-ready": PRESS_CASE_ACQUITTED_DETAIL},
    )
    _run_main(monkeypatch, api, invoke_text_stub=lambda **kw: response,
              argv=["--dry-run"])

    extract, = [r for r in _read_events(_events_path()) if r["step"] == "extract"]
    assert "accused_binds=1" in extract["detail"]
    assert "acquittal_line=appended" in extract["detail"]


def test_extract_event_names_the_reason_a_suppressed_line_was_suppressed(
    monkeypatch, patched_fetch_markdown, tmp_path
):
    # A skipped acquittal line must not be silent -- the ledger names the reason.
    response = json.dumps({"allegations": ["पहिलो आरोप गरेको।"]})
    api = _StubApi(
        [PRESS_CASE_READY],
        detail_overrides={"case-ready": PRESS_CASE_APPEALED_DETAIL},
    )
    _run_main(monkeypatch, api, invoke_text_stub=lambda **kw: response,
              argv=["--dry-run"])

    extract, = [r for r in _read_events(_events_path()) if r["step"] == "extract"]
    assert "acquittal_line=other-court:supreme" in extract["detail"]
    assert "सफाइ दिने" not in extract["detail"]
