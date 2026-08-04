"""Resumable crawler + material-API client for PPMO publications
(``ppmo.gov.np``) — procurement bulletins (खरिद पत्रिका), annual reports
(वार्षिक प्रतिवेदन), and policy documents.

PPMO's site runs a GIWMS CMS that is mostly JS-rendered, BUT the initial HTML of
each ``/content/{id}/`` page embeds the attached PDF url (on
``giwmscdnone.gov.np/media/...``). So this crawler walks content ids, extracts the
PDF + title with the pure helpers in :mod:`materials.sourcing.ppmo.shaper`, and
POSTs each as an ``OFFICIAL_REPORT`` material. HTTP client only — never ORM-direct
(see ``materials/sourcing/README.md``).

⚠️ **The PDFs are SCANNED IMAGES.** Their Nepali text (where contract-award tables
live) needs LLM-vision OCR — likhit + ``markitdown-ocr`` with a vision API key
(``OPENAI_API_KEY`` / ``GEMINI_API_KEY`` + ``MARKITDOWN_OCR_MODEL``). This crawler
ingests the *documents* so they are discoverable now; the transcript is a deferred
enrichment pass. Nothing here pretends the text was extracted.

    # scrape only (no token/Django needed):
    python -m materials.sourcing.ppmo.crawl --cache /tmp/ppmo.jsonl --dry-run --discover

    # local dev write via HTTP Basic against a DEV_AUTH server:
    python -m materials.sourcing.ppmo.crawl --cache /tmp/ppmo.jsonl --discover \\
        --api-base http://127.0.0.1:8000 --basic-auth ocr:ocrpass
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
from pathlib import Path
from typing import Any

from .shaper import (
    PPMO_BASE,
    extract_pdf_urls,
    extract_title,
    ppmo_report_to_jsonld,
)

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/150.0.0.0 Safari/537.36"
)

#: Content ids known to carry bulletins / annual reports (seed; --discover extends).
SEED_IDS = [7343, 7344, 7345, 7407, 7556, 13243, 13253]

_CONTENT_ID_RE = re.compile(r"/content/(\d+)/")


class PpmoFetcher:
    """HTTP transport for ppmo.gov.np content pages."""

    def __init__(self, timeout: int = 30):
        import requests
        import urllib3

        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        self._s = requests.Session()
        self._s.headers.update({"User-Agent": UA})
        self._s.verify = False
        self._timeout = timeout

    def get_content(self, content_id: int) -> str | None:
        """A ``/content/{id}/`` page's HTML, or ``None`` on failure (retryable)."""
        try:
            r = self._s.get(f"{PPMO_BASE}/content/{content_id}/", timeout=self._timeout)
            return r.text if r.ok else None
        except Exception:  # noqa: BLE001
            return None

    def get_home(self) -> str | None:
        """The PPMO home page HTML (for content-id discovery)."""
        try:
            r = self._s.get(PPMO_BASE + "/", timeout=self._timeout)
            return r.text if r.ok else None
        except Exception:  # noqa: BLE001
            return None

    def close(self) -> None:
        self._s.close()


class MaterialApiError(Exception):
    """A material POST returned a non-2xx (retryable)."""


class MaterialApiClient:
    """POSTs a shaped material to ``<api-base>/api/materials/`` (idempotent upsert)."""

    def __init__(
        self,
        api_base: str,
        token: str | None,
        timeout: int = 60,
        basic_auth: tuple[str, str] | None = None,
    ):
        import requests

        self.url = api_base.rstrip("/") + "/api/materials/"
        self._s = requests.Session()
        self._s.headers.update({"Accept": "application/json"})
        if token:
            self._s.headers["Authorization"] = f"Bearer {token}"
        elif basic_auth:
            self._s.auth = basic_auth
        self._timeout = timeout

    def post(self, doc: dict[str, Any], material_type: str) -> None:
        try:
            r = self._s.post(
                self.url,
                json={"material": doc, "material_type": material_type},
                timeout=self._timeout,
            )
        except Exception as e:  # noqa: BLE001
            raise MaterialApiError(str(e)[:200]) from e
        if r.status_code >= 300:
            raise MaterialApiError(f"{r.status_code}: {r.text[:200]}")

    def close(self) -> None:
        self._s.close()


def build_fetcher(timeout: int) -> PpmoFetcher:
    """Factory (a seam for tests to inject a fake fetch transport)."""
    return PpmoFetcher(timeout=timeout)


def build_material_client(
    api_base: str,
    token: str | None,
    timeout: int,
    basic_auth: tuple[str, str] | None = None,
) -> MaterialApiClient:
    """Factory (a seam for tests to inject a fake material client)."""
    return MaterialApiClient(api_base, token, timeout=timeout, basic_auth=basic_auth)


def discover_content_ids(html: str) -> list[int]:
    """Every ``/content/{id}/`` id referenced on a page (sorted, deduped)."""
    return sorted({int(m) for m in _CONTENT_ID_RE.findall(html or "")})


class PpmoCrawler:
    """Walks PPMO content ids, ingesting each page's PDF as an OFFICIAL_REPORT."""

    def __init__(self, args, fetcher=None, material_client=None):
        self.args = args
        self.cache_path = Path(args.cache)
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path = self.cache_path.with_suffix(
            self.cache_path.suffix + ".state.json"
        )
        self.done_ids: set[int] = set()
        self.nopdf_ids: set[int] = set()
        self._load_state()
        self.fetcher = fetcher if fetcher is not None else build_fetcher(args.timeout)
        # --dry-run is authoritative: no client even if one was injected.
        self.api: MaterialApiClient | None = None
        if not args.dry_run:
            self.api = (
                material_client
                if material_client is not None
                else build_material_client(
                    args.api_base,
                    args.token,
                    args.timeout,
                    basic_auth=getattr(args, "basic_auth", None),
                )
            )
        self.n_cached = self.n_published = self.n_nopdf = 0

    def _load_state(self) -> None:
        if self.state_path.exists():
            try:
                s = json.loads(self.state_path.read_text(encoding="utf-8"))
                self.done_ids = {int(i) for i in s.get("done_ids", [])}
                self.nopdf_ids = {int(i) for i in s.get("nopdf_ids", [])}
            except (json.JSONDecodeError, ValueError, TypeError):
                pass

    def _save_state(self) -> None:
        self.state_path.write_text(
            json.dumps(
                {"done_ids": sorted(self.done_ids), "nopdf_ids": sorted(self.nopdf_ids)}
            ),
            encoding="utf-8",
        )

    def _pause(self) -> None:
        base = self.args.delay
        if base > 0:
            time.sleep(base + random.uniform(0, base * 0.5))

    def crawl(self) -> None:
        try:
            self._run()
        finally:
            self._save_state()
            self.fetcher.close()
            if self.api is not None:
                self.api.close()

    def _frontier(self) -> list[int]:
        """Seed ids + any explicit --ids + (optionally) discovered ids, minus done."""
        ids = set(SEED_IDS)
        if getattr(self.args, "ids", None):
            ids |= {int(i) for i in self.args.ids.split(",") if i.strip().isdigit()}
        if self.args.discover:
            self._pause()
            home = self.fetcher.get_home()
            found = discover_content_ids(home or "")
            print(
                f"  discovery: {len(found)} content ids on the home page",
                file=sys.stderr,
            )
            ids |= set(found)
        seen = self.done_ids | self.nopdf_ids
        return sorted(i for i in ids if i not in seen)

    def _run(self) -> None:
        todo = self._frontier()
        mode = (
            "dry-run (cache only)"
            if self.api is None
            else f"→ {self.args.api_base}/api/materials/"
        )
        print(
            f"PPMO publications: {len(todo)} content pages to check "
            f"({len(self.done_ids | self.nopdf_ids)} already resolved); publish {mode}",
            file=sys.stderr,
        )
        fh = self.cache_path.open("a", encoding="utf-8")
        try:
            for cid in todo:
                self._pause()
                html = self.fetcher.get_content(cid)
                if html is None:
                    continue  # retryable — leave un-resolved for the next run.
                pdfs = extract_pdf_urls(html)
                if not pdfs:
                    # A genuine no-attachment page (a plain notice). Terminal, so a
                    # resume doesn't re-fetch it forever.
                    self.nopdf_ids.add(cid)
                    self.n_nopdf += 1
                    continue
                title = extract_title(html)
                doc, material_type = ppmo_report_to_jsonld(cid, pdfs[0], title)
                fh.write(
                    json.dumps(
                        {
                            "content_id": cid,
                            "title": title,
                            "pdf_url": pdfs[0],
                            "all_pdfs": pdfs,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
                fh.flush()
                self.n_cached += 1
                if self.api is None:
                    self.done_ids.add(cid)
                    continue
                try:
                    self.api.post(doc, material_type)
                except MaterialApiError as e:
                    print(f"  ! POST failed cid={cid}: {e}", file=sys.stderr)
                    continue  # un-resolved → retried next run (upsert is idempotent)
                self.done_ids.add(cid)
                self.n_published += 1
                if (self.n_published + self.n_nopdf) % 25 == 0:
                    self._save_state()
        finally:
            fh.close()
        print(
            f"done: cached={self.n_cached} published={self.n_published} "
            f"no-pdf={self.n_nopdf}",
            file=sys.stderr,
        )


def add_arguments(ap: argparse.ArgumentParser) -> None:
    ap.add_argument(
        "--cache", required=True, help="ppmo.jsonl cache path (resume source)"
    )
    ap.add_argument("--api-base", help="Platform base URL (required unless --dry-run)")
    ap.add_argument("--token", help="Bearer for the material API (NGM-role gated)")
    ap.add_argument(
        "--basic-auth",
        dest="basic_auth_raw",
        metavar="USER:PASS",
        help="HTTP Basic for a LOCAL DEV_AUTH server. Never for prod.",
    )
    ap.add_argument(
        "--dry-run", action="store_true", help="Scrape to the cache only; do NOT post"
    )
    ap.add_argument(
        "--discover",
        action="store_true",
        help="Harvest content ids from the PPMO home page",
    )
    ap.add_argument("--ids", help="Extra content ids to crawl (comma-separated)")
    ap.add_argument(
        "--delay", type=float, default=1.0, help="Base seconds between fetches (1.0)"
    )
    ap.add_argument(
        "--timeout", type=int, default=30, help="HTTP timeout seconds (default 30)"
    )


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Crawl PPMO publications and POST them to /api/materials/."
    )
    add_arguments(ap)
    args = ap.parse_args(argv)
    if not args.dry_run and not args.api_base:
        ap.error("--api-base is required unless --dry-run is set.")
    args.basic_auth = None
    if args.basic_auth_raw:
        if ":" not in args.basic_auth_raw:
            ap.error("--basic-auth must be USER:PASS")
        user, _, password = args.basic_auth_raw.partition(":")
        args.basic_auth = (user, password)
    if not args.dry_run and not args.token and not args.basic_auth:
        args.token = _mint_bearer()
    PpmoCrawler(args).crawl()


def _mint_bearer() -> str:
    """Mint the NGM-role bearer from the OIDC client-credentials env (sa-ingestion),
    so an in-cluster CronJob carries no static token — same pattern as the NKP and
    bolpatra crawlers.
    """
    import os

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    import django

    django.setup()
    from review.oidc_client_credentials import OIDCTokenError, resolve_service_bearer

    try:
        token = resolve_service_bearer()
    except OIDCTokenError as exc:
        print(f"FATAL: could not mint the material-API bearer: {exc}", file=sys.stderr)
        raise SystemExit(2)
    if not token:
        print(
            "FATAL: no material-API bearer. Set INGESTION_OIDC_CLIENT_ID/SECRET "
            "(else CASEWORK_OIDC_*), pass --token or --basic-auth, or use --dry-run.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return token


if __name__ == "__main__":
    main()
