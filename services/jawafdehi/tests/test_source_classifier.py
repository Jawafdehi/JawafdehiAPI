"""Unit tests for cases.services.source_classifier.classify_source_type.

Pure function over (title, description, urls, prior_type) → SourceType value.
No DB needed.
"""

import pytest

from cases.models import SourceType
from cases.services.source_classifier import classify_source_type


@pytest.mark.parametrize(
    "title,description,urls,expected",
    [
        # ── AG charge sheet (अभियोग पत्र) — most specific, wins over court no. ─
        (
            "CIAA अभियोग पत्र — मुद्दा नं ०८१-CR-०१२१",
            "",
            [],
            SourceType.AG_ABHIYOG_PATRA,
        ),
        ("AG Charge Sheet against minister", "", [], SourceType.AG_ABHIYOG_PATRA),
        ("आरोपपत्र दायर", "", [], SourceType.AG_ABHIYOG_PATRA),
        # ── CIAA press release (प्रेस विज्ञप्ति) ──────────────────────────────
        ("अख्तियारको प्रेस विज्ञप्ति", "", [], SourceType.CIAA_PRESS_RELEASE),
        ("CIAA प्रेश विज्ञप्ती नं 3151", "", [], SourceType.CIAA_PRESS_RELEASE),
        (
            "Notice",
            "",
            ["https://ciaa.gov.np/pressrelease/3173"],
            SourceType.CIAA_PRESS_RELEASE,
        ),
        # ── Court order / verdict (फैसला / आदेश / नेकाप) ──────────────────────
        ("विशेष अदालतको फैसला", "", [], SourceType.COURT_ORDER),
        ("Supreme Court order 081-WH-0320", "", [], SourceType.COURT_ORDER),
        ("एनसेल वि ठूला करदाता, नेकाप २०७६", "", [], SourceType.COURT_ORDER),
        # ── Other court filing (पुनरावेदन / रिट / case number alone) ──────────
        ("पुनरावेदनपत्र", "", [], SourceType.COURT_FILING_OTHER),
        ("Writ petition filed", "", [], SourceType.COURT_FILING_OTHER),
        ("मुद्दा नं 081-CR-0127", "", [], SourceType.COURT_FILING_OTHER),
        # ── OAG audit report (महालेखा) ────────────────────────────────────────
        (
            "महालेखा परीक्षकको ५४औँ वार्षिक प्रतिवेदन",
            "",
            [],
            SourceType.OAG_AUDIT_REPORT,
        ),
        ("Audit report 2079", "", [], SourceType.OAG_AUDIT_REPORT),
        # ── Law / act / bill (ऐन / विधेयक) ────────────────────────────────────
        ("भ्रष्टाचार निवारण ऐन २०५९", "", [], SourceType.LAW_OR_BILL),
        ("सार्वजनिक खरिद नियमावली २०६४", "", [], SourceType.LAW_OR_BILL),
        ("एेन (alt encoding)", "", [], SourceType.LAW_OR_BILL),
        # "Act" followed by punctuation/end-of-string still matches (boundary,
        # not space-padded) — regression for the comma after "Act".
        ("Cooperatives Act, 2074 (2017)", "", [], SourceType.LAW_OR_BILL),
        ("The Land Act", "", [], SourceType.LAW_OR_BILL),
        (
            "Land Reform Bill",
            "",
            ["https://lawcommission.gov.np/doc/123"],
            SourceType.LAW_OR_BILL,
        ),
        # ── News (by domain) ──────────────────────────────────────────────────
        (
            "Ncell taxation case ruling",
            "Report in Kathmandu Post",
            ["https://kathmandupost.com/national/2023/06/10/ncell"],
            SourceType.NEWS,
        ),
        (
            "समाचार",
            "",
            ["https://onlinekhabar.com/some-article"],
            SourceType.NEWS,
        ),
        # ── Social media (by domain) ──────────────────────────────────────────
        (
            "Leaked footage",
            "",
            ["https://youtube.com/watch?v=abc"],
            SourceType.SOCIAL_MEDIA,
        ),
        ("FB post", "", ["https://facebook.com/post/1"], SourceType.SOCIAL_MEDIA),
        # ── Fallback → MISC ───────────────────────────────────────────────────
        ("Bidding Document", "", [], SourceType.MISC),
        ("मन्त्री परिषदको निर्णय", "", [], SourceType.MISC),
        ("Generic file", "Some uploaded image", [], SourceType.MISC),
        # ── ASCII keyword word-boundary: no substring false positives ─────────
        # "written"⊃"writ", "auditor"⊃"audit", "contact"⊃"act" must NOT fire.
        ("A Written Statement on the budget", "", [], SourceType.MISC),
        ("Contact list of officials", "", [], SourceType.MISC),
    ],
)
def test_classify(title, description, urls, expected):
    assert classify_source_type(title, description, urls) == expected


def test_ascii_keywords_match_on_word_boundary_not_substring():
    """Short ASCII keywords must not match inside larger words, but must match
    when bounded by punctuation or string edges."""
    # substring-only matches that should NOT fire any rule
    assert classify_source_type("Rewriting the rules", "", []) == SourceType.MISC
    assert classify_source_type("An appealing proposal", "", []) == SourceType.MISC
    # genuine boundary matches that SHOULD fire
    assert (
        classify_source_type("Writ filed at court", "", [])
        == SourceType.COURT_FILING_OTHER
    )


def test_title_wins_over_description():
    """The title names the document; a press release whose *description* happens
    to mention a charge sheet is still a press release."""
    assert (
        classify_source_type(
            "CIAA प्रेस विज्ञप्ति नं 3151",
            "अभियोग पत्र दायर भएको सम्बन्धमा",
            [],
        )
        == SourceType.CIAA_PRESS_RELEASE
    )


def test_storage_and_archive_hosts_carry_no_signal():
    """An S3 upload / Wayback mirror is not itself a classification signal;
    with no keywords and no original URL, it falls back to MISC."""
    assert (
        classify_source_type(
            "Some document",
            "",
            [
                "https://s3.jawafdehi.org/case_uploads/abc.pdf",
                "https://web.archive.org/web/2023/https://x.test/y",
            ],
        )
        == SourceType.MISC
    )


def test_prior_label_used_as_last_resort():
    """A news source whose only URL is now an S3 upload (no domain signal,
    no keywords) keeps NEWS via the legacy-label fallback."""
    assert (
        classify_source_type(
            "Online Khabar",
            "",
            ["https://s3.jawafdehi.org/case_uploads/abc.md"],
            prior_type="MEDIA_NEWS",
        )
        == SourceType.NEWS
    )


def test_prior_label_does_not_override_a_rule_match():
    """An explicit keyword rule beats the prior label."""
    assert (
        classify_source_type(
            "विशेष अदालतको फैसला",
            "",
            [],
            prior_type="MEDIA_NEWS",
        )
        == SourceType.COURT_ORDER
    )


def test_ambiguous_legacy_label_falls_through_to_misc():
    """OFFICIAL_GOVERNMENT was a grab-bag with no clean new equivalent, so a
    row with no other signal lands in MISC rather than guessing."""
    assert (
        classify_source_type(
            "Bid Document",
            "",
            [],
            prior_type="OFFICIAL_GOVERNMENT",
        )
        == SourceType.MISC
    )
