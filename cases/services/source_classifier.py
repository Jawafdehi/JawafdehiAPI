"""Deterministic source-type classification for DocumentSource records.

Single source of truth for mapping a source's (title, description, urls) to a
``SourceType``. Used by:

- the ``revamp_source_types`` data migration (re-derive every row), and
- producers that create sources (so labels are right from the start).

Rules are priority-ordered, first match wins, no LLM. Keyword matching covers
both English and Nepali (Devanagari) because the corpus is bilingual.
"""

from __future__ import annotations

import re
import urllib.parse

# Imported lazily inside functions to avoid a hard dependency on Django app
# loading when this module is imported by a migration.


# ── Court case-number patterns (शब्द: मुद्दा नं ०८१-CR-०१२१) ───────────────
# Used only as a *supporting* signal for court documents; a bare case number is
# NOT sufficient (charge sheets cite them too), so it never classifies alone.
_COURT_CASE_NO_RE = re.compile(r"\b0?\d{2,3}-(?:CR|WH|WF|RE|C\d|RB)-\d", re.IGNORECASE)

# ── Domains ───────────────────────────────────────────────────────────────
# Our own storage / archive hosts carry no classification signal: an uploaded
# PDF or a Wayback mirror can back ANY document type, so they are ignored when
# deciding the type (the title/description and the *original* URL decide).
STORAGE_HOSTS = frozenset(
    {
        "s3.jawafdehi.org",
        "ngm-store.jawafdehi.org",
    }
)

NEWS_DOMAINS = frozenset(
    {
        "setopati.com",
        "ekantipur.com",
        "onlinekhabar.com",
        "himalayantimes.com",
        "therisingnepal.org.np",
        "nepalitimes.com",
        "kathmandupost.com",
        "annapurnapost.com",
        "theannapurnaexpress.com",
        "ratopati.com",
        "myrepublica.nagariknetwork.com",
        "nagariknetwork.com",
        "nepalpress.com",
        "gorkhapatraonline.com",
        "sidhakura.com",
        "kanunkhabar.com",
        "nayapatrikadaily.com",
        "nepalviews.com",
        "pardafas.com",
        "thahakhabar.com",
        "shilapatra.com",
        "capitalnepal.com",
        "merolagani.com",
        "swasthyalive.com",
        "donnews.com",
        "beemapost.com",
        "nonstopkhabar.com",
        "nepaltvonline.com",
        "ukeraa.com",
        "prasashan.com",
        "moneymitra.com",
        "sharesansar.com",
        "janaaastha.com",
        "bbc.com",
        "bbc.co.uk",
    }
)

SOCIAL_DOMAINS = frozenset(
    {
        "facebook.com",
        "fb.com",
        "x.com",
        "twitter.com",
        "instagram.com",
        "youtube.com",
        "youtu.be",
        "tiktok.com",
    }
)

# ── Keyword tuples (English + Nepali) ─────────────────────────────────────
ABHIYOG_PATRA_KEYWORDS = (
    "abhiyog",
    "charge sheet",
    "charge-sheet",
    "chargesheet",
    "indictment",
    "अभियोग पत्र",
    "अभियोगपत्र",
    "अभियाेग पत्र",  # alt encoding seen in corpus (ाे)
    "अभियाेगपत्र",
    "आरोपपत्र",
    "आरोप पत्र",
)

PRESS_RELEASE_KEYWORDS = (
    "press release",
    "press statement",
    "प्रेस विज्ञप्ति",
    "प्रेश विज्ञप्ती",
    "प्रेस विज्ञप्ती",
    "प्रेश विज्ञप्ति",
    "प्रेस वक्तव्य",
)

COURT_ORDER_KEYWORDS = (
    "court order",
    "verdict",
    "judgment",
    "judgement",
    "ruling",
    "फैसला",
    "आदेश",
    "नेकाप",  # Nepal Kanoon Patrika (law reports)
    "सर्वोच्च अदालत",  # Supreme Court
)

COURT_FILING_KEYWORDS = (
    "writ",
    "appeal",
    "petition",
    "पुनरावेदन",
    "रिट",
    "निवेदन",
    "मुद्दा दर्ता",
)

AUDIT_KEYWORDS = (
    "audit report",
    "audit",
    "महालेखा",
    "लेखापरीक्षण",
    "लेखापरिक्षण",
    "वार्षिक प्रतिवेदन",  # annual (audit) report
)

LAW_OR_BILL_KEYWORDS = (
    "law commission",
    "ordinance",
    "regulation",
    "विधेयक",
    "अध्यादेश",
    "नियमावली",
    "नेपाल राजपत्र",  # Nepal Gazette
    "कानून आयोग",
    "ऐन",  # Act
    "एेन",  # alt Devanagari encoding of ऐन (ए + े) seen in corpus
    "act",
    "bill",
)

# Fallback when no content/domain rule fires: trust the prior human label
# rather than dumping everything into MISC. Maps legacy SourceType values to
# the closest new type; ambiguous legacy buckets (OFFICIAL_GOVERNMENT) stay
# MISC because they conflated several document kinds.
_LEGACY_FALLBACK = {
    "MEDIA_NEWS": "NEWS",
    "LEGAL_COURT_ORDER": "COURT_ORDER",
    "SOCIAL_MEDIA": "SOCIAL_MEDIA",
    "LEGISLATIVE_DOC": "LAW_OR_BILL",
}


def _hostname(url: str) -> str:
    """Return the lowercased hostname of *url* (best-effort, no scheme ok)."""
    try:
        parsed = urllib.parse.urlparse(url)
        host = parsed.hostname or parsed.path.split("/")[0].split(":")[0]
        host = (host or "").lower()
        return host[4:] if host.startswith("www.") else host
    except Exception:  # noqa: BLE001
        return ""


def _domain_matches(host: str, domains: frozenset[str]) -> bool:
    """True if *host* equals or is a subdomain of any domain in *domains*."""
    return any(host == d or host.endswith(f".{d}") for d in domains)


def _keyword_in(corpus: str, keyword: str) -> bool:
    """Whether *keyword* occurs in (already-lowercased) *corpus*.

    ASCII keywords use a word-boundary match so short tokens don't match inside
    unrelated words ("act" in "contact", "writ" in "written", "audit" in
    "auditor") and so punctuation still counts as a boundary ("Cooperatives
    Act," matches "act"). Devanagari keywords fall back to substring matching:
    Python's ``\\b`` is unreliable around the combining matras Devanagari uses.
    """
    if keyword.isascii():
        return (
            re.search(rf"(?<![a-z0-9]){re.escape(keyword)}(?![a-z0-9])", corpus)
            is not None
        )
    return keyword in corpus


def _any_keyword(corpus: str, *keyword_groups: tuple[str, ...]) -> bool:
    return any(_keyword_in(corpus, kw) for group in keyword_groups for kw in group)


_LAW_DOMAINS = frozenset({"lawcommission.gov.np", "parliament.gov.np"})


def _match_keywords(corpus: str, clean_urls: list[str]) -> str | None:
    """Priority-ordered keyword rules over a text *corpus*. None if no match.

    The document-naming keywords are intentionally checked most-specific first
    (a charge sheet that also cites a court case number is a charge sheet).
    """
    from cases.models import SourceType

    # Rule 1: AG charge sheet (अभियोग पत्र) — most specific document.
    if _any_keyword(corpus, ABHIYOG_PATRA_KEYWORDS):
        return SourceType.AG_ABHIYOG_PATRA
    # Rule 2: CIAA press release (प्रेस विज्ञप्ति).
    if _any_keyword(corpus, PRESS_RELEASE_KEYWORDS) or any(
        "ciaa.gov.np/pressrelease" in u for u in clean_urls
    ):
        return SourceType.CIAA_PRESS_RELEASE
    # Rule 3: Court order / verdict (फैसला / आदेश).
    if _any_keyword(corpus, COURT_ORDER_KEYWORDS):
        return SourceType.COURT_ORDER
    # Rule 4: Other court filing (पुनरावेदन / रिट / appeal).
    if _any_keyword(corpus, COURT_FILING_KEYWORDS) or _COURT_CASE_NO_RE.search(corpus):
        return SourceType.COURT_FILING_OTHER
    # Rule 5: OAG audit report (महालेखा प्रतिवेदन).
    if _any_keyword(corpus, AUDIT_KEYWORDS):
        return SourceType.OAG_AUDIT_REPORT
    # Rule 6: Law / act / bill (ऐन / विधेयक).
    if _any_keyword(corpus, LAW_OR_BILL_KEYWORDS):
        return SourceType.LAW_OR_BILL
    return None


def classify_source_type(
    title: str,
    description: str,
    urls: list[str],
    prior_type: str | None = None,
) -> str:
    """Classify a source into a ``SourceType`` value (always returns a value).

    Resolution order:
      1. keyword rules on the **title** (the title names the document),
      2. keyword rules on title + description,
      3. domain rules (law/news/social) on the original URLs,
      4. the prior human label via ``_LEGACY_FALLBACK``,
      5. ``MISC``.

    Args:
        title: source title (any language).
        description: source description (any language).
        urls: list of original link strings. Our storage/archive hosts
            (S3, NGM, web.archive.org) carry no signal and are ignored by the
            domain rules.
        prior_type: existing ``source_type`` value, if any. Used only as a
            last-resort hint before falling back to MISC, so a re-classification
            never *loses* information a human previously recorded.

    Returns:
        A ``SourceType`` value string.
    """
    from cases.models import SourceType

    title = (title or "").strip()
    description = (description or "").strip()

    clean_urls = [u.strip() for u in (urls or []) if isinstance(u, str) and u.strip()]
    signal_hosts = [
        h
        for h in (_hostname(u) for u in clean_urls)
        if h and not _domain_matches(h, STORAGE_HOSTS) and "archive.org" not in h
    ]

    # 1: publisher-domain rules. A known news/social host identifies the source
    # as *coverage* regardless of what it covers — primary documents (orders,
    # charge sheets, press releases) live on government hosts or our S3 bucket,
    # never on a news/social domain. So publisher identity beats soft keywords
    # like "ruling" or "verdict" that appear in headlines about a document.
    if any(_domain_matches(h, NEWS_DOMAINS) for h in signal_hosts):
        return SourceType.NEWS
    if any(_domain_matches(h, SOCIAL_DOMAINS) for h in signal_hosts):
        return SourceType.SOCIAL_MEDIA

    # 2 & 3: keyword rules — title first (it names the document), then full text.
    hit = _match_keywords(title.lower(), clean_urls)
    if hit is None:
        hit = _match_keywords(f"{title} {description}".lower(), clean_urls)
    if hit is not None:
        return hit

    # 4: legislation by issuing-body domain (no document keyword present).
    if any(_domain_matches(h, _LAW_DOMAINS) for h in signal_hosts):
        return SourceType.LAW_OR_BILL

    # 5: trust the prior human label rather than dropping to MISC.
    if prior_type and prior_type in _LEGACY_FALLBACK:
        return _LEGACY_FALLBACK[prior_type]

    # 6: fallback.
    return SourceType.MISC
