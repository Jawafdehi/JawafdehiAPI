"""Ingest Nepal Law Journal (नेपाल कानून पत्रिका / NKP) precedents into Materials.

Thin management-command wrapper around the standalone crawler in
:mod:`materials.sourcing.nkp.crawl` (``NkpCrawler``). Running the crawler AS a
management command bootstraps Django (so the shaper's app imports resolve) and
lets the CronJob mint its NGM-role Caseworker bearer via
``review.oidc_client_credentials.resolve_service_bearer`` — so no static token is
baked into the cluster (same auth path as ``scrape_ciaa_press_releases`` /
``scrape_ppmo_blacklist``).

Replaces the retired ``kanun_patrika`` Scrapy spider, which only dumped issue PDFs
to R2 through the now-dead build-index -> ngm_v1 -> sync-materials chain. This
instead crawls the STRUCTURED nkp.gov.np precedent database (detail pages: full
text, headnotes, judges, referenced laws) and POSTs each decision to
``/api/materials/`` (idempotent upsert by ``@id``). It NEVER touches ngm_v1.

Dry-run by default (scrape to a cache, POST nothing); ``--write`` posts. For a
recurring CronJob, bound the run to recent BS years (``--year`` / ``--year-min``):
a stateless pod re-crawls a small, current slice each run and the idempotent
upsert absorbs the overlap, so no persistent checkpoint volume is required.

    manage.py scrape_nkp --year 2082                       # dry-run recon (one year)
    manage.py scrape_nkp --year 2082 --write               # a CronJob run
    manage.py scrape_nkp --year-min 2081 --max-decisions 5 --write   # bounded smoke

The crawl/parse/shape halves live in ``materials.sourcing.nkp`` (unit-tested); this
command only wires arguments, the bearer, and the API base into ``NkpCrawler``.
"""

from __future__ import annotations

import os
import tempfile
from types import SimpleNamespace

from django.core.management.base import BaseCommand, CommandError

from materials.sourcing.nkp.crawl import NkpCrawler

#: Default material-API base (local/dev). The CronJob overrides it with the
#: in-cluster platform host via ``MATERIAL_API_BASE`` / ``INGESTION_API_BASE``.
_DEFAULT_API_BASE = "http://127.0.0.1:8080"


def build_crawler(args) -> NkpCrawler:
    """Factory (a seam so tests inject a fake crawler instead of hitting the site)."""
    return NkpCrawler(args)


class Command(BaseCommand):
    help = "Ingest Nepal Law Journal (NKP) precedents into Materials via the material API."

    def add_arguments(self, parser):
        parser.add_argument(
            "--year", default=None,
            help="limit to one BS year (default: the whole corpus)",
        )
        parser.add_argument(
            "--year-min", type=int, default=None,
            help="shard: lowest BS year, inclusive (recommended for the recurring cron)",
        )
        parser.add_argument(
            "--year-max", type=int, default=None,
            help="shard: highest BS year (inclusive)",
        )
        parser.add_argument(
            "--max-decisions", type=int, default=0,
            help="stop after this many NEW decisions this run (0 = no limit)",
        )
        parser.add_argument(
            "--delay", type=float, default=3.0,
            help="base seconds between source requests (default 3; be polite to nkp.gov.np)",
        )
        parser.add_argument(
            "--transport", choices=["requests", "playwright"], default="requests",
            help="plain HTTP session (default) or a real browser (only if the F5 JS challenge returns)",
        )
        parser.add_argument(
            "--cache", default=None,
            help="decisions.jsonl cache path (default: an ephemeral temp file; a lost cache only "
            "re-posts, which is idempotent)",
        )
        parser.add_argument(
            "--from-cache", action="store_true",
            help="POST the existing cache without re-scraping the site (requires --write)",
        )
        parser.add_argument(
            "--api-base", default=None,
            help="material API base URL (default: $MATERIAL_API_BASE / $INGESTION_API_BASE / loopback)",
        )
        parser.add_argument(
            "--api-token", default=None,
            help="bearer token for writes (default: $INGESTION_API_TOKEN, else an OIDC grant)",
        )
        parser.add_argument(
            "--write", action="store_true",
            help="POST to the material API (default: dry-run — scrape to the cache only)",
        )

    def handle(self, *args, **o):
        write = o["write"]
        if o["from_cache"] and not write:
            raise CommandError("--from-cache re-posts a cache to the API and so requires --write.")

        cache = o["cache"] or os.path.join(tempfile.gettempdir(), "nkp-decisions.jsonl")
        base = (
            o["api_base"]
            or os.environ.get("MATERIAL_API_BASE")
            or os.environ.get("INGESTION_API_BASE")
            or _DEFAULT_API_BASE
        )
        token = self._resolve_token(o, require_token=write)

        # NkpCrawler consumes an argparse-style namespace; build it explicitly so the
        # crawler's own CLI (crawl.main) and this command stay a single source of
        # truth for the crawl knobs.
        crawler_args = SimpleNamespace(
            cache=cache,
            api_base=base,
            token=token,
            dry_run=not write,
            from_cache=o["from_cache"],
            year=o["year"],
            year_min=o["year_min"],
            year_max=o["year_max"],
            delay=o["delay"],
            transport=o["transport"],
            headful=False,
            max_decisions=o["max_decisions"],
        )

        mode = "WRITE" if write else "DRY-RUN"
        if o["year"]:
            scope = f"year={o['year']}"
        elif o["year_min"] or o["year_max"]:
            scope = f"years={o['year_min'] or ''}..{o['year_max'] or ''}"
        else:
            scope = "all-years"
        self.stdout.write(f"scrape_nkp [{mode}] {scope} transport={o['transport']} cache={cache}")
        build_crawler(crawler_args).crawl()

    def _resolve_token(self, o, *, require_token: bool) -> str | None:
        # Dry-run scrapes to the cache and never POSTs, so no bearer is minted (no
        # reason to hit Zitadel for a read-only crawl); use a static token only if set.
        if not require_token:
            return o["api_token"] or os.environ.get("INGESTION_API_TOKEN")

        # Lazy import keeps the OIDC dependency off dry-run / --help.
        from review.oidc_client_credentials import OIDCTokenError, resolve_service_bearer

        try:
            token = resolve_service_bearer(o["api_token"])
        except OIDCTokenError as exc:
            raise CommandError(
                f"--write bearer: OIDC client-credentials grant failed: {exc}"
            ) from exc
        if not token:
            raise CommandError(
                "--write needs a bearer: set INGESTION_API_TOKEN, or the OIDC "
                "client-credentials env (INGESTION_OIDC_CLIENT_ID/SECRET, else the "
                "CASEWORK_OIDC_* service account), or pass --api-token."
            )
        return token
