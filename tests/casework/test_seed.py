import pytest
from casework.ab.seed import material_iris_from_case, snapshot_is_usable


def test_material_iris_from_case_reads_evidence():
    case = {"slug": "x", "evidence": [
        {"material_iri": "https://jawafdehi.org/material/ciaa_press_release/123"},
        {"material_iri": "https://jawafdehi.org/material/court_order/special.081-cr-0098"},
    ]}
    assert material_iris_from_case(case) == [
        "https://jawafdehi.org/material/ciaa_press_release/123",
        "https://jawafdehi.org/material/court_order/special.081-cr-0098",
    ]


def test_material_iris_from_case_handles_no_evidence():
    assert material_iris_from_case({"slug": "x"}) == []


def test_snapshot_is_usable_rejects_empty():
    with pytest.raises(ValueError, match="no cases"):
        snapshot_is_usable([])
