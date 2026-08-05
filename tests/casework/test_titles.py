from casework.common.titles import (
    TITLE_RULES, parse_title, title_has_headcount, title_is_acceptable,
    validate_title,
)


def test_accepts_title_with_trailing_court_number():
    assert validate_title("घूसखोरी प्रकरण (081-CR-0098)", "081-cr-0098") is None


def test_rejects_title_missing_court_number():
    assert validate_title("घूसखोरी प्रकरण", "081-cr-0098") is not None


def test_rejects_title_with_wrong_court_number():
    # A court number IS present, but it's a different case's number. The
    # mismatch message names the expected number, distinguishing this branch
    # from the (also-failing) trailing-parens check below it.
    result = validate_title("घूसखोरी प्रकरण (082-CR-0011)", "081-cr-0098")
    assert result is not None
    assert "081-cr-0098" in result


def test_rejects_title_where_number_is_not_trailing():
    # The right number is present, but buried mid-sentence rather than at
    # the very end in parentheses — validate_title requires trailing parens.
    result = validate_title(
        "घूसखोरी प्रकरण (081-CR-0098) थप विवरण", "081-cr-0098"
    )
    assert result is not None


def test_validate_title_is_case_insensitive_on_court_number():
    assert validate_title("घूसखोरी प्रकरण (081-cr-0098)", "081-CR-0098") is None


def test_rejects_title_with_no_number_even_without_expected_court_number():
    # With no court_number to match against, validate_title still requires
    # *some* court case number to be present in the title.
    assert validate_title("घूसखोरी प्रकरण", None) is not None


def test_accepts_title_with_any_number_when_court_number_not_specified():
    assert validate_title("घूसखोरी प्रकरण (081-CR-0098)", None) is None


def test_rejects_headcount_in_title():
    assert title_has_headcount("५ जना विरुद्ध घूसखोरी मुद्दा (081-CR-0098)")
    # NOTE: the donor's _HEADCOUNT_RE only matches a *digit* (Devanagari or
    # ASCII) immediately followed by जना/व्यक्ति/प्रतिवादी — it does not catch
    # spelled-out number words like "तीन" (three). "३ प्रतिवादी" (digit) is the
    # donor-faithful positive case; see task-10-report.md for the brief-vs-donor
    # discrepancy this replaced ("तीन प्रतिवादीको मुद्दा" does NOT match donor regex).
    assert title_has_headcount("३ प्रतिवादीलाई मुद्दा (081-CR-0098)")


def test_headcount_matches_vyakti_noun_too():
    # Distinguishes the "व्यक्ति" alternative in _HEADCOUNT_RE from "जना" /
    # "प्रतिवादी" — a mutation dropping this alternative must fail this test.
    assert title_has_headcount("७ व्यक्ति विरुद्ध मुद्दा (081-CR-0098)")


def test_plain_title_has_no_headcount():
    assert not title_has_headcount("सडक निर्माणमा भ्रष्टाचार (081-CR-0098)")


def test_court_number_itself_is_not_mistaken_for_headcount():
    # A bare court-case number like 081-CR-0098 must NOT itself trip the
    # headcount guard (no जना/व्यक्ति/प्रतिवादी trails it).
    assert not title_has_headcount("घूसखोरी प्रकरण (081-CR-0098)")


def test_title_rules_is_shared_non_empty_text():
    assert isinstance(TITLE_RULES, str) and TITLE_RULES.strip()


# --------------------------------------------------------------------------
# title_is_acceptable -- one predicate behind both the skip gate and the write
# gate. If they could disagree, a title good enough to skip could be one the
# writer rejects, and the case would regenerate on every run forever.
# --------------------------------------------------------------------------


def test_title_is_acceptable_on_a_conforming_title():
    assert title_is_acceptable("घूसखोरी प्रकरण (081-CR-0098)", "081-cr-0098")


def test_title_is_not_acceptable_without_the_trailing_number():
    assert not title_is_acceptable("घूसखोरी प्रकरण", "081-cr-0098")


def test_title_is_not_acceptable_with_a_headcount():
    assert not title_is_acceptable("घूसखोरीमा ५ जना (081-CR-0098)", "081-cr-0098")


def test_a_missing_court_number_makes_every_title_unacceptable():
    # The contract IS "ends in that number". With no number there is nothing to
    # end in, so no title can conform -- and the card enricher uses exactly this
    # to skip the case instead of burning a call on a guaranteed rejection.
    assert not title_is_acceptable("घूसखोरी प्रकरण (081-CR-0098)", "")
    assert not title_is_acceptable("घूसखोरी प्रकरण (081-CR-0098)", None)


def test_a_blank_title_is_unacceptable():
    assert not title_is_acceptable("", "081-cr-0098")
    assert not title_is_acceptable(None, "081-cr-0098")


def test_the_importer_template_title_is_unacceptable():
    # The real production stub: it carries the case number but does not END in
    # it in parens. 2,666 DRAFT cases look like this.
    stub = "CIAA Special Court Case 076-CR-0182: बिनोद कुमार भूजेल समेत ५"
    assert not title_is_acceptable(stub, "076-CR-0182")


# --------------------------------------------------------------------------
# parse_title
# --------------------------------------------------------------------------


def test_parse_title_reads_the_json_object():
    assert parse_title('{"title": "घूसखोरी प्रकरण (081-CR-0098)"}') == (
        "घूसखोरी प्रकरण (081-CR-0098)")


def test_parse_title_strips_whitespace():
    assert parse_title('{"title": "  घूसखोरी (081-CR-0098)  "}') == (
        "घूसखोरी (081-CR-0098)")


def test_parse_title_reads_a_fenced_object():
    body = '```json\n{"title": "घूसखोरी प्रकरण (081-CR-0098)"}\n```'
    assert parse_title(body) == "घूसखोरी प्रकरण (081-CR-0098)"


def test_parse_title_scans_past_a_null_title():
    # The card prompt TELLS the model to null an unrequested key, so a
    # `{"title": null}` object is expected traffic, not a malformed reply. It
    # must not be returned as a parse success.
    body = '{"title": null} then {"title": "घूसखोरी (081-CR-0098)"}'
    assert parse_title(body) == "घूसखोरी (081-CR-0098)"


def test_parse_title_returns_none_for_a_null_title_alone():
    assert parse_title('{"title": null, "short_description": "सार"}') is None


def test_parse_title_returns_none_for_a_blank_title():
    assert parse_title('{"title": "   "}') is None


def test_parse_title_accepts_a_bare_single_line_title():
    # A frugal model may emit the headline with no JSON wrapper.
    assert parse_title("घूसखोरी प्रकरण (081-CR-0098)") == "घूसखोरी प्रकरण (081-CR-0098)"


def test_parse_title_accepts_a_fenced_bare_title():
    # Fences are stripped before the bare-line fallback, matching the donor.
    assert parse_title("```\nघूसखोरी प्रकरण (081-CR-0098)\n```") == (
        "घूसखोरी प्रकरण (081-CR-0098)")


def test_parse_title_rejects_prose_without_a_court_number():
    # A confirmation sentence must never be PATCHed as a public title.
    assert parse_title("Sure, I've written the title for you.") is None


def test_parse_title_rejects_a_multi_line_bare_response():
    assert parse_title("घूसखोरी (081-CR-0098)\nthanks!") is None


def test_parse_title_returns_none_for_empty_input():
    assert parse_title("") is None
    assert parse_title(None) is None
