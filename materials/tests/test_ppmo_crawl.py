"""PPMO crawler control-flow tests (fake fetcher + fake material API).

Asserts: discovery seeds the frontier, pages with no PDF are marked terminal (not
retried forever), a POST failure leaves the id un-resolved for retry, resume skips
resolved ids, and --dry-run posts nothing. No net/DB; sleeps patched out.
"""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase

from materials.sourcing.ppmo import crawl as C
from materials.sourcing.ppmo.crawl import PpmoCrawler

CRAWL = "materials.sourcing.ppmo.crawl"

_PDF = "https://giwmscdnone.gov.np/media/app/public/247/posts/1693200980_94.pdf"


def _page(pdf=True, title="Doc"):
    body = f'<a href="{_PDF}">dl</a>' if pdf else "<p>plain notice</p>"
    return f"<html><head><title>{title}</title></head><body>{body}</body></html>"


class _FakeFetcher:
    """content_id → HTML (None = transient). home → HTML for discovery."""

    def __init__(self, pages, home=""):
        self.pages = pages
        self.home = home
        self.calls = []

    def get_content(self, cid):
        self.calls.append(cid)
        return self.pages.get(cid)

    def get_home(self):
        return self.home

    def close(self):
        pass


class _FakeMaterial:
    def __init__(self, fail_ids=()):
        self.posts = []
        self.fail_ids = {str(i) for i in fail_ids}

    def post(self, doc, material_type):
        ident = doc["@id"].rsplit("/", 1)[-1]
        if ident in self.fail_ids:
            raise C.MaterialApiError("503 down")
        self.posts.append((ident, doc, material_type))

    def close(self):
        pass


def _args(cache, **over):
    base = dict(
        cache=str(cache),
        api_base="http://api",
        token="t",
        dry_run=False,
        discover=False,
        ids=None,
        delay=0.0,
        timeout=5,
        basic_auth=None,
    )
    base.update(over)
    return argparse.Namespace(**base)


class PpmoCrawlerTests(SimpleTestCase):
    def setUp(self):
        p = patch(f"{CRAWL}.time.sleep", lambda *a, **k: None)
        p.start()
        self.addCleanup(p.stop)
        # Neutralize the production SEED_IDS so --ids/--discover fully determine the
        # frontier; otherwise every test also queues the 7 real seed ids.
        s = patch(f"{CRAWL}.SEED_IDS", [])
        s.start()
        self.addCleanup(s.stop)

    def _cache(self):
        return Path(tempfile.mkdtemp()) / "ppmo.jsonl"

    def _state(self, cache):
        return json.loads(cache.with_suffix(".jsonl.state.json").read_text())

    def test_publishes_pages_with_pdfs(self):
        cache = self._cache()
        fetch = _FakeFetcher({7343: _page(), 7344: _page()})
        api = _FakeMaterial()
        PpmoCrawler(
            _args(cache, ids="7343,7344"), fetcher=fetch, material_client=api
        ).crawl()
        assert sorted(i for i, _, _ in api.posts) == ["7343", "7344"]

    def test_page_without_pdf_is_terminal_not_retried(self):
        cache = self._cache()
        fetch = _FakeFetcher({7343: _page(pdf=False)})
        api = _FakeMaterial()
        PpmoCrawler(
            _args(cache, ids="7343"), fetcher=fetch, material_client=api
        ).crawl()
        assert api.posts == []
        # recorded as no-pdf so a resume does not fetch it again
        assert self._state(cache)["nopdf_ids"] == [7343]
        fetch2 = _FakeFetcher({7343: _page(pdf=False)})
        PpmoCrawler(
            _args(cache, ids="7343"), fetcher=fetch2, material_client=_FakeMaterial()
        ).crawl()
        assert fetch2.calls == []

    def test_transient_fetch_failure_is_retried_next_run(self):
        cache = self._cache()
        fetch = _FakeFetcher({7343: None})  # transient
        PpmoCrawler(
            _args(cache, ids="7343"), fetcher=fetch, material_client=_FakeMaterial()
        ).crawl()
        st = self._state(cache)
        assert 7343 not in st["done_ids"] and 7343 not in st["nopdf_ids"]

    def test_post_failure_leaves_id_unresolved(self):
        cache = self._cache()
        fetch = _FakeFetcher({7343: _page()})
        PpmoCrawler(
            _args(cache, ids="7343"),
            fetcher=fetch,
            material_client=_FakeMaterial(fail_ids=["7343"]),
        ).crawl()
        assert 7343 not in self._state(cache)["done_ids"]

    def test_resume_skips_done_ids(self):
        cache = self._cache()
        pages = {7343: _page()}
        PpmoCrawler(
            _args(cache, ids="7343"),
            fetcher=_FakeFetcher(pages),
            material_client=_FakeMaterial(),
        ).crawl()
        fetch2 = _FakeFetcher(pages)
        PpmoCrawler(
            _args(cache, ids="7343"), fetcher=fetch2, material_client=_FakeMaterial()
        ).crawl()
        assert fetch2.calls == []

    def test_discovery_seeds_frontier(self):
        cache = self._cache()
        home = '<a href="/content/9001/x/">a</a><a href="/content/9002/y/">b</a>'
        fetch = _FakeFetcher({9001: _page(), 9002: _page()}, home=home)
        api = _FakeMaterial()
        PpmoCrawler(
            _args(cache, discover=True), fetcher=fetch, material_client=api
        ).crawl()
        assert {"9001", "9002"} <= {i for i, _, _ in api.posts}

    def test_dry_run_posts_nothing_but_caches(self):
        cache = self._cache()
        fetch = _FakeFetcher({7343: _page()})
        api = _FakeMaterial()
        PpmoCrawler(
            _args(cache, ids="7343", dry_run=True), fetcher=fetch, material_client=api
        ).crawl()
        assert api.posts == []
        lines = [ln for ln in cache.read_text().splitlines() if ln.strip()]
        assert len(lines) == 1
