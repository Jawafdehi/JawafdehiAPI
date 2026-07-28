"""ciaa_press_releases port: pure page-parse tests (no network, no DB).

Verifies the BeautifulSoup translation of the retired spider's xpath: the title
(``h4 > strong``), the attachment links (badges + image attachments under
``/uploads/``, deduped, the page logo excluded), the body text (download/social
chrome stripped), and the Bikram Sambat publication-date guesser.
"""

from materials.sourcing.ciaa.parse import guess_publication_date, parse_press_release

_PDF_URL = "https://ciaa.gov.np/uploads//pressRelease/abc.pdf"
_DOC_URL = "https://ciaa.gov.np/uploads//pressRelease/def.docx"
_IMG_URL = "https://ciaa.gov.np/uploads//pressRelease/img.jpg"

# A press-release page: title in h4>strong, a dated body, two download badges and
# one image attachment, plus a social embed and the site logo that must NOT be
# picked up as attachments/body.
_PAGE_HTML = f"""
<html><body>
  <div class="col-sm-4"><a href="https://ciaa.gov.np/uploads/images/logo.jpg">logo</a></div>
  <div class="col-sm-8">
    <h4><strong>भ्रष्टाचार मुद्दा दायर सम्बन्धी प्रेस विज्ञप्ति</strong></h4>
    <p>मिति २०८१।०९।२८ गते आयोगले मुद्दा दायर गरेको व्यहोरा जानकारी गराइन्छ ।</p>
    <a class="badge badge-info" href="{_PDF_URL}">डाउनलोड</a>
    <a class="badge badge-danger" href="{_DOC_URL}">Download</a>
    <a class="mailbox-attachment-name" href="{_IMG_URL}">image</a>
    <a class="badge badge-info" href="{_PDF_URL}">डाउनलोड</a>
    <div class="fb-share-button">Tweet</div>
  </div>
</body></html>
"""

_URL = "https://ciaa.gov.np/pressrelease/3540"


def test_parse_extracts_title():
    rec = parse_press_release(_PAGE_HTML, press_id=3540, source_url=_URL)
    assert rec.title == "भ्रष्टाचार मुद्दा दायर सम्बन्धी प्रेस विज्ञप्ति"
    assert rec.press_id == 3540
    assert rec.source_url == _URL


def test_parse_collects_attachment_urls_deduped_logo_excluded():
    rec = parse_press_release(_PAGE_HTML, press_id=3540, source_url=_URL)
    # pdf + docx + image, absolute + order-preserving + deduped (pdf listed twice);
    # the header logo is outside col-sm-8 AND not an attachment class → excluded.
    assert rec.file_urls == [_PDF_URL, _DOC_URL, _IMG_URL]


def test_parse_body_text_strips_chrome():
    rec = parse_press_release(_PAGE_HTML, press_id=3540, source_url=_URL)
    assert "मुद्दा दायर गरेको" in rec.full_text
    assert "Download" not in rec.full_text
    assert "डाउनलोड" not in rec.full_text
    assert "Tweet" not in rec.full_text


def test_parse_guesses_bs_date():
    rec = parse_press_release(_PAGE_HTML, press_id=3540, source_url=_URL)
    assert rec.publication_date_bs == "2081-09-28"


def test_parse_thin_page_yields_empty_record_not_error():
    rec = parse_press_release("<html><body>nope</body></html>", press_id=9, source_url=_URL)
    assert rec.press_id == 9
    assert rec.title == ""
    assert rec.file_urls == []


class TestGuessPublicationDate:
    def test_labelled_press_release_ascii(self):
        assert guess_publication_date("Press Release- 2072-08-15") == "2072-08-15"

    def test_miti_devanagari(self):
        assert guess_publication_date("मिति २०७९।१२।१२ गते ।") == "2079-12-12"

    def test_press_bigyapti_devanagari(self):
        assert guess_publication_date("प्रेस विज्ञप्ति २०७२/०८/१६") == "2072-08-16"

    def test_bare_leading_date_zero_pads(self):
        assert guess_publication_date("२०८१/९/२८ ...") == "2081-09-28"

    def test_no_date_returns_empty(self):
        assert guess_publication_date("no date here") == ""
        assert guess_publication_date("") == ""
