"""Parse a CIAA press-release page (``ciaa.gov.np/pressrelease/<id>``) into a record.

The pure parse half of the ``scrape_ciaa_press_releases`` command. CIAA publishes
press releases (प्रेस विज्ञप्ति) at sequential integer ids; each page carries a
title, a body, and zero or more downloadable attachments (PDF/DOC/images) under
``/uploads/``. A missing id 302-redirects to the site root — that "missing" signal
is an HTTP-status concern the command handles; this module only parses a fetched
200 page and never decides existence.

Ported from the retired ``ciaa_press_releases`` Scrapy spider (archived
``Jawafdehi/ngm``): the xpath selectors are translated to BeautifulSoup (the
parser lib the courts scrapers already use). ``press_id`` is preserved verbatim so
the shaper can mint the SAME ``@id`` the legacy index used
(``ngm:ciaa-press-release:<id>`` → ``/material/ciaa_press_release/<id>``), keeping
a re-ingest idempotent with the materials already synced from the frozen index.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

# The sourcing text helpers live once, in the nkp normalizer (same subsystem);
# reuse them rather than mint a fourth copy of whitespace/numeral normalization.
from materials.sourcing.nkp.normalizer import (
    nepali_to_roman_numerals,
    normalize_whitespace,
)

#: The single content column the whole record lives in (title, body, links). bs4
#: class matching is membership (``col-sm-8`` among the element's classes), which
#: is slightly more lenient than the legacy ``@class="col-sm-8"`` xpath and so
#: strictly more robust to an added utility class.
# Annotated `Any`-valued rather than left to infer `dict[str, str]`: bs4 types
# `find(attrs=...)` as a dict over its own wide "strainable" union, and `dict` is
# INVARIANT in its value type, so `dict[str, str]` is not assignable to it even
# though `str` is a member of that union. `Any` on a handle to an untyped
# third-party shape is the documented house style (see the ANN401 note in
# pyproject.toml, which names BeautifulSoup explicitly).
_CONTAINER_ATTRS: dict[str, Any] = {"class": "col-sm-8"}

#: Link/UI label text that is page chrome, never body text (the download badge and
#: the social buttons), dropped from the extracted full text.
_JUNK_TEXT = frozenset({"Download", "Tweet", "डाउनलोड"})

#: Attachment link classes: ``badge`` (PDF/DOC download badges) and
#: ``mailbox-attachment-name`` (inline image attachments). Only ``/uploads/`` hrefs
#: are attachments (the site logo / nav links are excluded by that path filter).
_ATTACHMENT_CLASSES = ("badge", "mailbox-attachment-name")

#: The command downloads every attachment URL this parser emits, from inside the
#: cluster — so a page that injected an absolute href to another host (cloud
#: metadata, an internal service) would be an SSRF vector. The ``/uploads/``
#: substring alone does NOT bound the host (``http://169.254.169.254/x/uploads/y``
#: contains it), so attachments are hard-restricted to the CIAA host here.
_ALLOWED_ATTACHMENT_HOSTS = frozenset({"ciaa.gov.np", "www.ciaa.gov.np"})

# Publication-date forms seen in the corpus, tried in order against the
# Devanagari→ASCII-digit-normalized text. All yield a Bikram Sambat YYYY-MM-DD.
_DATE_PATTERNS = (
    re.compile(r"Press\s+Release[-\s]*(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", re.IGNORECASE),
    re.compile(r"मिति\s*(\d{4})[।./\-]\s*(\d{1,2})[।./\-]\s*(\d{1,2})"),
    re.compile(r"प्रेस\s*विज्ञप्ति\s*(\d{4})[।./\-]\s*(\d{1,2})[।./\-]\s*(\d{1,2})"),
)
# A bare leading date (``२०८१/०९/२८``) — matched against the STRIPPED text so the
# ``^`` anchor holds even when the body starts with whitespace.
_BARE_DATE_PATTERN = re.compile(r"^(\d{4})[।./\-]\s*(\d{1,2})[।./\-]\s*(\d{1,2})")


@dataclass
class ParsedPressRelease:
    """One CIAA press release, parsed from its (200-response) page."""

    press_id: int
    title: str = ""
    full_text: str = ""
    publication_date_bs: str = ""
    file_urls: list[str] = field(default_factory=list)
    source_url: str = ""


def parse_press_release(html: object, *, press_id: int, source_url: str) -> ParsedPressRelease:
    """Parse a fetched press-release page into a ``ParsedPressRelease``.

    Always returns a record for a 200 page (title/body may be empty for a thin
    page); the caller decides "missing" from the HTTP status, not from an empty
    parse. Attachment links are read BEFORE the body text so stripping the
    download-badge chrome doesn't drop them.
    """
    soup = BeautifulSoup(str(html or ""), "html.parser")
    container = soup.find("div", attrs=_CONTAINER_ATTRS) or soup

    title = _extract_title(container)
    file_urls = _extract_file_urls(container, source_url)
    full_text = _extract_full_text(container)
    # Guess against the BODY first: the bare-``^`` date form is anchored to the
    # start of the text, and a press release usually opens with its date — folding
    # the title in front (as the legacy spider did) would defeat that anchor. The
    # labelled forms still match wherever they appear, so fall back to the title.
    publication_date_bs = guess_publication_date(full_text) or guess_publication_date(title)

    return ParsedPressRelease(
        press_id=press_id,
        title=title,
        full_text=full_text,
        publication_date_bs=publication_date_bs,
        file_urls=file_urls,
        source_url=source_url,
    )


def _extract_title(container) -> str:
    """Title text — the ``<strong>`` inside the header ``<h4>``, else the h4 itself."""
    h4 = container.find("h4")
    if h4 is None:
        return ""
    strong = h4.find("strong")
    return normalize_whitespace((strong or h4).get_text())


def _extract_file_urls(container, source_url: str) -> list[str]:
    """Absolute attachment URLs (badges + image attachments), deduped in order.

    Restricted to the CIAA host after resolution: a relative ``/uploads/…`` href
    resolves onto ``source_url`` (kept), while an absolute href to any other host
    is DROPPED so the command never fetches an attacker-supplied internal URL.
    """
    urls: list[str] = []
    for anchor in container.find_all("a", href=True):
        if "/uploads/" not in anchor["href"]:
            continue
        classes = anchor.get("class") or []
        if not any(cls in classes for cls in _ATTACHMENT_CLASSES):
            continue
        absolute = urljoin(source_url, anchor["href"])
        if (urlparse(absolute).hostname or "").lower() in _ALLOWED_ATTACHMENT_HOSTS:
            urls.append(absolute)
    return list(dict.fromkeys(urls))


def _extract_full_text(container) -> str:
    """The press-release BODY text, with the title and download/social chrome removed.

    Decomposes the header ``h4`` (its text is already captured as the title), the
    badges, social embeds, and attachment icons (attachment links were already read
    out), then flattens the remaining text line-by-line, dropping empty and
    pure-chrome lines. Dropping the title keeps the body clean — search doesn't get
    a duplicated title, and a bare date opening the body stays at position 0 for the
    ``^``-anchored date guess. This mutates ``container`` — call it last.
    """
    for junk in container.select(
        'h4, [class*="fb-"], a.badge, .badge, .mailbox-attachment-icon, script, style'
    ):
        junk.decompose()
    lines = (
        normalize_whitespace(line)
        for line in container.get_text(separator="\n").split("\n")
    )
    return "\n".join(line for line in lines if line and line not in _JUNK_TEXT)


def guess_publication_date(text: object) -> str:
    """Best-effort Bikram Sambat publication date (``YYYY-MM-DD``) from the text.

    Ported from the legacy spider: matches the labelled forms ("Press Release-
    2072-08-15", "मिति २०७९।१२।१२", "प्रेस विज्ञप्ति २०७२/०८/१६") and a bare
    leading date, in that order. Returns "" when none match. Zero-pads month/day.
    """
    if not text:
        return ""
    roman = nepali_to_roman_numerals(str(text))
    candidates = [(pattern, roman) for pattern in _DATE_PATTERNS]
    candidates.append((_BARE_DATE_PATTERN, roman.strip()))
    for pattern, target in candidates:
        match = pattern.search(target)
        if match:
            year, month, day = match.groups()
            return f"{year}-{month.zfill(2)}-{day.zfill(2)}"
    return ""
