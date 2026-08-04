"""Resumable, polite ID-walk crawler + entity-API client for the Office of the
Company Registrar, Nepal (company.ocr.gov.np).

A data-acquisition tool (dev/ops, not request-path product). OCR exposes a public,
no-auth JSON API whose companies are keyed by a **dense sequential integer**
(``GET /api/public/v1/company/{id}``, id ≈ 1 .. 427,800). The crawler walks that
id range, caches every record to ``companies.jsonl`` (the on-disk resume/dedup/audit
cache — the "local db"), shapes the real registrations, and **POSTs each to the
platform entity API** (``POST <api-base>/api/entities``). Sourcing goes through the
API plane, not a direct-DB write (see ``entities/sourcing/README.md``).

The management command ``manage.py scrape_ocr_companies`` is the operator entry
point (it resolves the API base + service bearer and injects the fetch/POST clients
via factory seams). This module can also run standalone::

    # scrape only, don't post (populate/refresh the cache; needs no token/Django):
    python -m entities.sourcing.ocr.crawl --cache /path/companies.jsonl --dry-run \\
        --id-min 1 --id-max 200

Design notes:

- **Frontier = an integer range**, not a listing walk — trivially shardable
  (``--id-min/--id-max``) and fully resumable (every id lands in the checkpoint as
  done / gap / bad).
- **Super-polite by default.** Small concurrency (``--concurrency 3``) with a
  per-request jittered ``--delay``, exponential backoff on 5xx, ``Retry-After``
  honored on 429, and a circuit-breaker that pauses the whole crawl on a burst of
  consecutive server errors. ``--max-requests`` caps a single run so a nightly
  off-peak window (Nepal is UTC+5:45) can be resumed across days.
- **Not idempotent server-side.** ``POST /api/entities`` 409s on a duplicate ``@id``
  (it is a create, not an upsert). The crawler treats **409 as already-published →
  checkpoint & skip**, so a re-run is safe. A **422** (validation) is logged to
  ``bad_ids`` and skipped (never retried forever). A **5xx / network** error leaves
  the id un-checkpointed so the next run retries it.
- **Status policy** lives in the shaper: only APPROVED / DEREGISTERED are shaped
  (published); DRAFT / REJECTED are still cached (audit) but not posted.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from .shaper import ocr_company_to_jsonld

#: OCR public API base + the per-company endpoint template.
API_BASE = "https://company.ocr.gov.np/api/public/v1"
#: Highest company id observed (binary-searched 2026-07); a safe default upper
#: bound. Override with --id-max; the crawl also stops after a long run of
#: consecutive gaps past the last real id (see NoMoreIds).
DEFAULT_MAX_ID = 430_000

#: Identify the crawler honestly (a courtesy to the OCR operators).
UA = "Jawafdehi-OCR-crawler/1.0 (+https://jawafdehi.org; company-registry ingest)"


def _now_iso() -> str:
    """Kathmandu-naive ``scraped_at`` timestamp (stdlib zoneinfo, no pytz dep)."""
    import datetime
    from zoneinfo import ZoneInfo

    return (
        datetime.datetime.now(ZoneInfo("Asia/Kathmandu"))
        .replace(tzinfo=None)
        .isoformat()
    )


class Checkpoint:
    """Tracks per-id outcomes on disk so a crawl is fully resumable.

    Sidecar ``<cache>.state.json`` holds four id sets:
      * ``done_ids``  — cached AND (published | skipped-non-publishable | 409).
      * ``gap_ids``   — 200-with-null-data or 400 (no such company).
      * ``bad_ids``   — shaped but the API rejected it 422 (structural; don't retry).
    ``done_ids`` also re-seeds from the JSONL cache, so a crawl resumes correctly
    even if the sidecar is lost. Thread-safe (the fetch pool records concurrently).
    """

    def __init__(self, out_path: Path):
        self.out_path = out_path
        self.state_path = out_path.with_suffix(out_path.suffix + ".state.json")
        self.done_ids: set[int] = set()
        self.gap_ids: set[int] = set()
        self.bad_ids: set[int] = set()
        self._lock = threading.Lock()
        self._fh = out_path.open("a", encoding="utf-8")
        self._load()

    def _load(self) -> None:
        if self.out_path.exists():
            # Stream line-by-line — the full cache is ~1 GB, never read whole.
            with self.out_path.open(encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                        cid = rec.get("companyId")
                        if cid is not None:
                            self.done_ids.add(int(cid))
                    except (json.JSONDecodeError, ValueError, TypeError):
                        continue
        if self.state_path.exists():
            try:
                s = json.loads(self.state_path.read_text(encoding="utf-8"))
                self.done_ids.update(int(i) for i in s.get("done_ids", []))
                self.gap_ids.update(int(i) for i in s.get("gap_ids", []))
                self.bad_ids.update(int(i) for i in s.get("bad_ids", []))
            except (json.JSONDecodeError, ValueError, TypeError):
                pass

    def seen(self, cid: int) -> bool:
        """True if ``cid`` was already resolved on a prior run (any terminal outcome)."""
        return cid in self.done_ids or cid in self.gap_ids or cid in self.bad_ids

    def cache(self, record: dict[str, Any]) -> None:
        """Append one raw record to the JSONL cache (flushed for crash-safety)."""
        with self._lock:
            self._fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._fh.flush()

    def save(self) -> None:
        with self._lock:
            self.state_path.write_text(
                json.dumps(
                    {
                        "done_ids": sorted(self.done_ids),
                        "gap_ids": sorted(self.gap_ids),
                        "bad_ids": sorted(self.bad_ids),
                    }
                ),
                encoding="utf-8",
            )

    def close(self) -> None:
        with self._lock:
            self._fh.close()


class FetchResult:
    """Outcome of one ``GET /company/{id}``.

    ``kind`` is one of: ``record`` (``data`` present), ``gap`` (200-null / 400 — no
    such company), ``retry`` (5xx / network / timeout — leave un-checkpointed),
    ``rate_limited`` (429; ``retry_after`` seconds if the server gave one).
    """

    __slots__ = ("kind", "data", "retry_after")

    def __init__(
        self, kind: str, data: dict[str, Any] | None = None, retry_after: float = 0.0
    ):
        self.kind = kind
        self.data = data
        self.retry_after = retry_after


class RequestsFetcher:
    """HTTP transport for the OCR JSON API via a ``requests`` session."""

    def __init__(self, timeout: int = 45, base: str = API_BASE):
        import requests

        self._base = base.rstrip("/")
        self._s = requests.Session()
        self._s.headers.update({"User-Agent": UA, "Accept": "application/json"})
        self._timeout = timeout

    def get_company(self, cid: int) -> FetchResult:
        try:
            r = self._s.get(f"{self._base}/company/{cid}", timeout=self._timeout)
        except Exception:  # noqa: BLE001 — network/timeout → retryable.
            return FetchResult("retry")
        if r.status_code == 429:
            ra = r.headers.get("Retry-After")
            try:
                retry_after = float(ra) if ra else 0.0
            except ValueError:
                retry_after = 0.0
            return FetchResult("rate_limited", retry_after=retry_after)
        if r.status_code >= 500:
            return FetchResult("retry")
        if r.status_code == 400:
            # OCR returns 400 for an id past the end / never-allocated → a gap.
            return FetchResult("gap")
        if not r.ok:
            return FetchResult("retry")
        try:
            data = r.json().get("data")
        except ValueError:
            return FetchResult("retry")
        if not data:
            return FetchResult("gap")  # 200 with null data — a hole in the id space.
        return FetchResult("record", data=data)

    def close(self) -> None:
        self._s.close()


class EntityApiError(Exception):
    """An entity POST failed in a retryable way (5xx / network)."""


class EntityApiClient:
    """POSTs an entity authoring payload to ``<api-base>/api/entities``.

    ``post(payload)`` returns a status token: ``created`` (201), ``exists`` (409 —
    already published, treated as success/skip), or raises :class:`EntityApiError`
    on a retryable failure. A 422 raises :class:`EntityValidationError` so the
    caller records the id as ``bad`` and never retries it.
    """

    def __init__(
        self,
        api_base: str,
        token: str | None,
        timeout: int = 60,
        basic_auth: tuple[str, str] | None = None,
    ):
        import requests

        self.url = api_base.rstrip("/") + "/api/entities"
        self._s = requests.Session()
        self._s.headers.update({"Accept": "application/json"})
        if token:
            self._s.headers["Authorization"] = f"Bearer {token}"
        elif basic_auth:
            # Local DEV_AUTH accepts HTTP Basic (a seeded Caseworker / superuser)
            # so an end-to-end run needs no Zitadel. Never used in prod (Bearer).
            self._s.auth = basic_auth
        self._timeout = timeout

    def post(self, payload: dict[str, Any]) -> str:
        try:
            r = self._s.post(self.url, json=payload, timeout=self._timeout)
        except Exception as e:  # noqa: BLE001 — network → retryable.
            raise EntityApiError(str(e)[:200]) from e
        if r.status_code in (200, 201):
            return "created"
        if r.status_code == 409:
            return "exists"  # duplicate @id — already published; skip idempotently.
        if r.status_code in (400, 422):
            raise EntityValidationError(f"{r.status_code}: {r.text[:300]}")
        raise EntityApiError(f"{r.status_code}: {r.text[:200]}")

    def close(self) -> None:
        self._s.close()


class EntityValidationError(Exception):
    """The entity API rejected a payload as invalid (400/422) — do not retry."""


def build_fetcher(timeout: int) -> RequestsFetcher:
    """Factory (a seam for tests to inject a fake fetch transport)."""
    return RequestsFetcher(timeout=timeout)


def build_entity_client(
    api_base: str,
    token: str | None,
    timeout: int,
    basic_auth: tuple[str, str] | None = None,
) -> EntityApiClient:
    """Factory (a seam for tests to inject a fake entity client)."""
    return EntityApiClient(api_base, token, timeout=timeout, basic_auth=basic_auth)


class OcrCrawler:
    """Walks the OCR company id range politely and (optionally) publishes entities."""

    def __init__(self, args, fetcher=None, entity_client=None):
        self.args = args
        self.cache_path = Path(args.cache)
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cp = Checkpoint(self.cache_path)
        self.fetcher = fetcher if fetcher is not None else build_fetcher(args.timeout)
        # Entity client — required unless --dry-run (scrape-to-cache only). --dry-run
        # is authoritative: it forces no client even if one was injected, so a
        # dry-run can NEVER post regardless of how the crawler was constructed.
        self.api: EntityApiClient | None = None
        if not args.dry_run:
            self.api = (
                entity_client
                if entity_client is not None
                else build_entity_client(
                    args.api_base,
                    args.token,
                    args.timeout,
                    basic_auth=getattr(args, "basic_auth", None),
                )
            )
        # Counters + the circuit-breaker's consecutive-error tally.
        self.n_requests = 0
        self.n_new = 0
        self.n_published = 0
        self.n_exists = 0
        self.n_skipped = 0
        self.n_gap = 0
        self.n_bad = 0
        self._consecutive_errors = 0
        self._lock = threading.Lock()

    # --- pacing / fetch -----------------------------------------------------

    def _pause(self, factor: float = 1.0) -> None:
        base = self.args.delay * factor
        if base > 0:
            time.sleep(base + random.uniform(0, base * 0.5))

    def _fetch_with_retry(self, cid: int) -> FetchResult:
        """Fetch one id, retrying 5xx/network with exponential backoff and honoring
        429 ``Retry-After``. Returns the terminal :class:`FetchResult` (``record`` /
        ``gap``), or a ``retry`` result if it exhausted attempts (id left for a
        later run).
        """
        for attempt in range(1, self.args.retries + 1):
            self._pause()
            res = self.fetcher.get_company(cid)
            if res.kind in ("record", "gap"):
                self._note_ok()
                return res
            if res.kind == "rate_limited":
                self._note_error()
                backoff = res.retry_after or min(60.0, 5.0 * attempt)
                print(
                    f"  ~ 429 for id={cid}; backing off {backoff:.0f}s", file=sys.stderr
                )
                time.sleep(backoff)
                continue
            # retry (5xx / network)
            self._note_error()
            if attempt < self.args.retries:
                time.sleep(min(60.0, 2.0**attempt))
        return FetchResult("retry")

    def _note_ok(self) -> None:
        with self._lock:
            self._consecutive_errors = 0

    def _note_error(self) -> None:
        with self._lock:
            self._consecutive_errors += 1

    def _circuit_open(self) -> bool:
        """True if too many consecutive server errors — pause to let OCR recover."""
        with self._lock:
            return self._consecutive_errors >= self.args.circuit_breaker

    # --- main walk ----------------------------------------------------------

    def crawl(self) -> None:
        try:
            if self.args.from_cache:
                self._ingest_cache()
            else:
                self._walk()
        finally:
            self.cp.save()
            self.cp.close()
            self.fetcher.close()
            if self.api is not None:
                self.api.close()

    def _walk(self) -> None:
        lo, hi = self.args.id_min, (self.args.id_max or DEFAULT_MAX_ID)
        mode = (
            "dry-run (cache only)"
            if self.args.dry_run
            else f"→ {self.args.api_base}/api/entities"
        )
        print(
            f"OCR crawl ids {lo}..{hi} (concurrency={self.args.concurrency}, "
            f"delay={self.args.delay}s); publish {mode}",
            file=sys.stderr,
        )
        todo = [cid for cid in range(lo, hi + 1) if not self.cp.seen(cid)]
        print(
            f"  {len(todo)} ids to fetch ({hi - lo + 1 - len(todo)} already done/gapped)",
            file=sys.stderr,
        )

        # A bounded thread pool keeps concurrency small and polite; each worker
        # fetches, then the (single-threaded) publish happens in the main loop as
        # results arrive so POSTs are serialized and ordered.
        with ThreadPoolExecutor(max_workers=self.args.concurrency) as pool:
            futures = {}
            it = iter(todo)
            # Prime the pool.
            for cid in _take(it, self.args.concurrency * 2):
                futures[pool.submit(self._fetch_with_retry, cid)] = cid
            while futures:
                for fut in as_completed(list(futures)):
                    cid = futures.pop(fut)
                    self._handle_fetch(cid, fut.result())
                    self.n_requests += 1
                    if self.n_requests % 500 == 0:
                        self.cp.save()
                        self._progress()
                    if self._hit_limit():
                        print(
                            "  reached --max-requests; stopping (resumable).",
                            file=sys.stderr,
                        )
                        return
                    if self._circuit_open():
                        print(
                            f"  ! {self._consecutive_errors} consecutive errors — "
                            f"pausing {self.args.circuit_pause}s (circuit breaker).",
                            file=sys.stderr,
                        )
                        time.sleep(self.args.circuit_pause)
                        self._note_ok()
                    # Refill one slot.
                    for nxt in _take(it, 1):
                        futures[pool.submit(self._fetch_with_retry, nxt)] = nxt
                    break  # re-enter as_completed with the refilled set
        self._progress(final=True)

    def _handle_fetch(self, cid: int, res: FetchResult) -> None:
        if res.kind == "gap":
            self.cp.gap_ids.add(cid)
            self.n_gap += 1
            return
        if res.kind != "record" or res.data is None:
            # retry — leave un-checkpointed so a later run picks it up.
            return
        record = res.data
        record["_scraped_at"] = _now_iso()
        self.cp.cache(record)  # cache EVERY status (full audit trail).
        self.n_new += 1
        self._publish(cid, record)

    def _publish(self, cid: int, record: dict[str, Any]) -> None:
        """Shape + POST a cached record. Non-publishable statuses are cached-only."""
        payload = ocr_company_to_jsonld(record)
        if payload is None:
            # DRAFT / REJECTED / unusable — cached but not published. Terminal.
            self.cp.done_ids.add(cid)
            self.n_skipped += 1
            return
        if self.api is None:  # dry-run — mark done without posting.
            self.cp.done_ids.add(cid)
            return
        try:
            status = self.api.post(payload)
        except EntityValidationError as e:
            print(f"  ! 422 for id={cid} ({payload.get('slug')}): {e}", file=sys.stderr)
            self.cp.bad_ids.add(cid)
            self.n_bad += 1
            return
        except EntityApiError as e:
            print(f"  ! entity POST retryable for id={cid}: {e}", file=sys.stderr)
            return  # leave un-checkpointed → retried next run.
        self.cp.done_ids.add(cid)
        if status == "exists":
            self.n_exists += 1
        else:
            self.n_published += 1

    def _ingest_cache(self) -> None:
        """POST every publishable cached record without re-crawling OCR.

        For re-driving ingestion from an existing ``companies.jsonl`` (e.g. after a
        platform API outage). 409s upsert-skip; already-done ids are skipped.
        """
        if self.api is None:
            print(
                "FATAL: --from-cache needs the API (remove --dry-run).", file=sys.stderr
            )
            return
        with self.cache_path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                cid = record.get("companyId")
                if cid is None or cid in self.cp.bad_ids:
                    continue
                self._publish(int(cid), record)
                self.n_requests += 1
                if self.n_requests % 500 == 0:
                    self.cp.save()
        self._progress(final=True)

    def _hit_limit(self) -> bool:
        return (
            bool(self.args.max_requests) and self.n_requests >= self.args.max_requests
        )

    def _progress(self, final: bool = False) -> None:
        tag = "done" if final else "…"
        print(
            f"  {tag}: fetched={self.n_requests} new={self.n_new} "
            f"published={self.n_published} exists={self.n_exists} "
            f"skipped={self.n_skipped} gaps={self.n_gap} bad={self.n_bad}",
            file=sys.stderr,
        )


def _take(it, n: int) -> list:
    """Pull up to ``n`` items from an iterator (drives the bounded pool refill)."""
    out = []
    for _ in range(n):
        try:
            out.append(next(it))
        except StopIteration:
            break
    return out


def _mint_bearer() -> str:
    """Mint the Caseworker bearer for the entity API from the OIDC client-
    credentials env (the ``sa-ingestion`` identity) so an in-cluster CronJob carries
    NO static token. A bare ``python -m`` run does not bootstrap Django, and
    ``resolve_service_bearer`` reads Django-settings fallbacks — so configure
    settings first (idempotent). Exits non-zero rather than POSTing unauthenticated.
    """
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    import django

    django.setup()
    from review.oidc_client_credentials import OIDCTokenError, resolve_service_bearer

    try:
        token = resolve_service_bearer()
    except OIDCTokenError as exc:
        print(f"FATAL: could not mint the entity-API bearer: {exc}", file=sys.stderr)
        raise SystemExit(2)
    if not token:
        print(
            "FATAL: no entity-API bearer. Set the OIDC client-credentials env "
            "(INGESTION_OIDC_CLIENT_ID/SECRET, else CASEWORK_OIDC_*), pass --token, "
            "or run with --dry-run.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return token


def add_crawler_arguments(ap: argparse.ArgumentParser) -> None:
    """Register the shared crawler flags (used by both __main__ and the command)."""
    ap.add_argument(
        "--cache",
        required=True,
        help="companies.jsonl cache path (appended; resume source)",
    )
    ap.add_argument(
        "--api-base",
        help="Platform base URL, e.g. https://api.jawafdehi.org (required unless --dry-run)",
    )
    ap.add_argument(
        "--token",
        help="Bearer for the entity API. Omit in-cluster: minted from the sa-ingestion OIDC env.",
    )
    ap.add_argument(
        "--basic-auth",
        dest="basic_auth_raw",
        metavar="USER:PASS",
        help="HTTP Basic credentials for a LOCAL DEV_AUTH server (a seeded Caseworker "
        "/ superuser) — a bearer-free path for local end-to-end runs. Never for prod.",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Scrape to the cache only; do NOT post to the API",
    )
    ap.add_argument(
        "--from-cache",
        action="store_true",
        help="Post the existing cache without re-crawling OCR",
    )
    ap.add_argument(
        "--id-min", type=int, default=1, help="Lowest company id (inclusive; default 1)"
    )
    ap.add_argument(
        "--id-max",
        type=int,
        default=0,
        help=f"Highest company id (inclusive; 0 = {DEFAULT_MAX_ID})",
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
        help="Base seconds between fetches per worker (default 0.7)",
    )
    ap.add_argument(
        "--retries",
        type=int,
        default=4,
        help="Fetch attempts per id on 5xx/network (default 4)",
    )
    ap.add_argument(
        "--timeout", type=int, default=45, help="HTTP timeout seconds (default 45)"
    )
    ap.add_argument(
        "--max-requests",
        type=int,
        default=0,
        help="Stop after N fetched ids this run (0=all; resumable)",
    )
    ap.add_argument(
        "--circuit-breaker",
        type=int,
        default=25,
        help="Pause after N consecutive server errors (default 25)",
    )
    ap.add_argument(
        "--circuit-pause",
        type=float,
        default=120.0,
        help="Circuit-breaker pause seconds (default 120)",
    )


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Crawl company.ocr.gov.np and POST company entities to /api/entities."
    )
    add_crawler_arguments(ap)
    args = ap.parse_args(argv)
    if not args.dry_run and not args.from_cache and not args.api_base:
        ap.error("--api-base is required unless --dry-run is set.")
    if args.from_cache and not args.api_base:
        ap.error("--from-cache requires --api-base (it posts).")
    # Parse --basic-auth "user:pass" into the tuple the client expects.
    args.basic_auth = None
    if args.basic_auth_raw:
        if ":" not in args.basic_auth_raw:
            ap.error("--basic-auth must be USER:PASS")
        user, _, password = args.basic_auth_raw.partition(":")
        args.basic_auth = (user, password)
    # Self-mint the bearer for a write run with no explicit --token — UNLESS
    # Basic-auth credentials were supplied (the local, bearer-free path).
    if not args.dry_run and not args.token and not args.basic_auth:
        args.token = _mint_bearer()
    OcrCrawler(args).crawl()


if __name__ == "__main__":
    main()
