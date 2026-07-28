"""Refresh the PPMO blacklist (blacklisted firms) into the platform.

The recurring replacement for the retired ``ppmo_blacklist`` Scrapy spider
(archived ``Jawafdehi/ngm``). Walks the paginated blacklist table on
``old.ppmo.gov.np``, follows each firm's detail page for address/cause, and
POSTs the parsed rows to the platform's REST ingestion plane
(``POST /api/ingestion/firms/``) — the server owns the idempotent upsert,
validation, and auditlog. This command is a thin CLIENT: it never touches the
ORM (writes go through the control plane, same as cases/documents ingestion).

Dry-run by default (scrape + parse, report counts, POST nothing); ``--write``
posts to the ingestion API. The ingestion endpoint is NGM-role gated, so
``--write`` needs an ``sa-ingestion`` (Caseworker) bearer token — supplied via
``INGESTION_API_TOKEN`` (the CronJob injects it from OpenBao) — and a base URL
via ``INGESTION_API_BASE`` (the in-cluster platform service).

    manage.py scrape_ppmo_blacklist                          # dry-run recon
    INGESTION_API_TOKEN=… manage.py scrape_ppmo_blacklist --write   # the CronJob run
    manage.py scrape_ppmo_blacklist --limit 20               # cap firms (smoke test)

The pure parse/shape half lives in ``courts.scraper.ppmo`` (unit-tested). The
legacy spider also wrote a per-firm JSON blob to R2 to feed the retired
DocumentSource index — intentionally dropped (that index is being retired).
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
from urllib.parse import urljoin

from django.core.management.base import BaseCommand, CommandError

from courts.scraper import ppmo as P

#: Safety cap on the pagination walk (the blacklist table is only a few pages).
_MAX_PAGES = 100

#: Default ingestion base URL (local/dev). The CronJob overrides it with the
#: in-cluster platform service via ``INGESTION_API_BASE``.
_DEFAULT_API_BASE = "http://127.0.0.1:8080"


class BlacklistHttpClient:
    """Live transport for the PPMO blacklist source: GET only, TLS-verify off
    (the old subdomain serves a bad cert — the retired spider set
    ``DOWNLOADER_CLIENT_TLS_VERIFY=False``), never raises. A transport failure
    returns ``status=None`` so the caller can stop the walk cleanly.
    """

    def __init__(self, timeout: int = 60):
        import requests
        import urllib3

        # verify=False emits an InsecureRequestWarning per request; silence it
        # once — the insecure fetch is deliberate and scoped to this one host.
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        self._session = requests.Session()
        self._session.headers.update(
            {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Jawafdehi-ppmo-crawler"}
        )
        self._timeout = timeout

    def get(self, url: str) -> tuple[int | None, str]:
        try:
            resp = self._session.get(url, timeout=self._timeout, verify=False)  # noqa: S501
        except Exception:
            return None, ""
        return resp.status_code, resp.text


class IngestionApiClient:
    """Thin client for ``POST /api/ingestion/firms/`` (Bearer-authenticated)."""

    def __init__(self, base_url: str, token: str, timeout: int = 60):
        self._url = base_url.rstrip("/") + "/api/ingestion/firms/"
        self._token = token
        self._timeout = timeout

    def post_firms(self, items: list[dict]) -> dict:
        body = json.dumps({"items": items}).encode("utf-8")
        req = urllib.request.Request(
            self._url,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:  # noqa: S310 (fixed URL)
            return json.loads(resp.read().decode("utf-8"))


def build_scrape_client(timeout: int) -> BlacklistHttpClient:
    """Factory (a seam for tests to inject a fake source transport)."""
    return BlacklistHttpClient(timeout=timeout)


def build_ingestion_client(base_url: str, token: str, timeout: int) -> IngestionApiClient:
    """Factory (a seam for tests to inject a fake ingestion client)."""
    return IngestionApiClient(base_url, token, timeout=timeout)


def _chunks(items: list, size: int):
    for start in range(0, len(items), size):
        yield items[start : start + size]


class Command(BaseCommand):
    help = "Refresh the PPMO blacklist into the platform via the ingestion API."

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit", type=int, default=0,
            help="max firms to scan (0 = no limit; the whole table)",
        )
        parser.add_argument(
            "--max-pages", type=int, default=_MAX_PAGES,
            help=f"pagination safety cap (default {_MAX_PAGES})",
        )
        parser.add_argument(
            "--batch-size", type=int, default=200,
            help="firms per ingestion POST (default 200)",
        )
        parser.add_argument(
            "--delay", type=float, default=1.0,
            help="seconds between HTTP requests (default 1.0)",
        )
        parser.add_argument("--timeout", type=int, default=60, help="HTTP timeout (s)")
        parser.add_argument(
            "--api-base", default=None,
            help="ingestion API base URL (default: $INGESTION_API_BASE or loopback)",
        )
        parser.add_argument(
            "--api-token", default=None,
            help="ingestion bearer token (default: $INGESTION_API_TOKEN)",
        )
        parser.add_argument(
            "--write", action="store_true",
            help="POST to the ingestion API (default: dry-run — scrape + parse only)",
        )

    def handle(self, *args, **o):
        write = o["write"]
        delay = max(0.0, o["delay"])
        scraper = build_scrape_client(o["timeout"])

        ingestion = self._ingestion_client(o) if write else None

        mode = "WRITE" if write else "DRY-RUN"
        self.stdout.write(f"scrape_ppmo_blacklist [{mode}] limit={o['limit'] or '∞'}")

        payloads: list[dict] = []
        scanned = skipped = 0
        for firm in self._walk(scraper, o["max_pages"], delay):
            if o["limit"] and scanned >= o["limit"]:
                break
            scanned += 1
            if not P.resolve_dates(firm):
                skipped += 1
                self.stdout.write(
                    f"  skip (implausible BS date): {firm.firm_name!r} {firm.duration!r}"
                )
                continue
            payloads.append(P.to_payload(firm))

        if not write:
            self.stdout.write(
                f"done [dry-run]: scanned={scanned} valid={len(payloads)} "
                f"skipped={skipped} (nothing posted)"
            )
            return

        totals = {"created": 0, "updated": 0, "unchanged": 0, "failed": 0}
        for batch in _chunks(payloads, o["batch_size"]):
            resp = ingestion.post_firms(batch)
            for key in totals:
                totals[key] += int(resp.get(key, 0) or 0)
            for result in resp.get("results", []):
                if result.get("status") == "failed":
                    self.stderr.write(f"  ingestion failed [{result.get('index')}]: {result.get('errors')}")
            if delay:
                time.sleep(delay)

        self.stdout.write(
            f"done: scanned={scanned} skipped={skipped} posted={len(payloads)} | "
            + " ".join(f"{key}={value}" for key, value in totals.items())
        )

    def _ingestion_client(self, o):
        base = o["api_base"] or os.environ.get("INGESTION_API_BASE") or _DEFAULT_API_BASE
        token = o["api_token"] or os.environ.get("INGESTION_API_TOKEN")
        if not token:
            raise CommandError(
                "--write needs an ingestion bearer token: set INGESTION_API_TOKEN "
                "(the CronJob injects the sa-ingestion token) or pass --api-token."
            )
        return build_ingestion_client(base, token, o["timeout"])

    def _walk(self, client, max_pages: int, delay: float):
        """Yield ``ParsedFirm`` across the paginated list, following detail pages."""
        url = P.LIST_URL
        pages = 0
        while url and pages < max_pages:
            pages += 1
            status, html = client.get(url)
            if status != 200 or not html:
                self.stderr.write(f"list page {status} at {url}; stopping walk")
                return
            firms, next_href = P.parse_list(html)
            for firm in firms:
                if firm.detail_href:
                    if delay:
                        time.sleep(delay)
                    d_status, d_html = client.get(urljoin(url, firm.detail_href))
                    detail = P.parse_detail(d_html) if d_status == 200 else None
                    if detail is None:
                        # Not a real detail page (followed a non-detail link) —
                        # don't emit a half-empty row.
                        continue
                    for key, value in detail.items():
                        setattr(firm, key, value)
                yield firm
            url = urljoin(url, next_href) if next_href else None
            if url and delay:
                time.sleep(delay)
