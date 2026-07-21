from casework.common.titles import TITLE_RULES, title_has_headcount, validate_title


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
