# tests/casework/test_materials.py
from casework.common.materials import (
    markdown_link, materials_of_type, raw_links, source_text,
)

CASE = {
    "slug": "c1",
    "evidence": [
        {"material_iri": "https://jawafdehi.org/material/ciaa_press_release/1",
         "material": {"material_type": "press_release", "urls": [
             {"link": "https://x/pr.pdf", "role": "RAW"},
             {"link": "https://x/pr.md", "role": "MARKDOWN"}]}},
        {"material_iri": "https://jawafdehi.org/material/court_order/2",
         "material": {"material_type": "court_order", "urls": [
             {"link": "https://x/co.pdf", "role": "RAW"}]}},
    ],
}


def test_markdown_link_found():
    assert markdown_link(CASE["evidence"][0]["material"]) == "https://x/pr.md"


def test_markdown_link_absent_returns_none():
    assert markdown_link(CASE["evidence"][1]["material"]) is None


def test_raw_links_collected():
    assert raw_links(CASE["evidence"][1]["material"]) == ["https://x/co.pdf"]


def test_materials_of_type_filters():
    got = materials_of_type(CASE, ("press_release",))
    assert len(got) == 1
    assert got[0]["material_type"] == "press_release"


def test_source_text_reports_unmet_when_no_markdown(monkeypatch):
    import casework.common.materials as m
    monkeypatch.setattr(m, "fetch_markdown", lambda link: "प्रेस विज्ञप्ति पाठ")
    text, unmet = source_text(CASE, api=None, types=("press_release", "court_order"))
    assert "प्रेस विज्ञप्ति पाठ" in text
    # court_order has RAW but no MARKDOWN -- must be REPORTED, not silently dropped.
    assert any("court_order" in u for u in unmet)


def test_source_text_reports_fetched_but_blank_markdown(monkeypatch):
    # The fixture MUST carry a MARKDOWN role. An earlier version used a
    # RAW-only material, which short-circuits at `if not link` and never
    # reaches the blank-document branch at all -- mutation testing showed
    # that branch had zero coverage while the test appeared to cover it.
    import casework.common.materials as m
    monkeypatch.setattr(m, "fetch_markdown", lambda link: "   ")
    case = {"slug": "c", "evidence": [
        {"material_iri": "i", "material": {"material_type": "court_order",
         "urls": [{"link": "https://x/a.md", "role": "MARKDOWN"}]}}]}
    text, unmet = source_text(case, api=None, types=("court_order",))
    assert text == ""
    assert any("empty" in u for u in unmet)


def test_source_text_reports_fetch_failure(monkeypatch):
    import casework.common.materials as m

    def _raise(link):
        raise OSError("boom")

    monkeypatch.setattr(m, "fetch_markdown", _raise)
    case = {"slug": "c", "evidence": [
        {"material_iri": "i", "material": {"material_type": "court_order",
         "urls": [{"link": "https://x/a.md", "role": "MARKDOWN"}]}}]}
    text, unmet = source_text(case, api=None, types=("court_order",))
    assert text == ""
    assert any("fetch failed" in u for u in unmet)


def test_source_text_reports_unresolved_material():
    # material:null == a LIST-endpoint payload. Must be loudly unmet, never
    # a silent ("", []) that reads as "this case has no evidence".
    case = {"slug": "c", "evidence": [{"material_iri": "i", "material": None}]}
    text, unmet = source_text(case, api=None, types=("court_order",))
    assert text == ""
    assert any("UNRESOLVED" in u for u in unmet)
