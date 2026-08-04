"""Pure tests for the PPMO publication extract + shape.

No DB, no network. Asserts the PDF/title extraction off a PPMO content page and
that the shaped ``OFFICIAL_REPORT`` doc passes the real ``validate_material_jsonld``
(so a live POST cannot 400 on shape).
"""

from __future__ import annotations

from materials.jsonld import MaterialType, validate_material_jsonld
from materials.sourcing.ppmo.crawl import discover_content_ids
from materials.sourcing.ppmo.shaper import (
    PPMO_AUTHORITY,
    PPMO_SOURCE,
    extract_pdf_urls,
    extract_title,
    ppmo_report_to_jsonld,
)

_PDF = "https://giwmscdnone.gov.np/media/app/public/247/posts/1693200980_94.pdf"

# Structurally faithful to a real PPMO content page: og:title carries the doc name
# with the site suffix appended, and the attached PDF sits on the GIWMS CDN.
_PAGE = f"""
<html><head>
  <title>सार्बजनिक खरिद पत्रिका २०७४ | Public Procurement Monitoring Office</title>
  <meta property="og:title" content="सार्बजनिक खरिद पत्रिका २०७४ | Public Procurement Monitoring Office" />
</head><body>
  <img src="https://giwmscdnone.gov.np/static/assets/image/Emblem_of_Nepal.png"/>
  <a href="{_PDF}">डाउनलोड</a>
  <a href="/content/13243/public-purchase-magazine--2082/">bulletin</a>
  <a href="/content/13253/annual-report-2082/">report</a>
</body></html>
"""


def test_extract_pdf_urls_finds_cdn_pdf():
    assert extract_pdf_urls(_PAGE) == [_PDF]


def test_extract_pdf_urls_ignores_static_images():
    # The emblem PNG on the same CDN must not be picked up as a document.
    assert all(u.endswith(".pdf") for u in extract_pdf_urls(_PAGE))


def test_extract_pdf_urls_dedups():
    html = f'<a href="{_PDF}">a</a><a href="{_PDF}">b</a>'
    assert extract_pdf_urls(html) == [_PDF]


def test_extract_pdf_urls_empty_when_none():
    assert extract_pdf_urls("<html><body>plain notice</body></html>") == []


def test_extract_title_strips_site_suffix():
    # The " | Public Procurement Monitoring Office" suffix is the site's, not the
    # document's — it must not end up in the material name.
    assert extract_title(_PAGE) == "सार्बजनिक खरिद पत्रिका २०७४"


def test_extract_title_unescapes_html_entities():
    # Raw markup carries &amp;; storing it literally would corrupt the name.
    html = "<html><head><title>e-GP Handsout &amp; Resources</title></head></html>"
    assert extract_title(html) == "e-GP Handsout & Resources"


def test_extract_title_empty_when_absent():
    assert extract_title("<html><body>no title</body></html>") == ""


def test_discover_content_ids():
    assert discover_content_ids(_PAGE) == [13243, 13253]


def test_shaper_is_official_report_with_pdf_attached():
    doc, mtype = ppmo_report_to_jsonld(7343, _PDF, extract_title(_PAGE))
    assert mtype == MaterialType.OFFICIAL_REPORT
    assert doc["@type"] == "Report"
    assert doc["@id"] == f"https://jawafdehi.org/material/{PPMO_SOURCE}/7343"
    assert doc["associatedMedia"][0]["contentUrl"] == _PDF
    assert doc["associatedMedia"][0]["encodingFormat"] == "application/pdf"
    assert doc["sources"] == [
        {"url": "https://www.ppmo.gov.np/content/7343/", "authority": PPMO_AUTHORITY}
    ]


def test_shaper_language_tags_nepali_title_as_ne():
    doc, _ = ppmo_report_to_jsonld(7343, _PDF, "सार्बजनिक खरिद पत्रिका २०७४")
    assert "ne" in doc["name"] and "en" not in doc["name"]


def test_shaper_language_tags_english_title_as_en():
    doc, _ = ppmo_report_to_jsonld(1, _PDF, "Public Procurement Magazine 2082")
    assert doc["name"] == {"en": "Public Procurement Magazine 2082"}


def test_shaper_falls_back_to_generic_name():
    doc, _ = ppmo_report_to_jsonld(99, _PDF, "")
    assert doc["name"]["en"] == "PPMO Publication 99"


def test_shaper_sets_no_text_field():
    # The PDFs are scanned images; text arrives only via the deferred OCR pass. The
    # shaper must NOT invent an empty/placeholder transcript.
    doc, _ = ppmo_report_to_jsonld(7343, _PDF, "x")
    assert "text" not in doc


def test_shaped_doc_passes_material_validation():
    doc, _ = ppmo_report_to_jsonld(7343, _PDF, extract_title(_PAGE))
    validate_material_jsonld(doc, iri=doc["@id"])  # raises on failure


def test_shaper_id_is_stable_for_same_content_id():
    a, _ = ppmo_report_to_jsonld(7343, _PDF, "t")
    b, _ = ppmo_report_to_jsonld("7343", _PDF, "t")
    assert a["@id"] == b["@id"]  # idempotent upsert key
