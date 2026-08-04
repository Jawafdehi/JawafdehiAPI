"""Refresh the PPMO blacklist (blacklisted firms) into the platform.

Replaces the retired ``ppmo_blacklist`` Scrapy spider (archived ``Jawafdehi/ngm``).
PPMO rebuilt its blacklist as a React/Yii2 SPA; the old ``old.ppmo.gov.np`` HTML
tables are dead. This fetches the public JSON feed
(``blacklist.ppmo.gov.np/api/info/company-list`` — every firm in one response,
valid TLS, no auth) and POSTs the parsed rows to the platform's REST ingestion
plane (``POST /api/ingestion/firms/``) — the server owns the idempotent upsert,
validation, and auditlog. This command is a thin CLIENT; it never touches the ORM.

Dry-run by default (fetch + parse, report counts, POST nothing); ``--write``
posts. The ingestion endpoint is NGM-role gated, so ``--write`` needs a Caseworker
bearer (base URL via ``INGESTION_API_BASE``). The bearer is resolved by
``review.oidc_client_credentials.resolve_service_bearer``: a static
``INGESTION_API_TOKEN`` (local dev), else an OIDC client-credentials grant as the
``sa-ingestion`` account (``INGESTION_OIDC_CLIENT_ID/SECRET``) or the shared
``CASEWORK_OIDC_*`` service account — so the CronJob carries no static token.

    manage.py scrape_ppmo_blacklist                          # dry-run recon
    INGESTION_API_TOKEN=… manage.py scrape_ppmo_blacklist --write   # the CronJob run
    manage.py scrape_ppmo_blacklist --limit 20               # cap firms (smoke test)

The pure parse/shape half lives in ``courts.scraper.ppmo`` (unit-tested). Dropped
from the legacy: the per-firm R2 JSON metadata (fed the retired DocumentSource
index) and NES mapping (``nes_id`` stays null; firm→entity linking is separate).
"""

from __future__ import annotations

import json
import os
import time
import urllib.request

from django.core.management.base import BaseCommand, CommandError

from courts.scraper import ppmo as P

#: Default ingestion base URL (local/dev). The CronJob overrides it with the
#: in-cluster platform service via ``INGESTION_API_BASE``.
_DEFAULT_API_BASE = "http://127.0.0.1:8080"


class BlacklistApiClient:
    """GETs the PPMO blacklist JSON feed. Valid TLS (verify on), never raises —
    a transport failure returns ``status=None`` so the caller can stop cleanly.
    """

    def __init__(self, timeout: int = 60):
        import requests

        self._session = requests.Session()
        self._session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Jawafdehi-ppmo-crawler",
                "Accept": "application/json",
            }
        )
        self._timeout = timeout

    def get(self, url: str) -> tuple[int | None, str]:
        try:
            resp = self._session.get(url, timeout=self._timeout)
        except Exception:  # noqa: BLE001 - any portal/parse failure is a miss, not a crash
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


def build_source_client(timeout: int) -> BlacklistApiClient:
    """Factory (a seam for tests to inject a fake source transport)."""
    return BlacklistApiClient(timeout=timeout)


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
            help="max firms to post (0 = no limit; the whole feed)",
        )
        parser.add_argument(
            "--batch-size", type=int, default=200,
            help="firms per ingestion POST (default 200)",
        )
        parser.add_argument(
            "--delay", type=float, default=0.5,
            help="seconds between ingestion POSTs (default 0.5)",
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
            help="POST to the ingestion API (default: dry-run — fetch + parse only)",
        )

    def handle(self, *args, **o):
        write = o["write"]
        ingestion = self._ingestion_client(o) if write else None

        firms = self._fetch_firms(o["timeout"])
        if o["limit"]:
            firms = firms[: o["limit"]]
        payloads = [P.to_payload(f) for f in firms]

        mode = "WRITE" if write else "DRY-RUN"
        self.stdout.write(f"scrape_ppmo_blacklist [{mode}] parsed={len(payloads)} firms")

        if not write:
            self.stdout.write(f"done [dry-run]: {len(payloads)} firms (nothing posted)")
            return

        delay = max(0.0, o["delay"])
        totals = {"created": 0, "updated": 0, "unchanged": 0, "failed": 0}
        for batch in _chunks(payloads, o["batch_size"]):
            try:
                resp = ingestion.post_firms(batch)
            except Exception as exc:  # noqa: BLE001 — one bad batch must not kill the run
                totals["failed"] += len(batch)
                self.stderr.write(
                    f"  ingestion POST failed for a {len(batch)}-firm batch "
                    f"(counted failed, continuing): {exc}"
                )
                continue
            for key in totals:
                totals[key] += int(resp.get(key, 0) or 0)
            for result in resp.get("results", []):
                if result.get("status") == "failed":
                    self.stderr.write(
                        f"  ingestion rejected [{result.get('index')}]: {result.get('errors')}"
                    )
            if delay:
                time.sleep(delay)

        self.stdout.write(
            f"done: posted={len(payloads)} | "
            + " ".join(f"{key}={value}" for key, value in totals.items())
        )

    def _fetch_firms(self, timeout: int):
        """GET the feed once and parse it into ``ParsedFirm`` rows."""
        status, body = build_source_client(timeout).get(P.API_URL)
        if status != 200 or not body:
            raise CommandError(f"PPMO blacklist API returned status {status}")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise CommandError(f"PPMO blacklist API returned non-JSON: {exc}") from exc
        return P.parse_company_list(payload)

    def _ingestion_client(self, o):
        # Lazy import: keeps the (review) OIDC dep off the module-load path so a
        # dry-run / --help never needs it, and avoids any app-import cycle.
        from review.oidc_client_credentials import OIDCTokenError, resolve_service_bearer

        base = o["api_base"] or os.environ.get("INGESTION_API_BASE") or _DEFAULT_API_BASE
        try:
            token = resolve_service_bearer(o["api_token"])
        except OIDCTokenError as exc:
            raise CommandError(f"--write bearer: OIDC client-credentials grant failed: {exc}") from exc
        if not token:
            raise CommandError(
                "--write needs an ingestion bearer: set INGESTION_API_TOKEN, or the "
                "OIDC client-credentials env (INGESTION_OIDC_CLIENT_ID/SECRET, else the "
                "CASEWORK_OIDC_* service account), or pass --api-token."
            )
        return build_ingestion_client(base, token, o["timeout"])
