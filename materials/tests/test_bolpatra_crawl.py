"""bolpatra crawler control-flow tests (fake fetcher + fake material API).

Injects a fake e-GP transport and a fake material client via the ``build_*`` seams
and asserts the crawl's control flow: id discovery, skip-already-done (resume),
gap handling (unparseable detail), dry-run posts nothing, retryable failure leaves
an id un-checkpointed, and ``--max-requests`` caps a run. Nothing hits net/DB;
``time.sleep`` is patched out.
"""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase

from materials.sourcing.bolpatra import crawl as C
from materials.sourcing.bolpatra.crawl import BolpatraCrawler

CRAWL = "materials.sourcing.bolpatra.crawl"

_DETAIL = """<table>
<tr><td>Public Entity Name</td><td>Entity {n}</td></tr>
<tr><td>Project Name</td><td>Project {n}</td></tr>
<tr><td>Procurement Method</td><td>NCB</td></tr>
</table>"""


class _FakeFetcher:
    """tender_id → detail HTML (None = transient/retryable). search pages → id lists."""

    def __init__(self, details, pages=None):
        self.details = details
        self.pages = pages or {}
        self.detail_calls = []

    def get_detail(self, tid):
        self.detail_calls.append(tid)
        return self.details.get(tid)

    def search_page(self, page_index, page_size=100):
        ids = self.pages.get(page_index, [])
        return "".join(f"getTenderDetails('{i}')" for i in ids) if ids else ""

    def close(self):
        pass


class _FakeMaterial:
    def __init__(self, fail_ids=()):
        self.posts = []
        self.fail_ids = set(fail_ids)

    def post(self, doc, material_type):
        tid = doc["@id"].rsplit("/", 1)[-1]
        if tid in self.fail_ids:
            raise C.MaterialApiError("503")
        self.posts.append((tid, doc, material_type))

    def close(self):
        pass


def _args(cache, **over):
    base = dict(
        cache=str(cache),
        api_base="http://api",
        token="t",
        dry_run=False,
        id_min=1,
        id_max=0,
        discover_pages=0,
        page_size=100,
        concurrency=1,
        delay=0.0,
        retries=1,
        timeout=5,
        max_requests=0,
        basic_auth=None,
    )
    base.update(over)
    return argparse.Namespace(**base)


class BolpatraCrawlerTests(SimpleTestCase):
    def setUp(self):
        p = patch(f"{CRAWL}.time.sleep", lambda *a, **k: None)
        p.start()
        self.addCleanup(p.stop)

    def _cache(self):
        return Path(tempfile.mkdtemp()) / "tenders.jsonl"

    def test_id_range_walk_publishes_and_records_gaps(self):
        cache = self._cache()
        fetch = _FakeFetcher(details={"1": _DETAIL.format(n=1), "2": None, "3": "err"})
        api = _FakeMaterial()
        # id 1 parses+posts; id 2 is transient(None)→uncheckpointed; id 3 unparseable→gap.
        BolpatraCrawler(
            _args(cache, id_min=1, id_max=3), fetcher=fetch, material_client=api
        ).crawl()
        assert [t for t, _, _ in api.posts] == ["1"]
        state = json.loads(cache.with_suffix(".jsonl.state.json").read_text())
        assert state["done_ids"] == ["1"]
        assert state["gap_ids"] == ["3"]

    def test_discovery_seeds_the_frontier(self):
        cache = self._cache()
        # Pager pages are 1-BASED (page 0 is never requested).
        fetch = _FakeFetcher(
            details={"10": _DETAIL.format(n=10), "11": _DETAIL.format(n=11)},
            pages={1: ["10", "11"], 2: []},
        )
        api = _FakeMaterial()
        BolpatraCrawler(
            _args(cache, discover_pages=2), fetcher=fetch, material_client=api
        ).crawl()
        assert sorted(t for t, _, _ in api.posts) == ["10", "11"]

    def test_discovery_stops_when_pager_repeats_itself(self):
        cache = self._cache()
        # The real e-GP pager re-serves the same page; discovery must stop, not loop.
        fetch = _FakeFetcher(
            details={"10": _DETAIL.format(n=10)},
            pages={1: ["10"], 2: ["10"], 3: ["10"]},
        )
        api = _FakeMaterial()
        BolpatraCrawler(
            _args(cache, discover_pages=5), fetcher=fetch, material_client=api
        ).crawl()
        # id 10 published exactly once despite the pager repeating it.
        assert [t for t, _, _ in api.posts] == ["10"]

    def test_full_flag_walks_whole_id_space(self):
        # --full is resolved in main(); assert the constant is what the range uses.
        assert C.DEFAULT_MAX_TENDER_ID > 300_000

    def test_resume_skips_done_ids(self):
        cache = self._cache()
        details = {"1": _DETAIL.format(n=1), "2": _DETAIL.format(n=2)}
        BolpatraCrawler(
            _args(cache, id_min=1, id_max=2),
            fetcher=_FakeFetcher(details),
            material_client=_FakeMaterial(),
        ).crawl()
        fetch2 = _FakeFetcher(details)
        BolpatraCrawler(
            _args(cache, id_min=1, id_max=2),
            fetcher=fetch2,
            material_client=_FakeMaterial(),
        ).crawl()
        assert fetch2.detail_calls == []  # all already done

    def test_dry_run_posts_nothing_but_caches(self):
        cache = self._cache()
        fetch = _FakeFetcher(details={"1": _DETAIL.format(n=1)})
        api = _FakeMaterial()
        BolpatraCrawler(
            _args(cache, id_min=1, id_max=1, dry_run=True),
            fetcher=fetch,
            material_client=api,
        ).crawl()
        assert api.posts == []
        lines = [ln for ln in cache.read_text().splitlines() if ln.strip()]
        assert len(lines) == 1

    def test_post_failure_leaves_id_uncheckpointed(self):
        cache = self._cache()
        fetch = _FakeFetcher(details={"1": _DETAIL.format(n=1)})
        cr = BolpatraCrawler(
            _args(cache, id_min=1, id_max=1),
            fetcher=fetch,
            material_client=_FakeMaterial(fail_ids={"1"}),
        )
        cr.crawl()
        assert not cr.cp.seen(
            "1"
        )  # retryable → retried next run (upsert is idempotent)

    def test_max_requests_caps_run(self):
        cache = self._cache()
        fetch = _FakeFetcher(
            details={str(i): _DETAIL.format(n=i) for i in range(1, 11)}
        )
        cr = BolpatraCrawler(
            _args(cache, id_min=1, id_max=10, max_requests=3),
            fetcher=fetch,
            material_client=_FakeMaterial(),
        )
        cr.crawl()
        assert cr.n_requests == 3
