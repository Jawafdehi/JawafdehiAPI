from casework.common.parse import (
    _strip_fence, balanced_object, is_valid_iso_date, parse_extraction_response,
    parse_object_response,
)


def test_balanced_object_ignores_braces_inside_strings():
    text = 'noise {"evidence_quote": "बिगो रु. {१०} हजार", "bigo": 10000} trailing'
    got = balanced_object(text, text.index("{"))
    assert got == '{"evidence_quote": "बिगो रु. {१०} हजार", "bigo": 10000}'


def test_balanced_object_survives_unbalanced_brace_inside_string():
    # THE discriminating test. The other brace-in-string cases embed a
    # BALANCED pair, so a naive depth counter coincidentally lands on the
    # same closing brace and passes. Only an UNBALANCED brace inside a
    # quoted value distinguishes the string-aware matcher from a naive
    # scan -- verified by mutation testing on 2026-07-18, where a naive
    # implementation passed every other test in this file.
    text = 'noise {"evidence_quote": "only a closing } brace", "bigo": 1} trailing'
    assert balanced_object(text, text.index("{")) == (
        '{"evidence_quote": "only a closing } brace", "bigo": 1}'
    )


def test_balanced_object_handles_nesting():
    text = '{"a": {"b": {"c": 1}}}'
    assert balanced_object(text, 0) == text


def test_balanced_object_handles_escaped_quote_in_string():
    text = '{"q": "say \\"hi\\" {x}"}'
    assert balanced_object(text, 0) == text


def test_balanced_object_returns_none_when_unterminated():
    assert balanced_object('{"a": 1', 0) is None


def test_parse_extraction_response_unwraps_list_key():
    # Returns the LIST under the wrapper key. Verified against every donor
    # call site: allegations, entities, accused_notes, timeline/entries are
    # all list-valued. Nothing calls this expecting a dict.
    body = 'prose\n{"timeline": [{"date": "2024-01-15", "title": "फैसला"}]}\nmore'
    assert parse_extraction_response(body, {"timeline"}) == [
        {"date": "2024-01-15", "title": "फैसला"}
    ]


def test_parse_extraction_response_survives_bracket_inside_quoted_value():
    # Regression: the array scan must be JSON-string aware. A ``]`` inside a
    # quoted value (common in Nepali evidence_quote / description text) must not
    # be counted as the array's close, which would truncate the fragment and
    # drop the whole payload. Bare-array fallback (no wrapper key).
    body = 'prose\n[{"title": "जफत ] गरियो", "date": "2024-01-15"}]\ntrailing'
    assert parse_extraction_response(body, {"timeline"}) == [
        {"title": "जफत ] गरियो", "date": "2024-01-15"}
    ]


def test_parse_extraction_response_survives_bracket_inside_wrapped_value():
    # Same, but inside the ``{"key": [...]}`` wrapper branch (object scan).
    body = '{"timeline": [{"title": "सूची [क] देखियो", "date": "2024-01-15"}]}'
    assert parse_extraction_response(body, {"timeline"}) == [
        {"title": "सूची [क] देखियो", "date": "2024-01-15"}
    ]


def test_parse_extraction_response_returns_none_when_key_absent():
    # NOTE: input deliberately has no top-level `[...]` array anywhere in the
    # text. The brief's original payload here was '{"other": [1, 2]}', but
    # the donor's real fallback path (verified byte-identical, see
    # test_parse_extraction_response_unwraps_list_key) scans for *any*
    # top-level JSON array in the text once no wrapper key matches — so that
    # payload actually returns [1, 2], not None. Using a value with no
    # bracket at all isolates the "key absent" case from that unrelated
    # array-fallback behavior.
    assert parse_extraction_response('{"other": "value"}', {"timeline"}) is None


def test_is_valid_iso_date():
    assert is_valid_iso_date("2024-01-15")
    assert not is_valid_iso_date("2024-13-01")
    assert not is_valid_iso_date("2081-01-15xx")
    assert not is_valid_iso_date(None)


# --------------------------------------------------------------------------
# parse_object_response -- the object-shaped reply (description enricher)
# --------------------------------------------------------------------------


def test_parse_object_response_returns_the_whole_dict():
    body = '{"description": "### क) अभियोगदावीको सार", "title": "शीर्षक"}'
    obj = parse_object_response(body, "description")
    assert obj["description"] == "### क) अभियोगदावीको सार"
    assert obj["title"] == "शीर्षक"


def test_parse_object_response_scans_past_a_leading_unrelated_object():
    """Every `{` is tried, not just `text.find("{")`.

    A reply that opens with an unrelated object -- a preamble, an echoed tool
    argument -- returns None under a first-brace-only parser, which is exactly
    the donor bug this scan exists to avoid.
    """
    body = '{"thinking": "let me see"} \n\n {"description": "असली विवरण"}'
    assert parse_object_response(body, "description") == {"description": "असली विवरण"}


def test_parse_object_response_handles_a_fenced_block():
    body = 'यहाँ छ:\n```json\n{"description": "विवरण"}\n```'
    assert parse_object_response(body, "description") == {"description": "विवरण"}


def test_parse_object_response_survives_a_brace_inside_a_quoted_value():
    body = '{"description": "दफा ३ {क} अनुसार", "n": 1}'
    assert parse_object_response(body, "description")["description"] == "दफा ३ {क} अनुसार"


def test_parse_object_response_returns_none_when_the_key_is_absent():
    assert parse_object_response('{"other": 1}', "description") is None


def test_parse_object_response_returns_none_for_a_bare_array():
    # A list is not an object; the description contract is an object.
    assert parse_object_response('["विवरण"]', "description") is None


def test_parse_object_response_returns_none_for_empty_and_none_input():
    assert parse_object_response("", "description") is None
    assert parse_object_response(None, "description") is None


def test_strip_fence_leaves_text_without_a_closing_fence_alone():
    # A truncated response must not have its whole body eaten by a half fence.
    assert _strip_fence('```json\n{"description": "विवरण"}') == (
        '```json\n{"description": "विवरण"}')
