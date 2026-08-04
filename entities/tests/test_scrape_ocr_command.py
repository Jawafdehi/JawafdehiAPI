"""OCR crawler control-flow tests (fake fetcher + fake entity API).

Injects a fake OCR fetch transport AND a fake entity client via the ``build_*``
seams and asserts the ID-walk's control flow: skip ids already checkpointed
(resume), 409→skip idempotently, 422→bad_ids (never retried), gaps recorded,
dry-run posts nothing, and ``--max-requests`` caps a run. The server-side create is
exercised elsewhere; here nothing hits the DB or network. ``time.sleep`` is patched
out so the polite delays don't slow the suite.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from django.test import SimpleTestCase

from entities.sourcing.ocr import crawl as C
from entities.sourcing.ocr.crawl import FetchResult, OcrCrawler

CRAWL = "entities.sourcing.ocr.crawl"


def _rec(cid, status="APPROVED", **over):
    rec = {
        "companyId": cid,
        "companyNameEnglish": f"Company {cid}",
        "registrationNumber": str(300000 + cid),
        "status": status,
        "companyTypeCategory": {"baseValue": "PRIVATE"},
    }
    rec.update(over)
    return rec


class _FakeFetcher:
    """cid → OCR ``data`` dict (a record), or ``None`` for a gap. Missing ids gap."""

    def __init__(self, records):
        self.records = records
        self.calls = []

    def get_company(self, cid):
        self.calls.append(cid)
        if cid in self.records:
            return FetchResult("record", data=dict(self.records[cid]))
        return FetchResult("gap")

    def close(self):
        pass


class _FakeEntityClient:
    """Records POSTs; returns a scripted status per slug (default 'created').

    ``statuses`` maps a slug → one of 'created' | 'exists' | raise-422 | raise-5xx.
    """

    def __init__(self, statuses=None):
        self.statuses = statuses or {}
        self.posts = []

    def post(self, payload):
        self.posts.append(payload)
        action = self.statuses.get(payload["slug"], "created")
        if action == "422":
            raise C.EntityValidationError("422: bad")
        if action == "5xx":
            raise C.EntityApiError("503: down")
        return action  # 'created' or 'exists'

    def close(self):
        pass


def _args(cache, **over):
    """A crawler arg namespace with test-fast defaults (no delays/retries)."""
    import argparse

    base = dict(
        cache=str(cache),
        api_base="http://api",
        token="t",
        dry_run=False,
        from_cache=False,
        id_min=1,
        id_max=5,
        concurrency=1,
        delay=0.0,
        retries=1,
        timeout=5,
        max_requests=0,
        circuit_breaker=1000,
        circuit_pause=0.0,
    )
    base.update(over)
    return argparse.Namespace(**base)


class OcrCrawlerControlFlowTests(SimpleTestCase):
    def setUp(self):
        # No real sleeping in the suite.
        p = patch(f"{CRAWL}.time.sleep", lambda *_a, **_k: None)
        p.start()
        self.addCleanup(p.stop)

    def _cache(self, name="ocr.jsonl"):
        import tempfile
        from pathlib import Path

        d = tempfile.mkdtemp()
        return Path(d) / name

    def test_publishes_approved_skips_draft_and_records_gaps(self):
        cache = self._cache()
        fetch = _FakeFetcher({1: _rec(1), 2: _rec(2, status="DRAFT"), 4: _rec(4)})
        api = _FakeEntityClient()
        OcrCrawler(
            _args(cache, id_min=1, id_max=5), fetcher=fetch, entity_client=api
        ).crawl()

        # ids 1 & 4 published; 2 (DRAFT) cached-not-posted; 3 & 5 gaps.
        posted = sorted(p["slug"] for p in api.posts)
        assert posted == ["company-1-300001", "company-4-300004"]
        # every id resolved to a terminal outcome (done or gap) — full coverage.
        state = json.loads((cache.with_suffix(".jsonl.state.json")).read_text())
        assert set(state["done_ids"]) == {1, 2, 4}
        assert set(state["gap_ids"]) == {3, 5}

    def test_409_is_skipped_idempotently(self):
        cache = self._cache()
        fetch = _FakeFetcher({1: _rec(1)})
        api = _FakeEntityClient(statuses={"company-1-300001": "exists"})
        cr = OcrCrawler(
            _args(cache, id_min=1, id_max=1), fetcher=fetch, entity_client=api
        )
        cr.crawl()
        assert cr.n_exists == 1 and cr.n_published == 0
        # 409 still checkpoints the id (so a re-run won't re-POST it).
        assert 1 in cr.cp.done_ids

    def test_422_goes_to_bad_ids_and_is_not_retried(self):
        cache = self._cache()
        fetch = _FakeFetcher({1: _rec(1)})
        api = _FakeEntityClient(statuses={"company-1-300001": "422"})
        cr = OcrCrawler(
            _args(cache, id_min=1, id_max=1), fetcher=fetch, entity_client=api
        )
        cr.crawl()
        assert cr.n_bad == 1
        assert 1 in cr.cp.bad_ids and 1 not in cr.cp.done_ids
        # a bad id counts as seen → a resume does not retry it.
        assert cr.cp.seen(1)

    def test_5xx_leaves_id_uncheckpointed_for_retry(self):
        cache = self._cache()
        fetch = _FakeFetcher({1: _rec(1)})
        api = _FakeEntityClient(statuses={"company-1-300001": "5xx"})
        cr = OcrCrawler(
            _args(cache, id_min=1, id_max=1), fetcher=fetch, entity_client=api
        )
        cr.crawl()
        # not done, not bad, not gap → the next run retries it.
        assert not cr.cp.seen(1)

    def test_resume_skips_already_done_ids(self):
        cache = self._cache()
        recs = {1: _rec(1), 2: _rec(2)}
        # First run does id 1..2.
        OcrCrawler(
            _args(cache, id_min=1, id_max=2),
            fetcher=_FakeFetcher(recs),
            entity_client=_FakeEntityClient(),
        ).crawl()
        # Second run over the same range must fetch nothing.
        fetch2 = _FakeFetcher(recs)
        OcrCrawler(
            _args(cache, id_min=1, id_max=2),
            fetcher=fetch2,
            entity_client=_FakeEntityClient(),
        ).crawl()
        assert fetch2.calls == []

    def test_dry_run_posts_nothing_but_caches(self):
        cache = self._cache()
        fetch = _FakeFetcher({1: _rec(1), 2: _rec(2)})
        api = _FakeEntityClient()
        # dry_run=True → the crawler ignores the injected client and posts nothing.
        OcrCrawler(
            _args(cache, id_min=1, id_max=2, dry_run=True),
            fetcher=fetch,
            entity_client=api,
        ).crawl()
        assert api.posts == []
        # but the raw records were cached (audit trail).
        lines = [ln for ln in cache.read_text().splitlines() if ln.strip()]
        assert len(lines) == 2

    def test_max_requests_caps_the_run(self):
        cache = self._cache()
        fetch = _FakeFetcher({i: _rec(i) for i in range(1, 11)})
        cr = OcrCrawler(
            _args(cache, id_min=1, id_max=10, max_requests=3),
            fetcher=fetch,
            entity_client=_FakeEntityClient(),
        )
        cr.crawl()
        assert cr.n_requests == 3  # stopped early; remaining ids resume next run.

    def test_from_cache_publishes_without_fetching(self):
        cache = self._cache()
        # Seed the cache with two records (one DRAFT that must not post).
        cache.write_text(
            json.dumps(_rec(1)) + "\n" + json.dumps(_rec(2, status="DRAFT")) + "\n",
            encoding="utf-8",
        )
        fetch = _FakeFetcher({})  # must never be called
        api = _FakeEntityClient()
        OcrCrawler(
            _args(cache, from_cache=True), fetcher=fetch, entity_client=api
        ).crawl()
        assert fetch.calls == []
        assert [p["slug"] for p in api.posts] == ["company-1-300001"]


class OcrCliTests(SimpleTestCase):
    """``main()`` CLI wiring: dry-run needs no auth; --basic-auth skips the mint."""

    def _cache(self):
        import tempfile
        from pathlib import Path

        return Path(tempfile.mkdtemp()) / "ocr.jsonl"

    def test_dry_run_needs_no_api_base_or_auth(self):
        cache = self._cache()
        fetch = _FakeFetcher({1: _rec(1)})
        # A dry-run must never try to mint a bearer or require --api-base.
        with (
            patch(f"{CRAWL}.build_fetcher", return_value=fetch),
            patch(f"{CRAWL}._mint_bearer", side_effect=AssertionError("must not mint")),
        ):
            C.main(
                [
                    "--cache",
                    str(cache),
                    "--id-min",
                    "1",
                    "--id-max",
                    "1",
                    "--dry-run",
                    "--delay",
                    "0",
                ]
            )
        # nothing posted (no client); the record was cached.
        lines = [ln for ln in cache.read_text().splitlines() if ln.strip()]
        assert len(lines) == 1

    def test_basic_auth_skips_bearer_mint_and_authenticates_client(self):
        cache = self._cache()
        fetch = _FakeFetcher({1: _rec(1)})
        captured = {}

        def _fake_build_client(api_base, token, timeout, basic_auth=None):
            captured["token"] = token
            captured["basic_auth"] = basic_auth
            return _FakeEntityClient()

        with (
            patch(f"{CRAWL}.build_fetcher", return_value=fetch),
            patch(f"{CRAWL}.build_entity_client", side_effect=_fake_build_client),
            patch(f"{CRAWL}._mint_bearer", side_effect=AssertionError("must not mint")),
        ):
            C.main(
                [
                    "--cache",
                    str(cache),
                    "--api-base",
                    "http://localhost:8000",
                    "--basic-auth",
                    "admin:secret",
                    "--id-min",
                    "1",
                    "--id-max",
                    "1",
                    "--delay",
                    "0",
                ]
            )
        # --basic-auth path: no bearer minted, credentials handed to the client.
        assert captured["token"] is None
        assert captured["basic_auth"] == ("admin", "secret")

    def test_basic_auth_rejects_malformed_value(self):
        cache = self._cache()
        with self.assertRaises(SystemExit):
            C.main(
                [
                    "--cache",
                    str(cache),
                    "--api-base",
                    "http://x",
                    "--basic-auth",
                    "nocolon",
                ]
            )
