"""Resumable, polite crawler + material-API client for bolpatra e-GP tenders.

A data-acquisition tool (dev/ops, not request-path product). bolpatra.gov.np/egp
is a legacy server-rendered Java/Struts app (NOT a JSON API) with a broken TLS
chain, so this crawler:

- **Walks the tenderId integer space** (``--id-min/--id-max``) — the PRIMARY and
  reliable frontier. Ids are DENSE from 1 to ~322k (probed), and the search form
  reports ``totalRecords=204925``. Ids past the end (or never allocated) return the
  empty form shell, which the parser rejects → recorded as a gap.
- **Optionally seeds from the search pager** (``--discover-pages``) to pick up the
  newest notices. ⚠️ The pager is **session-stateful and unreliable for exhaustive
  enumeration**: the server ignores/re-bases ``currentPageIndex``, needs
  ``pageAction=next`` for ``pageSize`` to apply at all, and different
  ``startIndex`` values can return overlapping or repeated pages. Use it for
  freshness, NOT for completeness — the id-walk is what guarantees coverage.
- **Fetches** each tender detail (``POST /egp/getTenderDetails`` with ``tenderId``),
  caches the raw HTML-derived record to ``tenders.jsonl`` (resume/audit), shapes it,
  and **POSTs it to the material API** (``POST <api-base>/api/materials/``). Sourcing
  goes through the API plane, not a direct-DB write.

Idempotent server-side: the material upsert is keyed by ``@id`` (unlike the entity
create path), so re-posting simply upserts — no 409 dance.

    # scrape only, no post (needs no token/Django):
    python -m materials.sourcing.bolpatra.crawl --cache /tmp/tenders.jsonl \\
        --discover-pages 5 --dry-run

    # local dev, write to a DEV_AUTH server via HTTP Basic:
    python -m materials.sourcing.bolpatra.crawl --cache /tmp/tenders.jsonl \\
        --api-base http://127.0.0.1:8000 --basic-auth ocr:ocrpass --id-min 319000 --id-max 319100

Super-polite defaults (small government server): concurrency 3, jittered delay,
backoff, ``--max-requests`` for resumable off-peak windows.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from jawafdehi_shared.entities.ids import build_material_iri

from .parse import ParsedTender, extract_tender_ids, parse_tender_detail
from .shaper import BOLPATRA_SOURCE, tender_to_jsonld

EGP_BASE = "https://bolpatra.gov.np/egp"

#: Highest tenderId observed (probed 2026-07; the search form reports
#: ``totalRecords=204925`` over ``numberOfPages=2050``, and ids are DENSE from 1 to
#: roughly here). Ids past the end return the empty form shell → recorded as gaps.
DEFAULT_MAX_TENDER_ID = 325_000
# e-GP's front end blocks the default ``python-requests`` User-Agent (403) but
# serves a browser-like client fine. A plain desktop UA suffices — no JS-challenge
# wall (unlike the NKP portal's F5). Keep a current Chrome UA so the wall passes.
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/150.0.0.0 Safari/537.36"
)


def _now_iso() -> str:
    import datetime
    from zoneinfo import ZoneInfo

    return (
        datetime.datetime.now(ZoneInfo("Asia/Kathmandu"))
        .replace(tzinfo=None)
        .isoformat()
    )


class Checkpoint:
    """Per-id outcomes on disk (``done_ids`` / ``gap_ids``), resumable.

    ``done_ids`` re-seeds from the JSONL cache so a resume survives a lost sidecar.
    Thread-safe (the fetch pool records concurrently).
    """

    def __init__(self, out_path: Path, seed_done_from_cache: bool = True):
        self.out_path = out_path
        self.state_path = out_path.with_suffix(out_path.suffix + ".state.json")
        self.done_ids: set[str] = set()
        self.gap_ids: set[str] = set()
        self._lock = threading.Lock()
        self._fh = out_path.open("a", encoding="utf-8")
        # ``seed_done_from_cache=False`` for --from-cache: a cached row proves the
        # tender was SCRAPED, not that it was PUBLISHED. Seeding done_ids from the
        # cache there would mark every cached tender done and republish nothing —
        # exactly the backlog we are trying to drain. The sidecar's done_ids is the
        # authoritative record of what actually reached the API.
        self._seed_done_from_cache = seed_done_from_cache
        self._load()

    def _load(self) -> None:
        if self._seed_done_from_cache and self.out_path.exists():
            with self.out_path.open(encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                        if rec.get("tender_id"):
                            self.done_ids.add(str(rec["tender_id"]))
                    except json.JSONDecodeError:
                        continue
        if self.state_path.exists():
            try:
                s = json.loads(self.state_path.read_text(encoding="utf-8"))
                self.done_ids.update(str(i) for i in s.get("done_ids", []))
                self.gap_ids.update(str(i) for i in s.get("gap_ids", []))
            except (json.JSONDecodeError, ValueError, TypeError):
                pass

    def seen(self, tid: str) -> bool:
        return tid in self.done_ids or tid in self.gap_ids

    def cache(self, record: dict[str, Any]) -> None:
        with self._lock:
            self._fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._fh.flush()

    def save(self) -> None:
        with self._lock:
            self.state_path.write_text(
                json.dumps(
                    {"done_ids": sorted(self.done_ids), "gap_ids": sorted(self.gap_ids)}
                ),
                encoding="utf-8",
            )

    def close(self) -> None:
        with self._lock:
            self._fh.close()


class EgpFetcher:
    """HTTP transport for e-GP. TLS-tolerant (the chain is broken) POST/GET."""

    def __init__(self, timeout: int = 45, base: str = EGP_BASE):
        import requests
        import urllib3

        # verify=False is deliberate (e-GP's cert chain is incomplete); silence the
        # per-request InsecureRequestWarning so a 400k-tender run doesn't flood logs.
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        self._base = base.rstrip("/")
        self._s = requests.Session()
        self._s.headers.update(
            {
                "User-Agent": UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9,ne;q=0.8",
                "Referer": f"{base.rstrip('/')}/searchOpportunity",
            }
        )
        self._s.verify = False  # e-GP serves an incomplete cert chain.
        self._timeout = timeout

    def get_detail(self, tender_id: str) -> str | None:
        """Tender detail HTML, or ``None`` on a transient/error response (retryable)."""
        try:
            r = self._s.post(
                f"{self._base}/getTenderDetails",
                data={"tenderId": tender_id},
                timeout=self._timeout,
            )
            if not r.ok:
                return None
            return r.text
        except Exception:  # noqa: BLE001 — network/timeout → retryable.
            return None

    def search_page(self, page_index: int, page_size: int = 100) -> str | None:
        """One search-results page HTML (id discovery only), or ``None`` on failure.

        The Struts pager needs ``pageAction=next`` for ``pageSize`` to apply at all
        (without it the server returns its default 10 rows), and it mirrors the
        paging state into ``*Input`` fields — so send both. Even then the pager is
        session-stateful and re-bases the offset, so this is a FRESHNESS seed, not
        an exhaustive enumerator (see the module docstring); the id-walk owns
        completeness.
        """
        start = max(0, (page_index - 1)) * page_size
        try:
            r = self._s.post(
                f"{self._base}/searchOpportunity.action",
                data={
                    "bidSearchTO.publicEntity": "0",
                    "parentPE": "0",
                    "addNewJV": "false",
                    "pageAction": "next",
                    "pageActionInput": "next",
                    "pageSize": page_size,
                    "pageSizeInput": page_size,
                    "currentPageIndex": page_index,
                    "currentPageIndexInput": page_index,
                    "startIndex": start,
                },
                timeout=self._timeout,
            )
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
            self._s.auth = basic_auth  # local DEV_AUTH path.
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


def build_fetcher(timeout: int) -> EgpFetcher:
    """Factory (a seam for tests to inject a fake fetch transport)."""
    return EgpFetcher(timeout=timeout)


def build_material_client(
    api_base: str,
    token: str | None,
    timeout: int,
    basic_auth: tuple[str, str] | None = None,
) -> MaterialApiClient:
    """Factory (a seam for tests to inject a fake material client)."""
    return MaterialApiClient(api_base, token, timeout=timeout, basic_auth=basic_auth)


class BolpatraCrawler:
    """Discovers + fetches e-GP tenders politely and (optionally) publishes them."""

    def __init__(self, args, fetcher=None, material_client=None):
        self.args = args
        self.cache_path = Path(args.cache)
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        # In --from-cache mode the sidecar (not the cache) is the record of what was
        # actually PUBLISHED — see Checkpoint's seed_done_from_cache note.
        self.cp = Checkpoint(
            self.cache_path,
            seed_done_from_cache=not getattr(args, "from_cache", False),
        )
        self.fetcher = fetcher if fetcher is not None else build_fetcher(args.timeout)
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
        self.n_requests = 0
        self.n_new = 0
        self.n_published = 0
        self.n_gap = 0
        self._lock = threading.Lock()

    def _pause(self, factor: float = 1.0) -> None:
        base = self.args.delay * factor
        if base > 0:
            time.sleep(base + random.uniform(0, base * 0.5))

    # --- frontier -----------------------------------------------------------

    def _discover_ids(self) -> list[str]:
        """Seed the frontier: explicit id range, and/or search-page discovery."""
        ids: list[str] = []
        if self.args.id_max:
            ids.extend(str(i) for i in range(self.args.id_min, self.args.id_max + 1))
        # Pager pages are 1-BASED. Dedup as we go and stop early when a page adds
        # nothing new (the pager repeats itself — see the module docstring).
        seen_from_pager: set[str] = set()
        for page in range(1, self.args.discover_pages + 1):
            self._pause()
            html = self.fetcher.search_page(page, self.args.page_size)
            if not html:
                break
            found = extract_tender_ids(html)
            fresh = [i for i in found if i not in seen_from_pager]
            if not fresh:
                print(
                    f"  discover page {page}: +0 new (pager repeated) — stopping discovery",
                    file=sys.stderr,
                )
                break
            seen_from_pager.update(fresh)
            ids.extend(fresh)
            print(f"  discover page {page}: +{len(fresh)} new ids", file=sys.stderr)
        # Dedup, order-preserving, minus what's already done.
        seen: dict[str, None] = {}
        for i in ids:
            if not self.cp.seen(i):
                seen.setdefault(i, None)
        return list(seen)

    # --- main walk ----------------------------------------------------------

    def crawl(self) -> None:
        try:
            if getattr(self.args, "from_cache", False):
                self._publish_cache()
            else:
                self._run()
        finally:
            self.cp.save()
            self.cp.close()
            self.fetcher.close()
            if self.api is not None:
                self.api.close()

    def _publish_cache(self) -> None:
        """Publish every cached tender to the material API without re-scraping e-GP.

        For re-driving ingestion after an API-side failure (e.g. a DRF throttle 429
        storm) — the scrape already succeeded, so re-fetching bolpatra would be pure
        waste (and rude). Posting is idempotent by ``@id``, so already-published
        tenders simply upsert; the checkpoint still skips ids marked done.

        Unlike the crawl path, a POST failure here RETRIES with backoff (up to
        ``--retries``) because the whole point of this mode is to drain a backlog
        that failed on the API side.
        """
        if self.api is None:
            print(
                "FATAL: --from-cache needs the API (remove --dry-run).", file=sys.stderr
            )
            return
        published = failed = skipped = 0
        with self.cache_path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                tid = str(record.get("tender_id") or "")
                if not tid:
                    continue
                if tid in self.cp.done_ids:
                    skipped += 1
                    continue
                record.pop("_scraped_at", None)
                try:
                    tender = ParsedTender(**record)
                except TypeError:
                    continue  # cache row from an older schema — skip, don't crash.
                if self._publish_with_retry(tid, tender):
                    published += 1
                else:
                    failed += 1
                if (published + failed) % 500 == 0:
                    self.cp.save()
                    print(
                        f"  … republish: published={published} failed={failed} "
                        f"skipped(already done)={skipped}",
                        file=sys.stderr,
                    )
        self.cp.save()
        print(
            f"from-cache done: published={published} failed={failed} "
            f"skipped(already done)={skipped}",
            file=sys.stderr,
        )

    def _publish_with_retry(self, tid: str, tender) -> bool:
        """POST one tender, retrying a throttle/5xx with exponential backoff."""
        doc, material_type = tender_to_jsonld(tender)
        doc["@id"] = build_material_iri(BOLPATRA_SOURCE, tid)
        for attempt in range(1, max(1, self.args.retries) + 1):
            try:
                self.api.post(doc, material_type)
            except MaterialApiError as e:
                msg = str(e)
                if attempt >= self.args.retries:
                    print(f"  ! give up on tender {tid}: {msg[:120]}", file=sys.stderr)
                    return False
                # A 429 tells us how long to wait; otherwise exponential backoff.
                wait = min(30.0, 2.0**attempt)
                if "429" in msg:
                    import re as _re

                    m = _re.search(r"in (\d+) second", msg)
                    if m:
                        wait = float(m.group(1)) + 0.5
                time.sleep(wait)
                continue
            self.cp.done_ids.add(tid)
            self.n_published += 1
            return True
        return False

    def _run(self) -> None:
        mode = (
            "dry-run (cache only)"
            if self.args.dry_run
            else f"→ {self.args.api_base}/api/materials/"
        )
        todo = self._discover_ids()
        print(
            f"bolpatra crawl: {len(todo)} tenders to fetch "
            f"(concurrency={self.args.concurrency}, delay={self.args.delay}s); publish {mode}",
            file=sys.stderr,
        )
        with ThreadPoolExecutor(max_workers=self.args.concurrency) as pool:
            futures = {}
            it = iter(todo)
            for tid in _take(it, self.args.concurrency * 2):
                futures[pool.submit(self._fetch, tid)] = tid
            while futures:
                for fut in as_completed(list(futures)):
                    tid = futures.pop(fut)
                    self._handle(tid, fut.result())
                    self.n_requests += 1
                    if self.n_requests % 200 == 0:
                        self.cp.save()
                        self._progress()
                    if self._hit_limit():
                        print(
                            "  reached --max-requests; stopping (resumable).",
                            file=sys.stderr,
                        )
                        return
                    for nxt in _take(it, 1):
                        futures[pool.submit(self._fetch, nxt)] = nxt
                    break
        self._progress(final=True)

    def _fetch(self, tid: str) -> str | None:
        for attempt in range(1, self.args.retries + 1):
            self._pause()
            html = self.fetcher.get_detail(tid)
            if html is not None:
                return html
            if attempt < self.args.retries:
                time.sleep(min(60.0, 2.0**attempt))
        return None

    def _handle(self, tid: str, html: str | None) -> None:
        if html is None:
            return  # retryable — leave un-checkpointed.
        tender = parse_tender_detail(html, tid)
        if tender is None:
            self.cp.gap_ids.add(tid)
            self.n_gap += 1
            return
        record = {**tender.__dict__, "_scraped_at": _now_iso()}
        self.cp.cache(record)
        self.n_new += 1
        self._publish(tid, tender)

    def _publish(self, tid: str, tender) -> None:
        if self.api is None:  # dry-run
            self.cp.done_ids.add(tid)
            return
        doc, material_type = tender_to_jsonld(tender)
        doc["@id"] = build_material_iri(BOLPATRA_SOURCE, tid)
        try:
            self.api.post(doc, material_type)
        except MaterialApiError as e:
            print(f"  ! material POST failed for tender {tid}: {e}", file=sys.stderr)
            return  # retryable — leave un-checkpointed (upsert is idempotent).
        self.cp.done_ids.add(tid)
        self.n_published += 1

    def _hit_limit(self) -> bool:
        return (
            bool(self.args.max_requests) and self.n_requests >= self.args.max_requests
        )

    def _progress(self, final: bool = False) -> None:
        tag = "done" if final else "…"
        print(
            f"  {tag}: fetched={self.n_requests} new={self.n_new} "
            f"published={self.n_published} gaps={self.n_gap}",
            file=sys.stderr,
        )


def _take(it, n: int) -> list:
    out = []
    for _ in range(n):
        try:
            out.append(next(it))
        except StopIteration:
            break
    return out


def add_arguments(ap: argparse.ArgumentParser) -> None:
    ap.add_argument(
        "--cache",
        required=True,
        help="tenders.jsonl cache path (appended; resume source)",
    )
    ap.add_argument("--api-base", help="Platform base URL (required unless --dry-run)")
    ap.add_argument("--token", help="Bearer for the material API (NGM-role gated).")
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
        "--from-cache",
        action="store_true",
        help="Publish the EXISTING cache without re-scraping e-GP (drains an "
        "API-side backlog, e.g. after a throttle 429 storm); retries with backoff",
    )
    ap.add_argument("--id-min", type=int, default=1, help="Lowest tenderId (default 1)")
    ap.add_argument(
        "--id-max",
        type=int,
        default=0,
        help="Highest tenderId (0 = discovery-only unless --full)",
    )
    ap.add_argument(
        "--full",
        action="store_true",
        help=f"Walk the whole tenderId space (--id-min..{DEFAULT_MAX_TENDER_ID}) — "
        "the reliable frontier for completeness",
    )
    ap.add_argument(
        "--discover-pages",
        type=int,
        default=0,
        help="Seed from N search pages (FRESHNESS only — the pager is not exhaustive)",
    )
    ap.add_argument(
        "--page-size",
        type=int,
        default=100,
        help="Search page size for discovery (default 100)",
    )
    ap.add_argument(
        "--concurrency",
        type=int,
        default=3,
        help="Concurrent fetches (default 3 — be polite)",
    )
    ap.add_argument(
        "--delay",
        type=float,
        default=0.7,
        help="Base seconds between fetches (default 0.7)",
    )
    ap.add_argument(
        "--retries", type=int, default=4, help="Fetch attempts per id (default 4)"
    )
    ap.add_argument(
        "--timeout", type=int, default=45, help="HTTP timeout seconds (default 45)"
    )
    ap.add_argument(
        "--max-requests",
        type=int,
        default=0,
        help="Stop after N fetched this run (0=all; resumable)",
    )


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Crawl bolpatra e-GP tenders and POST them to /api/materials/."
    )
    add_arguments(ap)
    args = ap.parse_args(argv)
    if not args.dry_run and not args.api_base:
        ap.error("--api-base is required unless --dry-run is set.")
    # --full walks the whole (dense) tenderId space — the reliable frontier.
    if args.full and not args.id_max:
        args.id_max = DEFAULT_MAX_TENDER_ID
    if not args.from_cache and not args.id_max and not args.discover_pages:
        ap.error(
            "give a frontier: --full, an --id-min/--id-max range, "
            "and/or --discover-pages N."
        )
    args.basic_auth = None
    if args.basic_auth_raw:
        if ":" not in args.basic_auth_raw:
            ap.error("--basic-auth must be USER:PASS")
        user, _, password = args.basic_auth_raw.partition(":")
        args.basic_auth = (user, password)
    if not args.dry_run and not args.token and not args.basic_auth:
        args.token = _mint_bearer()
    BolpatraCrawler(args).crawl()


def _mint_bearer() -> str:
    """Mint the NGM-role bearer for the material API from the OIDC client-
    credentials env (the ``sa-ingestion`` identity), so an in-cluster CronJob
    carries NO static token — same pattern as the NKP crawler. A bare ``python -m``
    run does not bootstrap Django, and ``resolve_service_bearer`` reads
    Django-settings fallbacks, so configure settings first (idempotent).
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
            "FATAL: no material-API bearer. Set INGESTION_OIDC_CLIENT_ID/SECRET (else "
            "CASEWORK_OIDC_*), pass --token or --basic-auth, or run with --dry-run.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return token


if __name__ == "__main__":
    main()
