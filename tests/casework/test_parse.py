from casework.common.parse import balanced_object, is_valid_iso_date, parse_extraction_response


def test_balanced_object_ignores_braces_inside_strings():
    text = 'noise {"evidence_quote": "बिगो रु. {१०} हजार", "bigo": 10000} trailing'
    got = balanced_object(text, text.index("{"))
    assert got == '{"evidence_quote": "बिगो रु. {१०} हजार", "bigo": 10000}'


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
