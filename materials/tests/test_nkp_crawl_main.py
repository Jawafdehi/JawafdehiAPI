"""crawl.main(): the in-cluster entry point's self-minting + arg wiring.

The CronJob runs the crawler as a bare module (``python -m
materials.sourcing.nkp.crawl``) with NO static token. main() must therefore mint
the NGM-role Caseworker bearer from the OIDC env when a WRITE run has no
``--token``, skip minting entirely on ``--dry-run`` (which posts nothing and stays
Django-free), leave an explicit ``--token`` untouched, and fail loudly when no
bearer can be produced. NkpCrawler is faked so no fetch/POST happens; nothing here
hits the network or the DB.
"""

from unittest.mock import patch

import pytest

from materials.sourcing.nkp import crawl

CRAWLER = "materials.sourcing.nkp.crawl.NkpCrawler"
BEARER = "review.oidc_client_credentials.resolve_service_bearer"


class _FakeCrawler:
    """Captures the args it was built with; never crawls."""

    last = None

    def __init__(self, args):
        self.args = args
        _FakeCrawler.last = self

    def crawl(self):
        self.crawled = True


def _argv(*extra):
    return ["--cache", "/tmp/nkp-test.jsonl", *extra]


def test_dry_run_skips_minting_and_needs_no_token():
    with patch(CRAWLER, _FakeCrawler), patch(BEARER) as mint:
        crawl.main(_argv("--dry-run", "--year", "2082"))
    mint.assert_not_called()
    assert _FakeCrawler.last.args.dry_run is True
    assert _FakeCrawler.last.args.token is None


def test_write_without_token_self_mints_the_bearer():
    with patch(CRAWLER, _FakeCrawler), patch(BEARER, return_value="MINTED"):
        crawl.main(_argv("--api-base", "http://api", "--year-min", "2082"))
    assert _FakeCrawler.last.args.dry_run is False
    assert _FakeCrawler.last.args.token == "MINTED"


def test_explicit_token_is_not_overridden_by_the_mint():
    with patch(CRAWLER, _FakeCrawler), patch(BEARER) as mint:
        crawl.main(_argv("--api-base", "http://api", "--token", "STATIC"))
    mint.assert_not_called()
    assert _FakeCrawler.last.args.token == "STATIC"


def test_write_with_no_resolvable_bearer_exits_nonzero():
    # A mint that yields nothing must abort the run (never POST unauthenticated).
    with patch(CRAWLER, _FakeCrawler), patch(BEARER, return_value=None):
        with pytest.raises(SystemExit):
            crawl.main(_argv("--api-base", "http://api", "--year", "2082"))


def test_api_base_required_unless_dry_run():
    with patch(CRAWLER, _FakeCrawler):
        with pytest.raises(SystemExit):
            crawl.main(_argv("--year", "2082"))  # write run, no --api-base
