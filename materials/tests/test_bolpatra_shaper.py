"""Pure tests for the bolpatra tender parse + shape.

No DB, no network: parse a recorded e-GP detail HTML fragment, shape it, and assert
the Material JSON-LD (@type/additionalType/fields/date-conversion) — and that it
passes the real ``validate_material_jsonld`` so a live POST cannot 400 on shape.
"""

from __future__ import annotations

from jawafdehi_shared.entities.ids import build_material_iri
from materials.jsonld import MaterialType, validate_material_jsonld
from materials.sourcing.bolpatra.parse import (
    ParsedTender,
    extract_tender_ids,
    parse_tender_detail,
)
from materials.sourcing.bolpatra.shaper import (
    BOLPATRA_SOURCE,
    _ad_iso,
    tender_to_jsonld,
)

# A trimmed but structurally faithful e-GP detail page (label/value <td> pairs),
# modeled on the live tender 321065.
_DETAIL_HTML = """
<html><body><table>
<tr><td>Public Entity Name</td><td>Miklajung Rural Municipality, Morang</td></tr>
<tr><td>Procurement Category</td><td>Works</td></tr>
<tr><td>Procurement Method</td><td>NCB</td></tr>
<tr><td>IFB/RFP/EOI/PQ No</td><td>02/MRMM/WORKS/NCB/083/084</td></tr>
<tr><td>Project Name</td><td>Hattimara Dekhi Ghising Chowk Samma Sadak Kalopatre</td></tr>
<tr><td>Current Status</td><td>Bid Published</td></tr>
<tr><td>Source of Funds</td><td>Government Budget</td></tr>
<tr><td>Notice Publication Date</td><td>30-07-2026 16:00</td></tr>
<tr><td>Last Date for Bid Submission</td><td>21-08-2026 12:00</td></tr>
<tr><td>Bid Opening Date</td><td>21-08-2026 13:00</td></tr>
</table></body></html>
"""

_RESULTS_HTML = """
<a onclick="getTenderDetails('321065')">x</a>
<a onclick="getTenderDetails('319751')">y</a>
<a onclick="getTenderDetails('321065')">dup</a>
"""


def test_extract_tender_ids_dedups_and_orders():
    assert extract_tender_ids(_RESULTS_HTML) == ["321065", "319751"]


def test_parse_detail_maps_labels():
    t = parse_tender_detail(_DETAIL_HTML, "321065")
    assert t is not None
    assert t.public_entity == "Miklajung Rural Municipality, Morang"
    assert t.procurement_category == "Works"
    assert t.procurement_method == "NCB"
    assert t.notice_number == "02/MRMM/WORKS/NCB/083/084"
    assert t.current_status == "Bid Published"
    assert t.publication_date == "30-07-2026 16:00"
    assert t.submission_deadline == "21-08-2026 12:00"


def test_parse_detail_returns_none_on_empty():
    assert parse_tender_detail("<html><body>error</body></html>", "1") is None


# Regression: e-GP serves the form SHELL (all labels, NO values) for a
# non-existent tenderId. Without the "a label is never a value" guard the parser
# read the FOLLOWING label as the value (public_entity="Procurement Category") and
# cached/published a garbage record instead of recording the id as a gap.
_SHELL_HTML = """
<html><body><table>
<tr><td>Public Entity Name</td><td></td></tr>
<tr><td>Procurement Category</td><td></td></tr>
<tr><td>Procurement Method</td><td></td></tr>
<tr><td>IFB/RFP/EOI/PQ No</td><td></td></tr>
<tr><td>Project Name</td><td></td></tr>
<tr><td>Current Status</td><td></td></tr>
<tr><td>Bid Information</td><td></td></tr>
</table></body></html>
"""


def test_parse_detail_rejects_empty_form_shell():
    assert parse_tender_detail(_SHELL_HTML, "400000") is None


def test_parse_detail_never_reads_a_label_as_a_value():
    # Even a partially-filled page must not absorb the next label as a value.
    html = """<table>
      <tr><td>Public Entity Name</td><td></td></tr>
      <tr><td>Procurement Method</td><td>NCB</td></tr>
    </table>"""
    t = parse_tender_detail(html, "7")
    assert t is not None
    assert t.public_entity is None  # empty on the page — NOT "Procurement Method"
    assert t.procurement_method == "NCB"


def test_ad_iso_converts_egp_date():
    assert _ad_iso("30-07-2026 16:00") == "2026-07-30"
    assert _ad_iso("21-08-2026") == "2026-08-21"


def test_ad_iso_none_on_garbage():
    assert _ad_iso("") is None
    assert _ad_iso("not-a-date") is None
    assert _ad_iso("2026/07/30") is None  # wrong separator/order → skip, don't guess


def test_shaper_type_and_fields():
    t = parse_tender_detail(_DETAIL_HTML, "321065")
    doc, mtype = tender_to_jsonld(t)
    assert mtype == MaterialType.PROCUREMENT_NOTICE
    assert doc["@type"] == "CreativeWork"
    assert doc["additionalType"] == "jawafdehi:ProcurementNotice"
    assert doc["jawafdehi:procuringEntity"] == "Miklajung Rural Municipality, Morang"
    assert doc["jawafdehi:noticeNumber"] == "02/MRMM/WORKS/NCB/083/084"
    assert doc["jawafdehi:currentStatus"] == "Bid Published"
    assert doc["datePublished"] == "2026-07-30"  # AD-converted for search sort
    assert doc["sources"] == [{"url": doc["url"], "authority": "bolpatra.gov.np"}]


def test_shaped_doc_passes_material_validation():
    t = parse_tender_detail(_DETAIL_HTML, "321065")
    doc, _ = tender_to_jsonld(t)
    doc["@id"] = build_material_iri(BOLPATRA_SOURCE, t.tender_id)
    validate_material_jsonld(doc, iri=doc["@id"])  # raises on failure
    assert doc["@id"] == "https://jawafdehi.org/material/bolpatra/321065"


def test_shaper_falls_back_to_notice_number_for_name():
    t = ParsedTender(tender_id="9", notice_number="ABC/1", project_name=None)
    doc, _ = tender_to_jsonld(t)
    assert doc["name"]["en"] == "ABC/1"


def test_shaper_minimal_tender_validates():
    t = ParsedTender(tender_id="42")
    doc, _ = tender_to_jsonld(t)
    doc["@id"] = build_material_iri(BOLPATRA_SOURCE, t.tender_id)
    validate_material_jsonld(doc, iri=doc["@id"])
    assert doc["name"]["en"] == "Tender 42"
