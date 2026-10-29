# SPDX-License-Identifier: LicenseRef-Hippocratic-3.0
"""Read news-outlet RSS feeds into `Article` records. READ-ONLY -- writes nothing.

Stage 1 (Discover) of the news-ingestion pipeline: feed first, case later. This is
the opposite direction to `news_search.py`, which starts from a known case and
searches the web for articles about it. Here we start from an outlet's feed and
emit every item; deciding which items concern corruption (filter), which are the
same story (dedupe) and which case they belong to are later stages.

Nothing here calls an LLM, touches the database or the case API. `--dry-run`
prints what was found and is the only mode.

Feeds are parsed with the stdlib parser (expat never fetches external entities,
and bounds entity expansion), not `lxml.etree`, which ships no type stubs. A
malformed feed yields no articles rather than an exception, so one broken outlet
cannot stop a multi-source run. An item with no link is skipped (it cannot be
cited); an item with an unparseable date is kept with `published_at=None`, so a
later stage can decide what to do with it rather than this one guessing.

    uv run python -m casework.news_feed --source onlinekhabar --dry-run
"""

import argparse
import html
import io
import logging
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests

logger = logging.getLogger(__name__)

SOURCES = {
    "onlinekhabar": "https://www.onlinekhabar.com/feed",
}

USER_AGENT = "Mozilla/5.0 (compatible; JawafdehiAPI/1.0; +https://jawafdehi.org)"
FETCH_TIMEOUT = 30  # seconds

# Query parameters that only identify the referrer, never the article.
_TRACKING_PARAMS = frozenset({"fbclid", "gclid", "mc_cid", "mc_eid"})
_TAG = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class Article:
    url: str
    title: str
    published_at: datetime | None
    source: str
    summary: str


def normalize_url(url: str) -> str:
    """Canonical form of an article URL, so one article has one key.

    Lower-cases scheme and host, drops the fragment, `utm_*` and other tracking
    parameters, and a trailing slash on the path. Idempotent.
    """
    parts = urlsplit(url.strip())
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not k.lower().startswith("utm_") and k.lower() not in _TRACKING_PARAMS]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(),
                       parts.path.rstrip("/"), urlencode(query), ""))


def _clean_text(text: str | None) -> str:
    """Plain text from an RSS text node: tags stripped, entities decoded.

    WordPress double-escapes entities in <description> ("[&amp;#8230;]"), so after
    the XML parser decodes one layer, `html.unescape` decodes the second.
    """
    text = html.unescape(_TAG.sub(" ", text or ""))
    return _SPACE.sub(" ", text).strip()


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value.strip())
    except (TypeError, ValueError):
        return None


def parse_rss(xml: bytes, source: str) -> list[Article]:
    """Every citable item in an RSS 2.0 document, in feed order."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        logger.warning("%s: malformed feed, no articles read (%s)", source, exc)
        return []

    articles = []
    for item in root.iter("item"):
        link = (item.findtext("link") or "").strip()
        if not link:
            logger.info("%s: skipping item with no link", source)
            continue
        articles.append(Article(
            url=normalize_url(link),
            title=_clean_text(item.findtext("title")),
            published_at=_parse_date(item.findtext("pubDate")),
            source=source,
            summary=_clean_text(item.findtext("description")),
        ))
    return articles


def fetch_feed(source: str) -> list[Article]:
    """Download and parse one configured source's feed."""
    response = requests.get(SOURCES[source], headers={"User-Agent": USER_AGENT},
                            timeout=FETCH_TIMEOUT)
    response.raise_for_status()
    return parse_rss(response.content, source)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--source", required=True, choices=sorted(SOURCES))
    p.add_argument("--dry-run", action="store_true", required=True,
                   help="Print the articles found. The only mode for now.")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    # Titles are mostly Nepali; a Windows console defaults to cp1252.
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8")

    articles = fetch_feed(args.source)
    for a in articles:
        date = a.published_at.isoformat() if a.published_at else "no-date"
        print(f"{date}  {a.title}\n    {a.url}")
    print(f"\n{len(articles)} articles from {args.source}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
