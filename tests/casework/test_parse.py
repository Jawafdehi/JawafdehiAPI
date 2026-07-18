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


def test_parse_extraction_response_unwraps_key():
    body = 'prose\n{"result": {"bigo": 500}}\nmore prose'
    assert parse_extraction_response(body, ("result",)) == {"bigo": 500}


def test_is_valid_iso_date():
    assert is_valid_iso_date("2024-01-15")
    assert not is_valid_iso_date("2024-13-01")
    assert not is_valid_iso_date("2081-01-15xx")
    assert not is_valid_iso_date(None)
