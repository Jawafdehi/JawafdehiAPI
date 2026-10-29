# SPDX-License-Identifier: LicenseRef-Hippocratic-3.0
# tests/casework/test_news_feed.py
from datetime import datetime, timezone
from pathlib import Path

from hypothesis import given
from hypothesis import strategies as st

from casework.news_feed import Article, normalize_url, parse_rss

FIXTURE = Path(__file__).parent / "fixtures" / "news_feed" / "onlinekhabar.xml"


def _articles():
    return parse_rss(FIXTURE.read_bytes(), "onlinekhabar")


def test_parses_real_items_and_skips_the_one_without_a_link():
    articles = _articles()
    # 5 items in the fixture; the link-less one is dropped.
    assert len(articles) == 4
    assert all(isinstance(a, Article) for a in articles)
    assert all(a.source == "onlinekhabar" for a in articles)
    assert all(a.url.startswith("https://www.onlinekhabar.com/") for a in articles)


def test_first_item_fields():
    first = _articles()[0]
    assert first.title == "बाल्टिनमा डुबेरमा १७ महिनाको बालकको मृत्यु"
    assert first.url == ("https://www.onlinekhabar.com/2026/10/2046869/"
                         "17-month-old-boy-dies-after-drowning-in-bucket")
    assert first.published_at == datetime(2026, 10, 29, 13, 52, 6, tzinfo=timezone.utc)


def test_summary_unescapes_double_encoded_entities():
    # The feed ships "[&amp;#8230;]", i.e. an entity escaped twice.
    summary = _articles()[0].summary
    assert summary.endswith("[…]")
    assert "&#8230;" not in summary


def test_unparseable_date_becomes_none_and_tracked_url_is_cleaned():
    last = _articles()[-1]
    assert last.title == "अख्तियारले मुद्दा दायर गर्‍यो"
    assert last.published_at is None
    assert last.url == "https://www.onlinekhabar.com/2026/10/2046850/ciaa-files-case"


def test_malformed_xml_returns_no_articles():
    assert parse_rss(b"<rss><channel><item>", "onlinekhabar") == []


def test_normalize_url_strips_tracking_fragment_and_trailing_slash():
    url = "HTTPS://WWW.Example.com/news/1/?utm_source=rss&id=7&fbclid=x#top"
    assert normalize_url(url) == "https://www.example.com/news/1?id=7"


@given(st.from_regex(r"https://[a-z]{1,10}\.com(/[a-z0-9]{1,8}){0,3}/?"
                     r"(\?(utm_source|id|q)=[a-z0-9]{1,5}(&(utm_medium|p)=[a-z0-9]{1,5})?)?"
                     r"(#[a-z]{1,5})?", fullmatch=True))
def test_normalize_url_is_idempotent(url):
    once = normalize_url(url)
    assert normalize_url(once) == once
    assert "utm_" not in once
    assert "#" not in once
