from casework.ab.snapshot import select_sample_cases, extract_golden


def test_selects_fy_080_081_case_insensitively():
    cases = [
        {"slug": "a", "court_cases": ["https://jawafdehi.org/courtcase/special/080-cr-0158"]},
        {"slug": "b", "court_cases": ["https://jawafdehi.org/courtcase/special/081-CR-0098"]},
        {"slug": "c", "court_cases": ["https://jawafdehi.org/courtcase/supreme/075-wf-0005"]},
        {"slug": "d", "court_cases": []},
    ]
    got = {c["slug"] for c in select_sample_cases(cases)}
    assert got == {"a", "b"}


def test_extract_golden_captures_june_output():
    cases = [
        {"slug": "a", "bigo": 10403941, "tags": ["CIAA"], "timeline": [],
         "key_allegations": ["घूस"], "missing_details": None},
        {"slug": "b", "bigo": None, "tags": [], "timeline": [],
         "key_allegations": [], "missing_details": None},
    ]
    golden = extract_golden(cases)
    assert golden["a"]["bigo"] == 10403941
    assert golden["a"]["key_allegations"] == ["घूस"]
    assert golden["b"]["bigo"] is None
