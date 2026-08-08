"""Tests for `casework/common/missing_details.py`.

The module is deliberately free of LLM and API dependencies, so every rule here
is a pure-function assertion. End-to-end paths through `enrich_description` live
in `tests/casework/test_enrich_description.py`.

The published corpus is the spec. Where a test pins an exact string it is because
that string appears VERBATIM in a published case -- editing it is a deliberate
divergence from house style, not a typo, and the test says which case it came
from so the claim can be rechecked against production.
"""

import pytest

from casework.common.missing_details import (
    APPEAL_ITEM,
    CHARGE_SHEET_ITEM,
    LETTERS,
    MAX_CHARS,
    MAX_ITEM_CHARS,
    MAX_ITEMS,
    MAX_LLM_ITEMS,
    NUMERALS,
    accept_items,
    bound_types,
    build,
    floor_items,
    has_charge_sheet,
    has_supreme_reference,
    has_verdict,
    held_summary,
    reject_item,
    render,
)


def _case(types=("ciaa_press_release", "court_order"), court_cases=None, **extra):
    """A case payload shaped like the DETAIL endpoint's."""
    return {
        "court_cases": (["https://jawafdehi.org/courtcase/special/078-cr-0038"]
                        if court_cases is None else court_cases),
        "evidence": [{"material_iri": f"https://jawafdehi.org/material/{t}/1",
                      "material": {"material_type": t}} for t in types],
        **extra,
    }


PRESS_AND_VERDICT = _case()


# --------------------------------------------------------------------------
# Shape detection
# --------------------------------------------------------------------------


def test_bound_types_reads_the_resolved_material_not_the_iri():
    """`material_type` lives on the RESOLVED material dict. A LIST payload leaves
    `material` null, and must NOT be mistaken for a case with no evidence-derived
    shape -- it falls through to "not templated" instead of guessing."""
    assert bound_types(PRESS_AND_VERDICT) == {"ciaa_press_release", "court_order"}
    list_payload = {"evidence": [{"material_iri": "https://x/material/court_order/1",
                                  "material": None}]}
    assert bound_types(list_payload) == set()
    assert has_verdict(list_payload) is False


def test_has_verdict_only_counts_court_order():
    assert has_verdict(PRESS_AND_VERDICT) is True
    assert has_verdict(_case(types=("ciaa_press_release",))) is False
    assert has_verdict(_case(types=("charge_sheet", "news"))) is False


def test_has_charge_sheet():
    assert has_charge_sheet(PRESS_AND_VERDICT) is False
    assert has_charge_sheet(_case(types=("charge_sheet", "court_order"))) is True


def test_has_supreme_reference_matches_only_the_supreme_court():
    assert has_supreme_reference(PRESS_AND_VERDICT) is False
    assert has_supreme_reference(_case(court_cases=[
        "https://jawafdehi.org/courtcase/special/081-cr-0091",
        "https://jawafdehi.org/courtcase/supreme/081-cr-2319"])) is True


def test_has_supreme_reference_tolerates_a_null_entry():
    assert has_supreme_reference(_case(court_cases=[None, ""])) is False


# --------------------------------------------------------------------------
# The prompt inventory
# --------------------------------------------------------------------------


def test_held_summary_lists_what_we_hold_in_nepali():
    """Nepali labels: the model answers in Nepali, and a mixed-script inventory
    invites it to echo the English type name into public prose."""
    assert held_summary(PRESS_AND_VERDICT) == (
        "अख्तियारको प्रेस विज्ञप्ति, विशेष अदालतको फैसला")


def test_held_summary_counts_duplicates():
    assert "समाचार x3" in held_summary(
        _case(types=("court_order", "news", "news", "news")))


def test_held_summary_says_so_when_nothing_is_bound():
    assert held_summary({"evidence": []}) == "(कुनै पनि छैन)"


# --------------------------------------------------------------------------
# The deterministic floor
# --------------------------------------------------------------------------


def test_floor_is_both_items_for_the_production_batch_shape():
    """Press release + verdict, no charge sheet, no Supreme ref. 24 of the 25
    cases in the first production batch have exactly this shape."""
    assert floor_items(PRESS_AND_VERDICT) == [CHARGE_SHEET_ITEM, APPEAL_ITEM]


def test_a_bound_charge_sheet_drops_the_charge_sheet_item():
    assert floor_items(_case(types=("charge_sheet", "court_order"))) == [APPEAL_ITEM]


def test_a_supreme_reference_drops_the_appeal_item():
    case = _case(court_cases=["https://jawafdehi.org/courtcase/supreme/081-cr-2319"])
    assert floor_items(case) == [CHARGE_SHEET_ITEM]


def test_no_verdict_means_no_floor_at_all():
    assert floor_items(_case(types=("ciaa_press_release",))) == []


def test_both_satisfied_means_an_empty_floor():
    case = _case(types=("charge_sheet", "court_order"),
                 court_cases=["https://jawafdehi.org/courtcase/supreme/081-cr-2319"])
    assert floor_items(case) == []


# --------------------------------------------------------------------------
# Enumerators -- one rule per corpus example
# --------------------------------------------------------------------------


def test_one_item_renders_bare():
    """`mishra-revenue-leakage-malpot-parsa-080-cr-0061`, 65 chars, no enumerator."""
    assert render(["एउटै वस्तु"]) == "एउटै वस्तु"


def test_two_items_take_devanagari_numerals():
    """`raju-puri-080-cr-0007-illegal-assets` and the four gajendra-maharjan cases."""
    assert render(["क वस्तु", "ख वस्तु"]) == "१. क वस्तु\n२. ख वस्तु"


def test_three_items_take_nepali_letters():
    """`case-080-cr-0196-baikuntha-aryal` (3 items) and `bara-hulak-081-CR-0091`."""
    assert render(["एक", "दुई", "तीन"]) == "क) एक\nख) दुई\nग) तीन"


def test_render_drops_blanks_and_trims():
    assert render(["  एक  ", "", None, "   "]) == "एक"


def test_render_of_nothing_is_empty_string():
    assert render([]) == ""
    assert render(None) == ""


# --------------------------------------------------------------------------
# Item acceptance
# --------------------------------------------------------------------------


def test_a_specific_document_is_accepted():
    """Verbatim from `case-081-cr-0097-d8f6d5b2`."""
    assert reject_item("३ चरणका ठेक्का सम्झौताका प्रतिलिपि", PRESS_AND_VERDICT) is None


def test_a_held_document_is_rejected():
    """THE CHECKABLE GROUNDING RULE. The model was shown the inventory; a claim
    that a bound document is missing is contradicted by our own data."""
    reason = reject_item("प्रेस विज्ञप्ति", PRESS_AND_VERDICT)
    assert reason and "bound as evidence" in reason


def test_claiming_the_verdict_copy_is_missing_is_rejected_when_bound():
    reason = reject_item("विशेष अदालतको फैसलाको प्रतिलिपि", PRESS_AND_VERDICT)
    assert reason and "bound as evidence" in reason


@pytest.mark.parametrize("item", [
    # Every one of these was ACCEPTED under the old `len(word)/len(item) >= 0.5`
    # ratio. The prompt demands "the document, with its date, party, phase, or
    # number", and each qualifier pushed the ratio down -- so the rule stopped
    # firing precisely on the specific items it was written for. The more specific
    # the wrong claim, the more likely it published.
    "मिति २०८१।०५।१२ को अख्तियारको प्रेस विज्ञप्ति",
    "विशेष अदालत काठमाडौंको फैसला (०८१-CR-००९१)",
    "विशेष अदालतले मिति २०८१।१२।१८ मा गरेको फैसलाको पूर्ण पाठ",
    "विशेष अदालतको फैसलाको प्रतिलिपि",
])
def test_a_held_document_is_rejected_however_specifically_it_is_named(item):
    reason = reject_item(item, PRESS_AND_VERDICT)
    assert reason and "bound as evidence" in reason, f"{item!r} slipped through"


def test_a_document_referenced_by_a_held_one_is_still_a_different_document():
    """Nepali is head-final, so the head noun is last. `…अभियोगपत्रमा उल्लेखित संलग्न
    अनुसूची` has `अनुसूची` as its head -- an annex the charge sheet references, not
    the charge sheet. Tested with the charge sheet BOUND, because otherwise the
    floor already names it and the restatement rule fires first (correctly)."""
    bound = _case(types=("press_release", "court_order", "charge_sheet"))
    assert reject_item("अभियोगपत्रमा उल्लेखित संलग्न अनुसूची", bound) is None
    assert reject_item("प्रेस विज्ञप्तिमा उल्लेखित बैंक विवरण", bound) is None
    # ...but the document itself, and any copy of it, still go.
    for item in ["अभियोगपत्र", "अख्तियारले दायर गरेको अभियोगपत्रको प्रतिलिपि"]:
        assert reject_item(item, bound), f"{item!r} slipped through"


@pytest.mark.parametrize("item", ["विशेष अदालतको फैसला", "अदालतको फैसला", "फैसला"])
def test_the_bare_verdict_phrasings_are_rejected_too(item):
    """`held_summary` prints `विशेष अदालतको फैसला` into the prompt, so echoing that
    label back is the model's most likely wrong answer. An earlier version listed
    only the possessive `फैसलाको …` forms, which let the most probable wrong answer
    through as the one answer that passed."""
    reason = reject_item(item, PRESS_AND_VERDICT)
    assert reason and "bound as evidence" in reason, f"{item!r} slipped through"


@pytest.mark.parametrize("item", [
    "सर्वोच्च अदालतको फैसला",
    "सर्वोच्च अदालतको फैसलाको प्रतिलिपि",
    "उच्च अदालत पाटनको आदेश",
    # The appeal-restatement rule used to pre-empt the exemption and drop this
    # one as "restates the appeal item" -- on every case without a Supreme
    # reference, i.e. 24 of the first batch's 25, which made the
    # `पुनरावेदन अदालत` entry in OTHER_COURT_WORDS dead code.
    "पुनरावेदन अदालत पाटनको फैसलाको प्रतिलिपि",
    "पुनरावेदन अदालतको आदेश",
])
def test_another_courts_decision_is_a_different_document(item):
    """We hold the SPECIAL court's verdict. `अदालतको फैसला` is a substring of
    `सर्वोच्च अदालतको फैसला`, so the held-`court_order` rule would otherwise reject
    the one appeal document worth naming."""
    assert reject_item(item, PRESS_AND_VERDICT) is None, f"{item!r} wrongly dropped"


@pytest.mark.parametrize("item", [
    "पुनरावेदन गरे नगरेको विवरण",
    # `सर्वोच्च` alone appears in APPEAL_ITEM itself, so matching the exemption on
    # the court NAME cancelled the rule it was scoped around: this was accepted and
    # published directly beneath the floor item saying the same thing.
    "सर्वोच्च अदालतमा पुनरावेदन परेको वा नपरेको विवरण",
    "सर्वोच्च अदालतमा पुनरावेदन गरेको अवस्था",
])
def test_a_plain_appeal_comment_is_still_rejected(item):
    """The exemption is for a document FROM another court -- it needs a court name
    AND a court-document noun. Commentary on whether an appeal happened is not a
    document, and the floor item already says it honestly."""
    assert reject_item(item, PRESS_AND_VERDICT) == "restates the appeal item"


def test_an_unlabelled_material_type_does_not_leak_english_into_the_prompt():
    """`material_type` is free-form, not a choices field. An unlabelled type used
    to render its snake_case English name into a Nepali prompt -- the exact
    failure `held_summary` exists to prevent."""
    summary = held_summary(_case(types=("audit_report", "court_order")))
    assert "audit_report" not in summary
    assert "अन्य कागजात" in summary


def test_the_same_claim_is_accepted_when_that_document_is_not_bound():
    """The rule is a DIFF against our bindings, not a keyword ban. A press-only
    case genuinely lacks the verdict copy."""
    press_only = _case(types=("ciaa_press_release",))
    assert reject_item("विशेष अदालतको फैसलाको प्रतिलिपि", press_only) is None


@pytest.mark.parametrize("filler", [
    "अन्य आवश्यक स्रोतहरू।",
    "थप आधार र प्रमाण पुष्टि गर्ने प्रमाणिक स्रोत",
    "अन्य प्रमाण कागजात आदि",
])
def test_filler_is_rejected(filler):
    """These appear in 15 and 17 published cases respectively and name nothing a
    reader can go and look for."""
    reason = reject_item(filler, PRESS_AND_VERDICT)
    assert reason == "filler, not a specific document"


@pytest.mark.parametrize("item", [
    "आदित्य नारायण श्रेष्ठको बैंक खाता विवरण",
    "आदिवासी जनजाति उत्थान प्रतिष्ठानको लेखापरीक्षण प्रतिवेदन",
])
def test_a_proper_noun_starting_with_the_filler_word_is_not_filler(item):
    """`आदि` is filler as a WHOLE word and a common opening for Nepali proper
    nouns. Matched as a substring it rejected a named defendant's bank records and
    a named audit report -- the exact specificity this output exists for."""
    assert reject_item(item, PRESS_AND_VERDICT) is None, f"{item!r} wrongly dropped"


@pytest.mark.parametrize("item", [
    "ठेक्का सम्झौता, कार्य सम्पन्न प्रतिवेदन आदि",
    "बैंक विवरण, कर विवरण आदि।",
])
def test_filler_used_as_a_whole_word_is_still_rejected(item):
    assert reject_item(item, PRESS_AND_VERDICT) == "filler, not a specific document"


def test_restating_a_floor_item_is_rejected():
    """A COPY of a floor document restates it. Commentary about the appeal restates
    the appeal item. But the appeal PETITION is a document in its own right --
    naming it is the most useful thing this output can do when an appeal was lodged,
    and the appeal rule used to drop it. See COURT_DOC_WORDS."""
    assert reject_item("अभियोगपत्रको पूर्णपाठ", PRESS_AND_VERDICT) == (
        "restates the charge-sheet item")
    assert reject_item("पुनरावेदन भएको वा नभएको ब्यहोरा", PRESS_AND_VERDICT) == (
        "restates the appeal item")
    assert reject_item("पुनरावेदनपत्रको मूलपाठ", PRESS_AND_VERDICT) is None


def test_the_floor_check_is_conditional_not_a_blanket_ban():
    """With a charge sheet BOUND, `CHARGE_SHEET_ITEM` is absent from the floor, so
    a claim about the charge sheet's own contents is legitimate."""
    with_charge = _case(types=("charge_sheet", "court_order"))
    assert reject_item("अभियोगपत्रमा उल्लेखित संलग्न अनुसूची", with_charge) is None


def test_markup_is_rejected():
    """0 of 61 published values carry a tag, and the field renders through an HTML
    component, so a tag would actually be interpreted."""
    assert reject_item("<b>सम्झौता</b>", PRESS_AND_VERDICT) == "contains markup"


def test_a_sentence_is_rejected_as_too_long():
    long_item = "क" * (MAX_ITEM_CHARS + 1)
    reason = reject_item(long_item, PRESS_AND_VERDICT)
    assert reason and "not a document name" in reason


def test_an_empty_item_is_rejected():
    assert reject_item("", PRESS_AND_VERDICT) == "empty"
    assert reject_item("   ", PRESS_AND_VERDICT) == "empty"
    assert reject_item(None, PRESS_AND_VERDICT) == "empty"


def test_a_duplicate_within_one_round_is_rejected():
    reason = reject_item("साक्षीहरूको वकपत्र", PRESS_AND_VERDICT,
                         accepted=["साक्षीहरूको वकपत्र"])
    assert reason == "duplicate of an item already listed"


# --------------------------------------------------------------------------
# accept_items
# --------------------------------------------------------------------------


def test_accept_items_returns_keepers_and_reasons():
    proposed = ["३ चरणका ठेक्का सम्झौताका प्रतिलिपि", "प्रेस विज्ञप्ति",
                "अन्य आवश्यक स्रोतहरू।", "साक्षीहरूको वकपत्र"]
    kept, rejected = accept_items(proposed, PRESS_AND_VERDICT)
    assert kept == ["३ चरणका ठेक्का सम्झौताका प्रतिलिपि", "साक्षीहरूको वकपत्र"]
    assert [r for _, r in rejected] == [
        "claims a ciaa_press_release is missing, but one is bound as evidence",
        "filler, not a specific document",
    ]


def test_accept_items_enforces_the_cap():
    proposed = [f"सम्झौता क्रमांक {n}" for n in range(MAX_LLM_ITEMS + 3)]
    kept, rejected = accept_items(proposed, PRESS_AND_VERDICT)
    assert len(kept) == MAX_LLM_ITEMS
    assert all("over the" in r for _, r in rejected)


def test_accept_items_skips_non_strings():
    kept, rejected = accept_items(["साक्षीहरूको वकपत्र", 42, None], PRESS_AND_VERDICT)
    assert kept == ["साक्षीहरूको वकपत्र"]
    assert [r for _, r in rejected] == ["not a string", "not a string"]


def test_accept_items_of_nothing():
    assert accept_items(None, PRESS_AND_VERDICT) == ([], [])
    assert accept_items([], PRESS_AND_VERDICT) == ([], [])


# --------------------------------------------------------------------------
# build
# --------------------------------------------------------------------------


def test_build_floor_only():
    assert build(PRESS_AND_VERDICT) == render([CHARGE_SHEET_ITEM, APPEAL_ITEM])


def test_build_appends_accepted_items_and_reenumerates():
    value = build(PRESS_AND_VERDICT, ["३ चरणका ठेक्का सम्झौताका प्रतिलिपि"])
    assert value.startswith("क) ")
    assert "ग) ३ चरणका ठेक्का सम्झौताका प्रतिलिपि" in value
    assert "१." not in value


def test_build_returns_none_without_a_verdict():
    press_only = _case(types=("ciaa_press_release",))
    assert build(press_only) is None
    assert build(press_only, ["साक्षीहरूको वकपत्र"]) is None, (
        "no verdict means no missing_details, even with accepted items")


def test_build_returns_none_when_there_is_nothing_to_say():
    """Both floor items satisfied and nothing found. An empty section is the right
    rendering -- `MissingDetailsSection` returns null on a falsy value -- and is
    strictly better than a placeholder."""
    complete = _case(types=("charge_sheet", "court_order"),
                     court_cases=["https://jawafdehi.org/courtcase/supreme/081-cr-2319"])
    assert build(complete) is None


def test_the_item_count_is_the_binding_limit_not_the_char_cap():
    """`MAX_CHARS` is a sanity guard, not a policy. A full 2-floor +
    `MAX_LLM_ITEMS` value at the longest permitted item length must still fit, so
    the ITEM COUNT is what limits the output -- a limit on how many findings, not
    on how precise they may be. At 299 and again at 450 the cap cut real findings
    on real cases, always the last and most specific one."""
    worst = ["स" * MAX_ITEM_CHARS for _ in range(MAX_LLM_ITEMS)]
    value = build(PRESS_AND_VERDICT, worst)
    assert len(value.splitlines()) == 2 + MAX_LLM_ITEMS, "the guard is still binding"
    assert len(value) <= MAX_CHARS


def test_the_char_cap_still_drops_whole_items_when_it_does_fire():
    """Trailing items are DROPPED whole, never truncated mid-phrase -- a
    half-written document name is worse than an absent one. `build` takes items
    on trust, so this passes more than `accept_items` would ever hand it."""
    value = build(PRESS_AND_VERDICT, ["स" * MAX_ITEM_CHARS for _ in range(12)])
    assert len(value) <= MAX_CHARS
    assert "…" not in value
    for line in value.splitlines():
        assert line.strip()


@pytest.mark.parametrize("slug,found", [
    # Both real dry runs, 2026-08-08. At 299 the first lost two items and at 450
    # the second lost one -- in both cases the dropped item was the most specific
    # in the set (dispatch numbers, account numbers), because specificity is long.
    ("case-078-cr-0111", [
        "निरोज मैनाली (२०८०।०१।२०), पुष्प पिया, रबिन्द्र महर्जन (२०७९।०५।०८) समेतका बकपत्र",
        "प्रतिवादी भिमकान्त भण्डारीले अदालतमा गरेको बयानको ब्याहोरा",
        "विध म्यानेजमेन्टको आ.व. ०७८।०७९ को लेखापरीक्षण प्रतिवेदन र कर विवरण",
        "मालपोत कार्यालय कलंकी (च.नं.३६८१) र ललितपुर (च.नं.११६४३) का जग्गा अभिलेख",
    ]),
    ("case-079-cr-0047", [
        "सि.डि.ई. सुदिप आचार्य समेतको टोलीको घर मूल्याङ्कन प्रतिवेदन (च.नं. १०७९८, मिति २०७९।०५।२०)",
        "आयोगको कृषि विज्ञको प्रतिवेदन (२०७० देखि २०७८ सम्मको धान, गहुँ, मकै गणना)",
        "प्रतिवादीहरूले अदालत तथा अनुसन्धान अधिकारीसमक्ष गरेको बयानको ब्यहोरा",
        "राष्ट्रिय वाणिज्य बैंकको च.नं. २२२ (२०७८।०५।०२) र च.नं. २०४५ (२०७९।०२।१३) का बैंक विवरण",
    ]),
])
def test_every_real_finding_survives(slug, found):
    value = build(PRESS_AND_VERDICT, found)
    assert len(value.splitlines()) == 6, f"{slug}: a real finding was dropped to fit"
    for item in found:
        assert item in value, f"{slug}: lost {item[:40]!r}"


def test_build_never_emits_html():
    value = build(PRESS_AND_VERDICT, ["साक्षीहरूको वकपत्र"])
    assert "<" not in value


def test_the_floor_items_are_verbatim_from_published_cases():
    """Copied from `case-080-cr-0196-baikuntha-aryal` / `case-081-cr-0048` and
    `case-081-cr-0060` / `bara-hulak-081-CR-0091` / `case-081-cr-0046`. Noun
    phrases, not sentences, so they sit in one list with the model's items.
    """
    assert CHARGE_SHEET_ITEM == "अख्तियार दुरुपयोग अनुसन्धान आयोगले दायर गरेको अभियोगपत्र"
    assert APPEAL_ITEM == (
        "हदम्याद भित्र वादी वा प्रतिवादीले सर्वोच्च अदालतमा पुनरावेदन गरे नगरेको ब्याहोरा")
    for item in (CHARGE_SHEET_ITEM, APPEAL_ITEM):
        assert not item.endswith("।"), "noun phrase, not a sentence"
        assert len(item) <= MAX_ITEM_CHARS


def test_the_charge_sheet_rule_matches_the_head_not_a_substring():
    """The same reason the held-document rule does. A substring test dropped
    `अभियोगपत्रमा उल्लेखित संलग्न अनुसूची` -- the module's own canonical keeper -- on
    every case with no charge sheet bound, which is 24 of the first batch's 25."""
    for item in ["अभियोगपत्रमा उल्लेखित संलग्न अनुसूची",
                 "अभियोगपत्रसाथ पेश भएको बैंक विवरण",
                 "आरोपपत्रमा उल्लेखित मालपोत अभिलेख"]:
        assert reject_item(item, PRESS_AND_VERDICT) is None, f"{item!r} wrongly dropped"
    # The charge sheet itself, and any copy of it, still restate the floor item.
    for item in ["अभियोगपत्र", "अख्तियारले दायर गरेको अभियोगपत्रको प्रतिलिपि"]:
        assert reject_item(item, PRESS_AND_VERDICT) == "restates the charge-sheet item"


@pytest.mark.parametrize("pair", [
    ("साक्षीहरूको वकपत्र", "साक्षीहरूको बकपत्र"),
    ("प्रतिवादीको विवरण", "प्रतिवादीको बिवरण"),
])
def test_one_document_under_two_spellings_takes_only_one_slot(pair):
    """व/ब is the commonest Nepali orthographic variant and the model emits both --
    this diff's own tests quote each. Unfolded, one document took two of the four
    slots and printed twice on the page."""
    kept, rejected = accept_items(list(pair), PRESS_AND_VERDICT)
    assert len(kept) == 1
    assert rejected and rejected[0][1] == "duplicate of an item already listed"


@pytest.mark.parametrize("item", ["एक\nदुई", "एक\r\nदुई"])
def test_an_item_containing_a_line_break_is_rejected(item):
    """`render` joins items with newlines, so an embedded one leaves a second,
    UN-enumerated line -- which closes the frontend's custom-marker list and renders
    as a stray paragraph."""
    assert reject_item(item, PRESS_AND_VERDICT) == "contains a line break"


def test_the_enumerator_supply_covers_every_reachable_item_count():
    """`MAX_ITEMS` is 2 floor + `MAX_LLM_ITEMS`. It must not exceed the letters,
    or `render` would index past them -- and `build` renders in order to measure,
    so that crash would pre-empt the trim that would have fixed the list."""
    assert MAX_ITEMS == 2 + MAX_LLM_ITEMS <= len(LETTERS)
    assert len(NUMERALS) == 2, "only render's 2-item branch indexes NUMERALS"
    value = build(PRESS_AND_VERDICT, ["कागजात %d" % i for i in range(MAX_LLM_ITEMS)])
    assert all(line[0] in LETTERS for line in value.splitlines())
