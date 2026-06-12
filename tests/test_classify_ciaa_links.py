"""Unit tests for classify_ciaa_links — the CIAA source-link role classifier.

Encodes the stored convention: a CIAA press-release source has exactly one RAW
(the document file, preferring .pdf), the ciaa.gov.np landing page is
SOURCE_PAGE, and any other uploaded file (e.g. a .doc export) is ALTERNATE.
This is what stops the ingester from re-creating multi-RAW sources.
"""

from cases.services.source_files import classify_ciaa_links

PAGE = "https://ciaa.gov.np/pressrelease/2579"
PDF = "https://ngm-store.jawafdehi.org/uploads/ciaa/press-releases/files/2579-x.pdf"
DOC = "https://ngm-store.jawafdehi.org/uploads/ciaa/press-releases/files/2579-x.doc"


def _roles(result):
    return {u["link"]: u["role"] for u in result}


def test_page_pdf_doc_gets_one_raw():
    out = classify_ciaa_links([PAGE, DOC, PDF])
    roles = _roles(out)
    assert roles[PDF] == "RAW"
    assert roles[PAGE] == "SOURCE_PAGE"
    assert roles[DOC] == "ALTERNATE"
    assert sum(1 for u in out if u["role"] == "RAW") == 1


def test_order_preserved():
    out = classify_ciaa_links([PAGE, DOC, PDF])
    assert [u["link"] for u in out] == [PAGE, DOC, PDF]


def test_page_only_keeps_page_as_raw():
    # A draft case created before files are mapped: the page is the only link,
    # so it must stay RAW (else the source has zero RAW and fails the gate).
    out = classify_ciaa_links([PAGE])
    assert _roles(out) == {PAGE: "RAW"}


def test_no_pdf_first_file_is_raw():
    # Only a .doc uploaded (no pdf): the doc becomes the canonical RAW.
    out = classify_ciaa_links([PAGE, DOC])
    roles = _roles(out)
    assert roles[DOC] == "RAW"
    assert roles[PAGE] == "SOURCE_PAGE"
    assert sum(1 for u in out if u["role"] == "RAW") == 1


def test_two_files_pdf_wins():
    other = "https://s3.jawafdehi.org/case_uploads/abc.pdf"
    out = classify_ciaa_links([PAGE, DOC, PDF, other])
    roles = _roles(out)
    # exactly one RAW, and it's a pdf (the first pdf encountered)
    raws = [k for k, v in roles.items() if v == "RAW"]
    assert raws == [PDF]
    assert roles[DOC] == "ALTERNATE"
    assert roles[other] == "ALTERNATE"


def test_empty_and_blank():
    assert classify_ciaa_links([]) == []
    assert classify_ciaa_links([None, "", "  "]) == []
