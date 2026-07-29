"""scrape_nkp: the thin command wrapper around NkpCrawler.

Injects a fake crawler via the ``build_crawler`` seam and asserts the command's
only job: assemble the crawler args correctly (dry_run vs write, year sharding,
api base/token), mint the bearer ONLY on ``--write``, and fail loudly when a write
has no bearer. The crawl itself (fetch → shape → POST) is covered by the nkp
parse/shaper/ingest tests; nothing here hits the network or the DB.
"""

from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase

CMD = "materials.management.commands.scrape_nkp"
BEARER = "review.oidc_client_credentials.resolve_service_bearer"


class _FakeCrawler:
    def __init__(self, args):
        self.args = args
        self.crawled = False

    def crawl(self):
        self.crawled = True


class ScrapeNkpCommandTests(SimpleTestCase):
    def _run(self, *extra, bearer="tok"):
        """Run the command with build_crawler + the OIDC mint patched. Returns the
        fake crawler that was built (or None if none was)."""
        holder = {}

        def _factory(args):
            holder["crawler"] = _FakeCrawler(args)
            return holder["crawler"]

        with patch(f"{CMD}.build_crawler", side_effect=_factory), patch(
            BEARER, return_value=bearer
        ):
            call_command("scrape_nkp", *extra)
        return holder.get("crawler")

    def test_dry_run_builds_dry_crawler_and_mints_no_bearer(self):
        holder = {}

        def _factory(args):
            holder["crawler"] = _FakeCrawler(args)
            return holder["crawler"]

        with patch(f"{CMD}.build_crawler", side_effect=_factory), patch(BEARER) as mint:
            call_command("scrape_nkp", "--year", "2082")

        crawler = holder["crawler"]
        assert crawler.crawled is True
        assert crawler.args.dry_run is True
        assert crawler.args.token is None
        assert crawler.args.year == "2082"
        mint.assert_not_called()

    def test_write_mints_bearer_and_drives_live_crawler(self):
        crawler = self._run("--year", "2082", "--write", bearer="BEARER")
        assert crawler.crawled is True
        assert crawler.args.dry_run is False
        assert crawler.args.token == "BEARER"

    def test_write_without_bearer_errors_before_building(self):
        with patch(BEARER, return_value=None), patch(f"{CMD}.build_crawler") as factory:
            with self.assertRaises(CommandError):
                call_command("scrape_nkp", "--year", "2082", "--write")
            factory.assert_not_called()

    def test_year_sharding_passes_through(self):
        crawler = self._run("--year-min", "2081", "--year-max", "2083", "--write")
        assert crawler.args.year is None
        assert crawler.args.year_min == 2081
        assert crawler.args.year_max == 2083

    def test_api_base_resolves_from_flag(self):
        crawler = self._run("--api-base", "http://api.example", "--year", "2082")
        assert crawler.args.api_base == "http://api.example"

    def test_from_cache_requires_write(self):
        with self.assertRaises(CommandError):
            call_command("scrape_nkp", "--from-cache")
