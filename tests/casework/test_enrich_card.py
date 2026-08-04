"""Tests for the DB-free standalone card enricher (casework/enrich_card.py).

`enrich_card.py` writes `Case.title` and `Case.short_description` from the
`description` already on the case. It fetches no source documents, and it is the
SOLE writer of `title` on `main`.

The two things worth the most test weight here:

1. THE TEMPLATE STUB. 2,666 of 2,918 DRAFT cases carry a `short_description`
   that is 120-odd characters of grammatical Nepali naming a real case and a
   real defendant. Anything that treats "populated" as "done" skips every case
   this script exists to fix, which is why the donor's list-level
   `skip_field="short_description"` had to go and why the gate is a content
   judge. `test_the_real_template_stub_is_classified_as_a_placeholder` uses the
   verbatim production string.

2. `--only title` MUST NOT TOUCH THE DESCRIPTION. That path replaces the title
   pass stripped out of `enrich_description`, and its whole reason to exist is
   fixing a title on a case whose description is already good.
"""
import argparse
import ast
import json
import subprocess
import sys
import types
from pathlib import Path

import pytest

from casework import enrich_card as ec
from casework.enrich_card import (
    _build_prompt,
    _generate,
    _snippet,
    build_parser,
    vet_short_description,
    vet_title,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DONOR_COMMIT = "0321a85"

# The verbatim production template, from the brief. Both fields, one real case.
STUB_TITLE = "CIAA Special Court Case 076-CR-0182: बिनोद कुमार भूजेल समेत ५"
STUB_SHORT = (
    "अख्तियार दुरुपयोग अनुसन्धान आयोगले विशेष अदालतमा दायर गरेको मुद्दा "
    "076-CR-0182, प्रतिवादी: बिनोद कुमार भूजेल समेत ५।"
)
GOOD_TITLE = "काठमाडौं महानगरपालिका ठेक्का घोटाला: रु ३३ करोड हिनामिना (081-CR-0091)"

# A title whose court number matches CASE_STUB_BOTH. `GOOD_TITLE` names
# 081-CR-0091, so `vet_title` rightly rejects it for a 076-CR-0182 case --
# use this whenever a test needs the title write to actually land.
MATCHING_TITLE = "काठमाडौं ठेक्का घोटाला: रु ३३ करोड हिनामिना (076-CR-0182)"


def _donor_source(name) -> str:
    proc = subprocess.run(
        ["git", "show", f"{DONOR_COMMIT}:casework/{name}"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        pytest.skip(
            f"donor commit {DONOR_COMMIT} not in local history "
            "(shallow clone?); fidelity check needs full git history")
    return proc.stdout


@pytest.fixture(scope="module")
def donor_card():
    return _donor_source("enrich_card.py")


@pytest.fixture(scope="module")
def donor_title():
    return _donor_source("enrich_title.py")


def _shipped_source():
    return Path(ec.__file__).read_text(encoding="utf-8")


def _imports_name(path, name):
    """True when `path` really imports `name`.

    An AST check, not a text grep: `enrich_description.py`'s docstring MENTIONS
    `TITLE_RULES` on purpose -- it explains that the title pass was dropped --
    and a grep would read that explanation as a violation of the rule it exists
    to document.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return any(
        isinstance(node, ast.ImportFrom)
        and any(a.name == name for a in node.names)
        for node in ast.walk(tree)
    )


def _identifiers(source):
    names = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.keyword) and node.arg:
            names.add(node.arg)
        elif isinstance(node, ast.alias):
            names.add(node.asname or node.name.split(".")[-1])
    return names


class TestDonorDeviations:
    """The four deliberate deviations, pinned in both directions."""

    def test_enrich_title_folds_in_as_only_title(self, donor_title):
        """Deviation 1. The donor file exists and is deliberately not ported."""
        assert "enrich_title.py" in _shipped_source(), (
            "the docstring must say why enrich_title.py was not ported, or the "
            "next reader ports it again")
        assert "def main" in donor_title
        choices = build_parser().parse_args(["--only", "title"])
        assert choices.only == "title"

    def test_the_wider_enrich_title_input_set_wins(self, donor_card, donor_title):
        """Deviation 1. `enrich_title` fed `entities`; the card donor did not.

        Named accused are exactly what a headline needs, so the wider set is
        taken -- asserted against the donors rather than trusted from prose.
        """
        assert "format_entities" in donor_title
        assert "format_entities" not in donor_card
        assert "NAMED ENTITIES" in ec.USER_PROMPT

    def test_shared_title_rules_are_used_not_restated(self, donor_card):
        """Deviation 1. The card donor restated the rules inline; this imports.

        Two copies of a public-headline contract is two places a rule changes
        and one place it silently does not.
        """
        from casework.common.titles import TITLE_RULES

        assert "TITLE_RULES" not in donor_card       # the donor restated them
        assert TITLE_RULES in ec.SYSTEM_PROMPT       # this port imports them
        assert "NEVER put a defendant HEADCOUNT" in ec.SYSTEM_PROMPT

    def test_titles_module_has_exactly_one_importer(self):
        """The single-owner rule, enforced across the whole package.

        This is the assertion that actually keeps `title` single-owner: a future
        enricher importing TITLE_RULES fails here rather than being noticed in
        review.
        """
        importers = sorted(
            p.name for p in (REPO_ROOT / "casework").rglob("*.py")
            if p.name != "titles.py" and _imports_name(p, "TITLE_RULES")
        )
        assert importers == ["enrich_card.py"], importers

    def test_the_two_donors_disagreed_on_tier_and_cheap_wins(
        self, donor_card, donor_title
    ):
        """Deviation 2, pinned against both donor sources."""
        from casework.common.llm import tier_for

        assert 'tier="cheap"' in donor_card
        assert 'tier="premium"' in donor_title
        assert tier_for("card") == "cheap"

    def test_no_list_level_skip_field_survives(self, donor_card):
        """Deviation 3. `skip_field="short_description"` would skip all 2,666."""
        assert 'skip_field="short_description"' in donor_card
        ids = _identifiers(_shipped_source())
        assert "skip_field" not in ids
        assert "get_target_cases" not in ids

    def test_the_donor_had_no_title_length_check(self, donor_card, donor_title):
        """Deviation 4. Neither donor checked `max_length=200`."""
        assert "MAX_TITLE_CHARS" not in donor_card
        assert "MAX_TITLE_CHARS" not in donor_title
        assert ec.MAX_TITLE_CHARS == 200

    def test_short_description_cap_matches_the_donor(self, donor_card):
        assert "MAX_SHORT_DESCRIPTION_CHARS = 320" in donor_card
        assert ec.MAX_SHORT_DESCRIPTION_CHARS == 320

    def test_snippet_budget_takes_the_wider_donor_value(self, donor_card, donor_title):
        assert "DESCRIPTION_SNIPPET_BUDGET = 3000" in donor_card
        assert "DESCRIPTION_SNIPPET_BUDGET = 2000" in donor_title
        assert ec.DESCRIPTION_SNIPPET_BUDGET == 3000

    def test_max_tokens_deliberately_exceeds_the_donors(self, donor_card):
        """DEVIATION 5, pinned both ways.

        The donor's 1000 killed a real case on the 2026-08-04 local smoke run:
        `API Error: Claude's response exceeded the 1000 output token maximum`.
        The provider raises rather than truncating, so the case produced nothing
        at all. Devanagari costs far more tokens per character than Latin, and
        the two fields' own caps (200 + 320 chars) have to fit.
        """
        assert "max_tokens=1000" in donor_card
        assert ec.CARD_MAX_TOKENS == 4000


# --------------------------------------------------------------------------
# stage + tier registration
# --------------------------------------------------------------------------


def test_stage_reads_no_material_but_hard_gates_on_description():
    """`requires_stages` only ORDERS -- it never checks. So the description
    dependency has to be in `requires_fields` as well, or a card gets built
    from an empty description whenever the stage runs on its own."""
    from casework.common.pipeline import STAGES

    stage = STAGES["card"]
    assert stage.requires_materials == ()
    assert stage.requires_fields == ("description",)
    assert stage.requires_stages == ("description",)


def test_stage_provides_both_card_fields():
    from casework.common.pipeline import STAGES

    assert STAGES["card"].provides == ("title", "short_description")


def test_card_orders_after_description():
    from casework.common.pipeline import order_stages

    assert order_stages(["card", "description"]) == ["description", "card"]


def test_an_empty_description_is_an_unmet_prerequisite():
    from casework.common.pipeline import STAGES, unmet_prerequisites

    unmet = unmet_prerequisites(STAGES["card"], {"description": ""})
    assert unmet == ["required field description is empty"]


# --------------------------------------------------------------------------
# the template stub -- the gate that matters most
# --------------------------------------------------------------------------


def test_the_real_template_stub_is_classified_as_a_placeholder():
    """The production stub, verbatim, must reach the model rather than being
    short-circuited as "populated, therefore fine".

    It is 120-odd characters of grammatical Nepali naming a real case and a
    real defendant, so it passes every cheap heuristic -- non-empty, long
    enough, well-formed. Only a content judgement catches it, which is why the
    gate spends a cheap LLM call instead of measuring a length.
    """
    from casework.common.judge import _JUDGE_MIN_CHARS, judge_description_adequacy

    assert len(STUB_SHORT) > _JUDGE_MIN_CHARS, (
        "the stub is long enough to clear the no-call floor -- if it weren't, "
        "this test would be passing for the wrong reason")

    seen = {}

    def stub(**kw):
        seen.update(kw)
        return json.dumps({"adequate": False, "reason": "generic boilerplate"})

    adequate, reason = judge_description_adequacy(
        STUB_SHORT, kind="case card short description", invoke_text=stub)
    assert adequate is False
    assert "boilerplate" in reason
    # The judge must actually have seen the stub text.
    assert STUB_SHORT in seen["content"]
    assert seen["tier"] == "cheap"


def test_the_template_title_fails_the_format_contract():
    """The stub title carries the case number but does not END in it in parens,
    so `title_is_acceptable` refuses it and the case is regenerated."""
    from casework.common.titles import title_is_acceptable

    assert not title_is_acceptable(STUB_TITLE, "076-CR-0182")
    assert title_is_acceptable(GOOD_TITLE, "081-CR-0091")


# --------------------------------------------------------------------------
# vet_title / vet_short_description
# --------------------------------------------------------------------------


class TestVetTitle:
    def test_a_good_title_passes(self):
        assert vet_title(GOOD_TITLE, "081-CR-0091") == (GOOD_TITLE, None)

    def test_whitespace_is_stripped(self):
        assert vet_title(f"  {GOOD_TITLE}  ", "081-CR-0091")[0] == GOOD_TITLE

    def test_an_empty_title_is_rejected(self):
        assert vet_title("", "081-CR-0091") == (None, "LLM returned no title")
        assert vet_title(None, "081-CR-0091")[0] is None

    def test_a_title_missing_the_case_number_is_rejected(self):
        value, reason = vet_title("काठमाडौं ठेक्का घोटाला", "081-CR-0091")
        assert value is None
        assert "no court case number" in reason

    def test_a_title_with_a_headcount_is_rejected(self):
        value, reason = vet_title(
            "काठमाडौं ठेक्कामा ५ जना प्रतिवादी (081-CR-0091)", "081-CR-0091")
        assert value is None
        assert "headcount" in reason

    def test_an_over_length_title_is_rejected_not_truncated(self):
        """Deviation 4. A trimmed headline looks deliberate and loses the case
        number the contract requires at the END."""
        long_title = "क" * 200 + " (081-CR-0091)"
        value, reason = vet_title(long_title, "081-CR-0091")
        assert value is None
        assert "too long" in reason
        assert "not truncating" in reason

    def test_a_title_at_exactly_the_limit_is_accepted(self):
        suffix = " (081-CR-0091)"
        exact = "क" * (ec.MAX_TITLE_CHARS - len(suffix)) + suffix
        assert len(exact) == ec.MAX_TITLE_CHARS
        assert vet_title(exact, "081-CR-0091") == (exact, None)


class TestVetShortDescription:
    def test_a_good_teaser_passes(self):
        text = "काठमाडौं महानगरपालिकाको ठेक्कामा रु ३३ करोड हिनामिना भएको आरोप।"
        assert vet_short_description(text) == (text, None)

    def test_an_empty_value_is_rejected_never_written(self):
        """`CaseListSerializer` ships this field with no fallback to title or
        description, so writing "" renders a blank card on the public list."""
        value, reason = vet_short_description("   ")
        assert value is None
        assert "empty" in reason

    def test_none_is_rejected(self):
        assert vet_short_description(None)[0] is None

    def test_an_over_length_teaser_is_rejected(self):
        value, reason = vet_short_description("क" * 321)
        assert value is None
        assert "too long" in reason

    def test_exactly_the_cap_is_accepted(self):
        text = "क" * ec.MAX_SHORT_DESCRIPTION_CHARS
        assert vet_short_description(text) == (text, None)


# --------------------------------------------------------------------------
# prompt assembly
# --------------------------------------------------------------------------

DETAIL = {
    "slug": "case-081-cr-0091",
    "title": STUB_TITLE,
    "short_description": STUB_SHORT,
    "description": "### क) अभियोगदावीको सार\nकाठमाडौं महानगरपालिकाको ठेक्कामा मिलेमतो।",
    "bigo": 330000000,
    "court_cases": ["https://jawafdehi.org/courtcase/special/081-cr-0091"],
    "key_allegations": ["ठेक्कामा मिलेमतो गरी सार्वजनिक सम्पत्ति हानि पुर्‍याएको।"],
    "entities": [
        {"nes_id": "person/kamal-raj-gautam", "display_name": "कमल राज गौतम",
         "entity_type": "person", "type": "accused", "outcome": "", "notes": ""},
    ],
}


class TestSnippetKeepsTheOutcome:
    """The teaser cannot state a verdict the snippet clamped away.

    A description states the allegation first and the verdict last. Measured over
    the 83 descriptions longer than the 3,000-char budget, on whether the passage
    the model actually receives contains the court's granted acquittal:

        plain head clamp   2/54
        `_verdict_window` 53/54

    `081-CR-0060` is the case that surfaced it -- acquitted, सफाई at offset 5,100,
    and the generated teaser read as a live accusation.
    """

    def _long(self, tail):
        return dict(DETAIL, description=("क) अभियोगदावी। " * 400) + tail)

    def test_a_late_outcome_is_spliced_in(self):
        snippet = _snippet(self._long("\n### ग) विशेष अदालतको फैसलाको सार\nसफाई भएको।"))
        assert "सफाई भएको।" in snippet
        assert "[…]" in snippet, "the elision has to be visible to the model"
        assert len(snippet) <= ec.DESCRIPTION_SNIPPET_BUDGET + 16

    def test_a_headingless_outcome_is_still_found(self):
        """Only 28 of the 83 long descriptions carry the ### ग) heading; most are
        plain prose, so the vocabulary scan carries the real load."""
        snippet = _snippet(self._long("\nविशेष अदालतले प्रतिवादीलाई सफाई दिएको।"))
        assert "सफाई" in snippet
        assert "[…]" in snippet

    def test_a_multi_defendant_verdict_keeps_every_outcome(self):
        """A multi-defendant case states one outcome PER defendant, so any slice
        that ends early keeps the conviction and drops the acquittal.
        `078-CR-0103` reproduced it twice -- सफाई sat 172 characters past where a
        fixed 1,200-char slice ended.
        """
        verdict = ("\n### ग) विशेष अदालतको फैसलाको सार\n"
                   + ("अदालतले प्रतिवादीलाई दोषी ठहर गरेको। " * 40)
                   + "\nदोस्रो प्रतिवादीलाई सफाई दिएको।")
        snippet = _snippet(dict(DETAIL, description=("क) अभियोगदावी। " * 400) + verdict))
        assert "दोषी ठहर" in snippet, "the conviction must survive"
        assert "सफाई दिएको।" in snippet, (
            "the ACQUITTAL is the half a fixed-width slice used to drop")
        assert len(snippet) <= ec.SNIPPET_MAX_CHARS + 16

    def test_the_ceiling_still_bounds_a_runaway_verdict(self):
        """The longest published verdict section is 13,617 chars; one outlier
        judgment must not fill the whole prompt."""
        text = ("क) अभियोगदावी। " * 400) + "\nफैसला: " + ("दोषी ठहर गरेको। " * 2000)
        snippet = _snippet(dict(DETAIL, description=text))
        assert len(snippet) <= ec.SNIPPET_MAX_CHARS + 16

    def test_a_short_verdict_does_not_pad_the_snippet_to_the_ceiling(self):
        """Filling the budget backward stops at a line boundary, so a one-line
        verdict sends one line -- not 4,200 characters of the preceding narrative.

        Be honest about the cost: across the 83 real long descriptions the
        average snippet is 5,119 chars against 3,159 under the old locator. That
        is the price of carrying the verdict, and it is input tokens on the cheap
        tier; the output cap (`CARD_MAX_TOKENS`) is what bounds spend.
        """
        text = ("क) अभियोगदावी। " * 400) + "\nफैसला: सफाई दिएको।"
        assert len(_snippet(dict(DETAIL, description=text))) < 3000

    def test_an_early_outcome_word_does_not_hijack_the_slice(self):
        """Finding 1. The vocabulary scan takes the LAST match, not the first.

        Section क) states the punishment SOUGHT (मागदावी) in this exact
        vocabulary, and narrative reasoning uses ठहर freely. Measured on a real
        case: first match `दोषी` at offset 6,466 in a sentence about
        investigating officials, actual ठहर at 26,224. Taking the first match
        spliced 6,466 onward and handed the model unrelated prose under a prompt
        rule that says STATE THE OUTCOME.
        """
        text = ("क) मागदावी: कैद र जरिवाना माग गरिएको। " * 200
                + "\nविशेष अदालतले प्रतिवादीलाई सफाई दिएको ठहर।")
        snippet = _snippet(dict(DETAIL, description=text))
        assert "सफाई दिएको ठहर।" in snippet, (
            "the real outcome sits at the end; a first-match scan misses it")
        assert "[…]" in snippet

    def test_the_window_ENDS_at_the_last_outcome_word(self):
        """The window is anchored to the END of the outcome discussion and fills
        the budget backward, so every defendant named before the last one still
        reaches the model."""
        text = ("क) अभियोगदावी। " * 400
                + "\nपहिलो प्रतिवादीलाई दोषी ठहर। दोस्रोलाई सफाई भएको।")
        start, end = ec._verdict_window(text)
        assert end >= text.rindex("सफाई"), "the window must reach the last outcome"
        assert start <= text.index("दोषी"), (
            "and must extend back far enough to keep the first defendant")

    def test_an_appeal_section_after_the_verdict_does_not_steal_the_window(self):
        """Review finding 1. `फैसला` is how an appeal section refers BACK to the
        judgment, so a locator that treats it as an outcome word lands past the
        verdict. On the corpus, `पुनरावेदन` follows the last outcome word on 16
        of the 83 long descriptions.

        Before this fix the snippet contained the appeal sentence and NOT the
        acquittal -- worse than no rescue at all, because the plain clamp would
        have kept it.
        """
        text = ("क) अभियोगदावी।\n" * 130
                + "विशेष अदालतले राकेशमान श्रेष्ठलाई सफाई दिएको।\n"
                + "ख) प्रमाणको विवेचना गरिएको।\n" * 250
                + "घ) पुनरावेदनको सार: उक्त फैसलाउपर पुनरावेदन गरेको।")
        snippet = _snippet(dict(DETAIL, description=text))
        assert "सफाई" in snippet, "the acquittal is the whole point of the rescue"
        assert "फैसला" not in ec._OUTCOME_WORD_RE.pattern, (
            "फैसला in the vocabulary is what caused this")

    def test_a_single_paragraph_verdict_is_still_rescued(self):
        """Review finding 2. A description with no newline at all used to cancel
        the rescue silently: the old locator backed up to the previous line start,
        `rfind` returned -1, `+ 1` made that offset 0, and offset 0 reads as "the
        outcome is already in the head". The module's own docstring says most
        descriptions are hand-written prose."""
        text = ("क) अभियोगदावीमा भ्रष्टाचारको आरोप छ। " * 200
                + "विशेष अदालतले प्रतिवादी राकेशमान श्रेष्ठलाई सफाई दिएको।")
        assert "\n" not in text
        snippet = _snippet(dict(DETAIL, description=text))
        assert "[…]" in snippet, "the rescue must fire"
        assert "सफाई" in snippet

    def test_the_ceiling_survives_line_alignment(self):
        """Aligning the window start to a line boundary must move FORWARD.

        Backing up to the previous line start is the obvious reading and grows
        the window by up to a whole line -- it put 50 of the 83 real snippets over
        the ceiling, the worst at 6,998 chars.
        """
        # One 900-char line, then the verdict, so a backward alignment would
        # overshoot by the full line length.
        text = ("क) " + "अभियोगदावीको विस्तृत विवरण। " * 200 + "\n"
                + "अ" * 900 + " अन्ततः प्रतिवादीलाई सफाई दिएको।")
        snippet = _snippet(dict(DETAIL, description=text))
        assert len(snippet) <= ec.SNIPPET_MAX_CHARS + 16, (
            f"snippet is {len(snippet)}, ceiling is {ec.SNIPPET_MAX_CHARS}")

    def test_the_ceg_heading_is_deliberately_not_preferred(self):
        """Measured: preferring the `ग)` heading and reading forward from it
        scores WORSE than ignoring it (51/54 vs 53/54 on carrying the court's
        granted acquittal), because a long verdict discussion gets clipped before
        it finishes. Only 28 of the 83 long descriptions carry the heading at all.
        """
        assert not hasattr(ec, "_OUTCOME_HEADING_RE"), (
            "the heading branch was removed on evidence; re-adding it needs new "
            "evidence, not intuition about clean boundaries")

    def test_a_table_cell_reference_is_not_mistaken_for_a_verdict(self):
        """`081-CR-0060` carries "(ग), (घ) र (ङ) मा उल्लिखित सम्पत्ति" in a table
        at offset 1,740. Splicing from there would present a table row as the
        verdict."""
        detail = dict(DETAIL, description=(
            "क) सुरु। तालिका (ग), (घ) र (ङ) मा उल्लिखित सम्पत्ति। "
            + ("भरण। " * 900)))
        snippet = _snippet(detail)
        assert "[…]" not in snippet, "no outcome exists, so nothing to splice"
        assert len(snippet) == ec.DESCRIPTION_SNIPPET_BUDGET

    def test_an_outcome_already_in_the_head_is_left_alone(self):
        detail = dict(DETAIL, description="सफाई भएको। " + ("पछि। " * 900))
        snippet = _snippet(detail)
        assert "सफाई" in snippet
        assert "[…]" not in snippet, "a plain clamp already keeps it"

    def test_a_long_description_with_no_outcome_is_plainly_clamped(self):
        snippet = _snippet(self._long("कुनै फैसला उल्लेख नभएको।".replace("फैसला", "निर्णय")))
        assert "[…]" not in snippet
        assert len(snippet) == ec.DESCRIPTION_SNIPPET_BUDGET

    def test_a_short_description_is_returned_whole(self):
        detail = dict(DETAIL, description="छोटो विवरण। सफाई भएको।")
        assert _snippet(detail) == "छोटो विवरण। सफाई भएको।"

    def test_the_prompt_requires_the_outcome_when_one_exists(self):
        assert "STATE THE OUTCOME" in ec.SYSTEM_PROMPT
        assert "सफाई" in ec.SYSTEM_PROMPT
        assert "do not guess" in ec.SYSTEM_PROMPT


class TestPromptAssembly:
    def test_the_court_number_reaches_the_model_uppercased(self):
        """All 50 PUBLISHED titles carrying a case number use `(081-CR-0060)`.

        `court_number()` reads the lowercase canonical IRI, and the model copies
        whatever it is given, so the lowercase form shipped `(081-cr-0060)` on 2
        of 5 cases in the 2026-08-04 evaluation. `validate_title` compares
        case-insensitively and cannot catch it.
        """
        prompt = _build_prompt(DETAIL, "081-cr-0091", True, True)
        assert "081-CR-0091" in prompt
        assert "081-cr-0091" not in prompt

    def test_the_prompt_carries_every_context_block(self):
        content = _build_prompt(DETAIL, "081-cr-0091", True, True)
        assert STUB_TITLE in content
        # Uppercased on the way in -- see
        # test_the_court_number_reaches_the_model_uppercased.
        assert "081-CR-0091" in content
        assert "330,000,000" in content
        assert "ठेक्कामा मिलेमतो" in content
        assert "[accused] कमल राज गौतम" in content
        assert "अभियोगदावीको सार" in content

    def test_asking_for_both_says_so(self):
        content = _build_prompt(DETAIL, "081-cr-0091", True, True)
        assert "Produce the title AND the short_description." in content

    def test_asking_for_one_tells_the_model_to_null_the_other(self):
        content = _build_prompt(DETAIL, "081-cr-0091", True, False)
        assert "Only the title is needed" in content
        content = _build_prompt(DETAIL, "081-cr-0091", False, True)
        assert "Only the short_description is needed" in content

    def test_a_missing_court_number_renders_as_unknown(self):
        assert "(unknown)" in _build_prompt(DETAIL, "", True, False)

    def test_the_description_snippet_is_capped(self):
        detail = dict(DETAIL, description="क" * 5000)
        assert len(_snippet(detail)) == ec.DESCRIPTION_SNIPPET_BUDGET

    def test_no_description_renders_as_none(self):
        assert _snippet(dict(DETAIL, description="")) == "(none)"


def test_generate_uses_the_cheap_tier_and_the_card_token_budget():
    seen = {}

    def stub(**kw):
        seen.update(kw)
        return json.dumps({"title": GOOD_TITLE, "short_description": "सार।"})

    result = _generate(DETAIL, "081-CR-0091", True, True, stub, usage=None)
    assert result["title"] == GOOD_TITLE
    assert seen["tier"] == "cheap"
    assert seen["max_tokens"] == ec.CARD_MAX_TOKENS
    assert seen["system"] == ec.SYSTEM_PROMPT


def test_generate_accepts_a_reply_carrying_only_one_key():
    """A `--only title` run gets `{"title": ..., "short_description": null}`.
    Demanding both keys would reject every single-field reply."""
    body = json.dumps({"title": GOOD_TITLE, "short_description": None})
    result = _generate(DETAIL, "081-CR-0091", True, False,
                       lambda **kw: body, usage=None)
    assert result["title"] == GOOD_TITLE


def test_generate_returns_none_for_an_object_with_neither_key():
    assert _generate(DETAIL, "081-CR-0091", True, True,
                     lambda **kw: '{"other": 1}', usage=None) is None


def test_generate_returns_none_for_unparseable_text():
    assert _generate(DETAIL, "081-CR-0091", True, True,
                     lambda **kw: "sorry, no", usage=None) is None


# --------------------------------------------------------------------------
# main() -- integration over a stubbed API + LLM
# --------------------------------------------------------------------------

CASE_STUB_BOTH = {
    "slug": "case-stub-both",
    "title": STUB_TITLE,
    "short_description": STUB_SHORT,
    "state": "DRAFT",
    "description": "### क) अभियोगदावीको सार\n" + "काठमाडौंको ठेक्कामा मिलेमतो। " * 30,
    "bigo": 330000000,
    "court_cases": ["https://jawafdehi.org/courtcase/special/076-cr-0182"],
    "key_allegations": ["ठेक्कामा मिलेमतो गरेको।"],
    "entities": [],
}

CASE_GOOD_TITLE = dict(
    CASE_STUB_BOTH, slug="case-good-title", title=GOOD_TITLE,
    court_cases=["https://jawafdehi.org/courtcase/special/081-cr-0091"],
)

CASE_NO_DESCRIPTION = dict(
    CASE_STUB_BOTH, slug="case-no-description", description="", key_allegations=[])

CASE_NO_COURT_NUMBER = dict(
    CASE_STUB_BOTH, slug="case-no-court-number", court_cases=[])


class _StubApi:
    def __init__(self, cases, etag='W/"etag-1"'):
        self._cases = {c["slug"]: dict(c) for c in cases}
        self._etag = etag
        self.patched = []
        self.requests = []

    def iter_cases(self, params=None, timeout=60):
        yield from self._cases.values()

    def get_case_with_etag(self, slug, timeout=60):
        return self._cases[slug], self._etag

    def patch_field(self, slug, field, value, timeout=60, if_match=None):
        self.patched.append((slug, field, value, if_match))
        self._cases[slug][field] = value
        return {}

    def patch_fields(self, slug, pairs, timeout=60, if_match=None):
        """One request, several fields -- what `enrich_card` actually calls.

        `patched` still gets one entry per field so the assertions here read the
        same as before; `requests` counts the HTTP calls, which is what tells a
        multi-op write apart from a loop.
        """
        pairs = list(pairs)
        if not pairs:
            return {}
        self.requests.append((slug, [f for f, _ in pairs], if_match))
        for field, value in pairs:
            self.patched.append((slug, field, value, if_match))
            self._cases[slug][field] = value
        return {}


class _EtagEnforcingApi(_StubApi):
    """`_StubApi` with the server's actual optimistic-concurrency semantics.

    Every successful write mints a NEW ETag, and a PATCH carrying a stale
    `If-Match` is refused — which is what `/api/cases/{slug}/` does. `_StubApi`
    returns one constant ETag and ignores `If-Match` altogether, and that is
    precisely why a full green suite coexisted with a script that could never
    write both of its fields: the 2026-08-04 local smoke run wrote `title`, then
    took `HTTP Error 412: Precondition Failed` on `short_description` for every
    case. Any future two-PATCH enricher should be tested against this, not the
    permissive double.
    """

    def __init__(self, cases):
        super().__init__(cases)
        self._version = 1

    def _bump(self, if_match):
        if if_match is not None and if_match != self._etag:
            raise RuntimeError("HTTP Error 412: Precondition Failed")
        self._version += 1
        self._etag = f'W/"etag-{self._version}"'

    def patch_field(self, slug, field, value, timeout=60, if_match=None):
        self._bump(if_match)
        return super().patch_field(slug, field, value, timeout, if_match)

    def patch_fields(self, slug, pairs, timeout=60, if_match=None):
        pairs = list(pairs)
        if pairs:
            self._bump(if_match)
        return super().patch_fields(slug, pairs, timeout, if_match)


class _FakeUsage:
    def __init__(self):
        self.calls = 0

    def as_dict(self):
        return {"by_provider": []}


def _llm(*, title=GOOD_TITLE, short="काठमाडौं ठेक्कामा रु ३३ करोड हिनामिना भएको आरोप।",
         adequate=False):
    """One stub serving both call sites.

    `main()` makes TWO kinds of call per case -- the adequacy judge, then the
    generation -- and they are told apart by the judge's system prompt, not by
    call order: a `--only title` run never calls the judge at all, so ordering
    would make the stub silently answer the wrong question.
    """
    calls = []

    def stub(**kw):
        calls.append(kw)
        if "data-quality reviewer" in kw.get("system", ""):
            return json.dumps({"adequate": adequate, "reason": "stub verdict"})
        payload = {}
        if title is not None:
            payload["title"] = title
        if short is not None:
            payload["short_description"] = short
        return json.dumps(payload)

    stub.calls = calls
    return stub


def _run_main(monkeypatch, api, invoke_text_stub, argv):
    monkeypatch.setattr(ec, "build_api", lambda args: api)
    monkeypatch.setattr(ec, "bootstrap", lambda *a, **k: None)

    fake_llm_invoke = types.ModuleType("llm.invoke")
    fake_llm_invoke.invoke_text = invoke_text_stub

    fake_llm_usage = types.ModuleType("llm.usage")
    fake_llm_usage.UsageAccumulator = _FakeUsage
    fake_llm_usage.render_usage_table = lambda by_provider, title=None: ""

    monkeypatch.setitem(sys.modules, "llm.invoke", fake_llm_invoke)
    monkeypatch.setitem(sys.modules, "llm.usage", fake_llm_usage)

    return ec.main(argv)


BASE_ARGV = ["--api-base-url", "http://127.0.0.1:48010"]


def test_both_fields_are_written_on_a_template_stub_case(monkeypatch):
    api = _StubApi([CASE_STUB_BOTH])
    _run_main(monkeypatch, api, _llm(title="काठमाडौं ठेक्का घोटाला (076-CR-0182)"),
              BASE_ARGV + ["--apply"])
    fields = [f for _, f, _, _ in api.patched]
    assert sorted(fields) == ["short_description", "title"]
    assert all(etag == 'W/"etag-1"' for *_, etag in api.patched)


def test_only_title_writes_the_title_alone(monkeypatch):
    api = _StubApi([CASE_STUB_BOTH])
    _run_main(monkeypatch, api, _llm(title="काठमाडौं ठेक्का घोटाला (076-CR-0182)"),
              BASE_ARGV + ["--only", "title", "--apply"])
    assert [f for _, f, _, _ in api.patched] == ["title"]


def test_only_title_leaves_the_description_untouched(monkeypatch):
    """The standalone-title path the stripped description pass used to cover.

    A case with a good description must come out with that description
    byte-identical -- this script fetches no documents and must never write
    that field.
    """
    before = CASE_STUB_BOTH["description"]
    api = _StubApi([CASE_STUB_BOTH])
    _run_main(monkeypatch, api, _llm(title="काठमाडौं ठेक्का घोटाला (076-CR-0182)"),
              BASE_ARGV + ["--only", "title", "--apply"])
    assert api._cases["case-stub-both"]["description"] == before
    assert "description" not in {f for _, f, _, _ in api.patched}


def test_only_title_never_calls_the_adequacy_judge(monkeypatch):
    """The judge is a real LLM call. A title-only run must not pay for it."""
    api = _StubApi([CASE_STUB_BOTH])
    stub = _llm(title="काठमाडौं ठेक्का घोटाला (076-CR-0182)")
    _run_main(monkeypatch, api, stub, BASE_ARGV + ["--only", "title", "--dry-run"])
    assert not any("data-quality reviewer" in c.get("system", "") for c in stub.calls)


def test_only_short_description_writes_that_field_alone(monkeypatch):
    api = _StubApi([CASE_STUB_BOTH])
    _run_main(monkeypatch, api, _llm(), BASE_ARGV +
              ["--only", "short_description", "--apply"])
    assert [f for _, f, _, _ in api.patched] == ["short_description"]


def test_a_good_title_is_left_alone(monkeypatch):
    """`title_is_acceptable` passes, so no title call and no title write."""
    api = _StubApi([CASE_GOOD_TITLE])
    report = _run_main(monkeypatch, api, _llm(adequate=True),
                       BASE_ARGV + ["--only", "title", "--apply"])
    assert api.patched == []
    assert {r["status"] for r in report.rows} == {"already"}


def test_an_adequate_short_description_is_left_alone(monkeypatch):
    api = _StubApi([CASE_GOOD_TITLE])
    report = _run_main(monkeypatch, api, _llm(adequate=True),
                       BASE_ARGV + ["--only", "short_description", "--apply"])
    assert api.patched == []
    assert {r["status"] for r in report.rows} == {"already"}


def test_force_overrides_both_gates(monkeypatch):
    """Both fields are already fine, so only --force reaches the model."""
    api = _StubApi([CASE_GOOD_TITLE])
    _run_main(monkeypatch, api, _llm(adequate=True),
              BASE_ARGV + ["--force", "--apply"])
    assert sorted(f for _, f, _, _ in api.patched) == ["short_description", "title"]


def test_force_skips_the_adequacy_judge_entirely(monkeypatch):
    """--force means regenerate, so asking the judge would be a wasted call."""
    api = _StubApi([CASE_GOOD_TITLE])
    stub = _llm(adequate=True)
    _run_main(monkeypatch, api, stub, BASE_ARGV + ["--force", "--dry-run"])
    assert not any("data-quality reviewer" in c.get("system", "") for c in stub.calls)


def test_dry_run_writes_nothing(monkeypatch):
    api = _StubApi([CASE_STUB_BOTH])
    report = _run_main(monkeypatch, api,
                       _llm(title="काठमाडौं ठेक्का घोटाला (076-CR-0182)"),
                       BASE_ARGV + ["--dry-run"])
    assert api.patched == []
    assert any(r["status"] == "would-enrich" for r in report.rows)


def test_an_over_length_title_is_reported_and_not_written(monkeypatch):
    api = _StubApi([CASE_STUB_BOTH])
    report = _run_main(
        monkeypatch, api, _llm(title="क" * 250 + " (076-CR-0182)"),
        BASE_ARGV + ["--only", "title", "--apply"],
    )
    assert api.patched == []
    rejected = [r for r in report.rows if r["status"] == "rejected"]
    assert rejected and "too long" in rejected[0]["reason"]


def test_an_empty_short_description_is_never_patched(monkeypatch):
    api = _StubApi([CASE_STUB_BOTH])
    report = _run_main(monkeypatch, api, _llm(short="   "),
                       BASE_ARGV + ["--only", "short_description", "--apply"])
    assert api.patched == []
    assert any(r["status"] == "rejected" for r in report.rows)


def test_a_case_without_a_description_is_unmet(monkeypatch):
    api = _StubApi([CASE_NO_DESCRIPTION])
    stub = _llm()
    report = _run_main(monkeypatch, api, stub, BASE_ARGV + ["--dry-run"])
    assert {r["status"] for r in report.rows} == {"unmet"}
    assert api.patched == []
    assert stub.calls == [], "an unmet prerequisite must cost zero LLM calls"


def test_a_case_without_a_court_number_skips_the_title(monkeypatch):
    """No court number means no title can satisfy the format contract, so
    regenerating would burn a call on a guaranteed rejection."""
    api = _StubApi([CASE_NO_COURT_NUMBER])
    report = _run_main(monkeypatch, api, _llm(),
                       BASE_ARGV + ["--only", "title", "--dry-run"])
    assert api.patched == []
    reasons = [r["reason"] for r in report.rows if r["status"] == "unmet"]
    assert any("special-court number" in r for r in reasons)


def test_a_rejected_title_does_not_block_the_short_description(monkeypatch):
    """The two fields are independent writes. One bad headline must not cost the
    case its card teaser."""
    api = _StubApi([CASE_STUB_BOTH])
    _run_main(monkeypatch, api, _llm(title="शीर्षक without a number"),
              BASE_ARGV + ["--apply"])
    assert [f for _, f, _, _ in api.patched] == ["short_description"]


def test_both_fields_land_in_one_patch_when_the_server_enforces_if_match(monkeypatch):
    """DEVIATION 6, and the regression test for the bug that motivated it.

    The donor shape -- one PATCH per field -- took a 412 on the second write for
    every case, because the first write had already changed the ETag. Asserting
    ONE request is the point: a passing "both fields are present" check would
    also pass for a two-request version that refreshes the ETag in between, and
    that version is the one that cannot be atomic.

    Invisible to any dry run, which issues no PATCH and reports `would-enrich`
    for both fields.
    """
    api = _EtagEnforcingApi([CASE_STUB_BOTH])
    report = _run_main(monkeypatch, api, _llm(title=MATCHING_TITLE),
                       BASE_ARGV + ["--apply"])

    assert len(api.requests) == 1, "both fields must go in ONE conditional PATCH"
    slug, fields, if_match = api.requests[0]
    assert fields == ["title", "short_description"]
    assert if_match, "the write must stay conditional"
    assert not [r for r in report.rows if r["status"] == "error"]
    assert api._cases[slug]["title"] == MATCHING_TITLE


def test_a_rejected_title_leaves_the_teaser_in_a_single_op_patch(monkeypatch):
    """Per-field independence survives the merge into one request.

    It lives in vetting, not in the number of PATCHes: a title that fails
    `vet_title` is left out of the op list and the teaser still lands.
    """
    api = _EtagEnforcingApi([CASE_STUB_BOTH])
    _run_main(monkeypatch, api, _llm(title="शीर्षक without a number"),
              BASE_ARGV + ["--apply"])

    assert len(api.requests) == 1
    assert api.requests[0][1] == ["short_description"]


def test_a_failed_patch_reports_every_field_as_failed(monkeypatch):
    """The write is atomic, so the failure has to be reported that way.

    Reporting one field written and one failed would describe a state the server
    never held.
    """
    class _FailingApi(_EtagEnforcingApi):
        def patch_fields(self, slug, pairs, timeout=60, if_match=None):
            raise RuntimeError("HTTP Error 500: Internal Server Error")

    api = _FailingApi([CASE_STUB_BOTH])
    report = _run_main(monkeypatch, api, _llm(title=MATCHING_TITLE),
                       BASE_ARGV + ["--apply"])

    assert api.patched == []
    errors = [r for r in report.rows if r["status"] == "error"]
    assert len(errors) == 2, "both fields are unwritten, so both are errors"


def test_an_unparseable_reply_is_skipped_not_written(monkeypatch):
    api = _StubApi([CASE_STUB_BOTH])
    report = _run_main(monkeypatch, api, lambda **kw: "no json here",
                       BASE_ARGV + ["--apply"])
    assert api.patched == []
    assert any(r["status"] == "skipped" for r in report.rows)


def test_an_llm_exception_is_recorded_as_error(monkeypatch):
    def boom(**kw):
        if "data-quality reviewer" in kw.get("system", ""):
            return json.dumps({"adequate": False, "reason": "stub"})
        raise RuntimeError("provider exploded")

    api = _StubApi([CASE_STUB_BOTH])
    report = _run_main(monkeypatch, api, boom, BASE_ARGV + ["--apply"])
    assert api.patched == []
    errors = [r for r in report.rows if r["status"] == "error"]
    assert errors and "provider exploded" in errors[0]["reason"]


def test_a_patch_failure_is_recorded_and_the_run_continues(monkeypatch):
    """One case's failed write must not end the batch.

    This used to assert that a failed `title` write let `short_description`
    through. DEVIATION 6 removed that: the two fields share one atomic PATCH, so
    a failure loses both (see
    `test_a_failed_patch_reports_every_field_as_failed`). What still has to hold
    -- and what the original test was really protecting -- is that the RUN keeps
    going to the next case.
    """
    class _FailingApi(_StubApi):
        def patch_fields(self, slug, pairs, timeout=60, if_match=None):
            if slug == "case-stub-both":
                raise RuntimeError("412 stale")
            return super().patch_fields(slug, pairs, timeout, if_match)

    api = _FailingApi([CASE_STUB_BOTH, CASE_GOOD_TITLE])
    report = _run_main(monkeypatch, api,
                       _llm(title="काठमाडौं ठेक्का घोटाला (076-CR-0182)"),
                       BASE_ARGV + ["--apply"])

    assert any("412 stale" in r["reason"] for r in report.rows)
    written = {s for s, _, _, _ in api.patched}
    assert "case-stub-both" not in written, "the failed write must not be recorded"
    assert "case-good-title" in written, (
        "the other case must still be written after this one failed")


# --------------------------------------------------------------------------
# the review file
# --------------------------------------------------------------------------


def _review_file(tmp_path):
    files = sorted((tmp_path / "reviews").glob("*.md"))
    assert files, "every run must write exactly one review file"
    return files[-1]


def test_the_review_file_shows_the_stub_before_and_the_headline_after(
    monkeypatch, tmp_path
):
    api = _StubApi([CASE_STUB_BOTH])
    new_title = "काठमाडौं ठेक्का घोटाला (076-CR-0182)"
    _run_main(monkeypatch, api, _llm(title=new_title), BASE_ARGV + ["--dry-run"])
    text = _review_file(tmp_path).read_text(encoding="utf-8")
    assert STUB_TITLE in text          # before
    assert new_title in text           # generated
    assert "DRY RUN" in text
    assert "title + short_description" in text


def test_the_review_file_names_the_description_as_the_source_not_a_material(
    monkeypatch, tmp_path
):
    """This stage fetches no document. Printing a material IRI beside the
    generated headline would misattribute where it came from."""
    api = _StubApi([CASE_STUB_BOTH])
    _run_main(monkeypatch, api, _llm(title="काठमाडौं ठेक्का घोटाला (076-CR-0182)"),
              BASE_ARGV + ["--dry-run"])
    text = _review_file(tmp_path).read_text(encoding="utf-8")
    assert "case description (no document fetched)" in text
    assert "jawafdehi.org/material/" not in text


def test_the_review_file_records_a_rejection_with_its_reason(monkeypatch, tmp_path):
    api = _StubApi([CASE_STUB_BOTH])
    _run_main(monkeypatch, api, _llm(title="क" * 250 + " (076-CR-0182)"),
              BASE_ARGV + ["--only", "title", "--dry-run"])
    text = _review_file(tmp_path).read_text(encoding="utf-8")
    assert "rejected (title)" in text
    assert "too long" in text


def test_the_review_file_keeps_devanagari_unescaped(monkeypatch, tmp_path):
    api = _StubApi([CASE_STUB_BOTH])
    _run_main(monkeypatch, api, _llm(title="काठमाडौं ठेक्का घोटाला (076-CR-0182)"),
              BASE_ARGV + ["--dry-run"])
    text = _review_file(tmp_path).read_text(encoding="utf-8")
    assert "काठमाडौं" in text
    assert "\\u0915" not in text


# --------------------------------------------------------------------------
# guard wiring
# --------------------------------------------------------------------------


def test_dry_run_is_the_default():
    assert build_parser().parse_args([]).dry_run is True
    assert build_parser().parse_args(["--apply"]).dry_run is False


def test_only_defaults_to_both():
    assert build_parser().parse_args([]).only == "both"


def test_only_rejects_an_unknown_field():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--only", "description"])


def test_build_api_refuses_a_remote_write_by_default():
    class _Args:
        api_base_url = "https://api.jawafdehi.org"
        api_token = "tok"
        allow_remote_writes = False

    api = ec.build_api(_Args())
    with pytest.raises(RuntimeError, match="refusing to write"):
        api.patch_field("case-x", "title", GOOD_TITLE)


def test_add_common_args_is_wired():
    """Sanity: the shared flags this script relies on all parse."""
    args = build_parser().parse_args(
        ["--limit", "3", "--force", "--verbose"])
    assert args.limit == 3
    assert args.force is True
    assert isinstance(build_parser(), argparse.ArgumentParser)


class TestPredicateRejectsAnAllNullObject:
    """Finding 4. Key PRESENCE is not a usable field.

    The system prompt tells the model to "set an unrequested key to null", so
    `{"title": null, "short_description": null}` is a shape the prompt invites.
    A presence test accepted it, stopping `parse_object_response`'s scan on an
    object carrying nothing -- while the real object may have been the next `{`.
    """

    def test_an_all_null_object_is_not_a_usable_field(self):
        assert ec._carries_a_field({"title": None, "short_description": None}) is False

    def test_a_blank_string_is_not_a_usable_field(self):
        assert ec._carries_a_field({"title": "   ", "short_description": ""}) is False

    def test_one_populated_field_is_enough(self):
        """`--only title` legitimately nulls the other key."""
        assert ec._carries_a_field({"title": "शीर्षक (081-CR-0091)",
                                    "short_description": None}) is True
        assert ec._carries_a_field({"title": None,
                                    "short_description": "सारांश।"}) is True

    def test_the_scan_walks_past_an_all_null_object_to_the_real_one(self):
        """The behaviour the predicate exists for, end to end."""
        response = (
            '{"title": null, "short_description": null}\n'
            '{"title": "असली शीर्षक (081-CR-0091)", "short_description": "असली सार।"}'
        )
        obj = ec.parse_object_response(response, predicate=ec._carries_a_field)
        assert obj["title"] == "असली शीर्षक (081-CR-0091)"


def test_a_failed_detail_fetch_skips_the_case_and_never_writes(monkeypatch):
    """Finding 2. A fetch failure must not fall back to the LIST payload.

    `CaseSerializer` is the list serializer's child, so the list payload carries
    `title`, `short_description` AND `description` -- and `STAGES["card"]`
    declares no `requires_materials`, so nothing else would stop the run. The old
    fallback generated from a page-cached description and PATCHed with
    `if_match=None`, because the failed fetch is what left the ETag unset.
    """
    class _FetchFails(_StubApi):
        def get_case_with_etag(self, slug):
            raise RuntimeError("HTTP Error 502: Bad Gateway")

    api = _FetchFails([CASE_STUB_BOTH])
    _run_main(monkeypatch, api, _llm(title="काठमाडौं ठेक्का घोटाला (076-CR-0182)"),
              ["--apply", "--allow-remote-writes"])
    assert api.requests == [], "no PATCH may be attempted without a fresh ETag"


class TestTheSplitVerdictRule:
    """The teaser must not convict a defendant the court cleared.

    Measured on production: 1,250 of 3,003 cases (42%) name co-defendants in the
    title, and 10 of the 35 published descriptions that state a decision state
    BOTH दोषी and सफाई -- roughly three in ten decided cases hand different
    outcomes to different people. `078-CR-0103` is the case that surfaced it: the
    model was shown the acquittal (after `SNIPPET_MAX_CHARS` widened the window)
    and still wrote a teaser naming only the conviction, because the prompt asked
    it to "state the outcome", singular.
    """

    def test_the_prompt_demands_the_split_be_named(self):
        assert "DIFFERENT OUTCOMES" in ec.SYSTEM_PROMPT
        assert "सफाई" in ec.SYSTEM_PROMPT

    def test_the_rule_names_the_harm_not_just_the_format(self):
        """A rule the model can follow mechanically without understanding why
        gets dropped under length pressure. This one says who is hurt."""
        assert "CLEARED must never read as convicted" in ec.SYSTEM_PROMPT

    def test_the_outcome_rules_still_forbid_inventing_a_verdict(self):
        """The split rule must not weaken the no-guessing rule beside it."""
        assert "do not guess" in ec.SYSTEM_PROMPT
        assert "अनुसन्धान जारी" in ec.SYSTEM_PROMPT


def test_batch_csv_reaches_selection(monkeypatch, tmp_path):
    """`--batch-csv` selects through `select_for_run` (#410), not a local slice.

    Pins the wiring, not the loader: `tests/casework/test_select_batch_csv.py`
    owns the parsing. What this asserts is that THIS enricher routes selection
    through the shared path, so a batch file restricts the run here the same way
    it does on the five enrichers already on main.
    """
    csv = tmp_path / "batch.csv"
    csv.write_text("slug\n076-cr-0182-nope\n", encoding="utf-8")
    args = build_parser().parse_args(["--batch-csv", str(csv)])
    assert args.batch_csv == str(csv)
    assert "select_for_run" in Path(ec.__file__).read_text(encoding="utf-8")
