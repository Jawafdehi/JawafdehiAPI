"""Resumable crawler + API-ingest client for nkp.gov.np (Nepal Law Journal).

A data-acquisition tool (dev/ops, not request-path product). It crawls the NKP
precedent corpus (~10.5k decisions) and **POSTs each one to the platform's
material API** (``POST <api-base>/api/materials/``) — sourcing goes through the
API plane, not a direct-DB management command. The scraped ``decisions.jsonl``
is an on-disk CACHE (dedup + resume + audit), not the write path.

Run as a plain module (no management command — writes flow through HTTP):

    python -m materials.sourcing.nkp.crawl \\
        --api-base https://api.jawafdehi.org --token "$NGM_TOKEN" \\
        --cache /path/to/nkp/decisions.jsonl [--year 2082] [--delay 3.0]

    # scrape only, don't post (populate/refresh the cache):
    python -m materials.sourcing.nkp.crawl --cache … --dry-run

    # ingest an existing cache without re-scraping the site:
    python -m materials.sourcing.nkp.crawl --cache … --api-base … --token … --from-cache

Design notes learned the hard way:

- **Fetch transport:** default ``requests`` (plain HTTP works when the site is
  healthy). A ``playwright`` fetch fallback handles the F5 BIG-IP JS challenge the
  site serves during maintenance windows (all routes 302→``/web/``); that was a
  transient site-wide outage, not a persistent bot-wall.
- **Pagination:** month listings paginate via a ``per_page`` ROW OFFSET (≈20/page)
  and print their exact total ("<N> खोजी नतिजाहरु") — the crawler steps the offset
  and stops at the total (an early bug read only page 1 → capped at 20/month).
- **Encoding:** the pages are UTF-8 Devanagari but the server mis-declares charset,
  so the requests transport forces ``utf-8`` (else mojibake).
- **Resumable:** every posted decision id + finished (year, month) is checkpointed
  (``<cache>`` + ``<cache>.state.json``), so a re-run only fetches/posts what's
  missing. Posting is idempotent server-side (upsert by ``@id``).
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path
from typing import Any

from .parse import (
    BASE,
    extract_listing,
    parse_browse,
    parse_detail,
    parse_year_months,
)
from .shaper import nkp_decision_to_jsonld

#: NKP precedents publish from a single authoritative government portal.
NKP_AUTHORITY = "nkp.gov.np"
SUPREME_COURT_AUTHORITY = "supremecourt.gov.np"

# F5 serves a real page only to a browser-like client; keep a current desktop UA.
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/150.0.0.0 Safari/537.36"
)
# The /web/ redirect target is the Supreme Court portal shell, not a decision —
# reaching it means the F5 challenge bounced us. Treat as a soft failure + backoff.
_CHALLENGE_MARKERS = ("/web/", "/advance_search/web")


def _is_bounce(final_url: str) -> bool:
    """True if a request landed on the F5 portal-shell (challenge/outage).

    Tested against the FINAL (post-redirect) url only. A healthy page — including
    a ``/full_detail/{id}`` decision — never contains a challenge marker in its
    final url, so no path-based exemption is needed; adding one (e.g. "full_detail
    not in url") would wrongly suppress a genuine bounce of a detail request and
    silently drop the decision instead of backing off. Shared by both fetchers so
    the rule has a single definition.
    """
    return any(m in final_url for m in _CHALLENGE_MARKERS)


class Checkpoint:
    """Tracks scraped decision ids + finished (year, month) listings on disk.

    Sidecar ``<out>.state.json``. ``done_ids`` seeds from the existing JSONL too,
    so a crawl resumes correctly even if the sidecar is lost.
    """

    def __init__(self, out_path: Path):
        self.out_path = out_path
        self.state_path = out_path.with_suffix(out_path.suffix + ".state.json")
        self.done_ids: set[str] = set()
        self.done_listings: set[str] = set()  # "year:month"
        self._load()

    def _load(self) -> None:
        if self.out_path.exists():
            # Stream line-by-line — the full corpus cache is ~500 MB, so never
            # read it whole into memory.
            with self.out_path.open(encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                        if rec.get("detail_id"):
                            self.done_ids.add(str(rec["detail_id"]))
                    except json.JSONDecodeError:
                        continue
        if self.state_path.exists():
            try:
                s = json.loads(self.state_path.read_text(encoding="utf-8"))
                self.done_ids.update(str(i) for i in s.get("done_ids", []))
                self.done_listings.update(s.get("done_listings", []))
            except json.JSONDecodeError:
                pass

    def save(self) -> None:
        self.state_path.write_text(
            json.dumps(
                {
                    "done_ids": sorted(self.done_ids),
                    "done_listings": sorted(self.done_listings),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def listing_key(self, year: str, month: str) -> str:
        return f"{year}:{month}"


class RequestsFetcher:
    """HTTP transport via a ``requests`` session (no browser).

    Works when the site is serving normally (curl-reachable) — the F5 ``/web/``
    bounce during the 2026-07-09 outage turned out to be a maintenance state, not
    a persistent JS wall, so a plain session with a browser-like UA suffices.
    Follows redirects and treats a landing on the ``/web/`` portal shell as a
    soft failure (challenge/outage) so the caller can back off.
    """

    def __init__(self):
        import requests

        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA, "Accept-Language": "ne,en;q=0.8"})

    def warm(self) -> None:
        try:
            self.s.get(BASE + "/", timeout=30)
        except Exception:  # noqa: BLE001
            pass

    def get(self, url: str) -> tuple[str | None, bool]:
        """Return ``(html, bounced)``. ``bounced`` = hit the /web/ portal shell."""
        try:
            r = self.s.get(url, timeout=45, allow_redirects=True)
            if _is_bounce(r.url):
                return None, True
            # A transient/error status (5xx, 404, …) returns an error page, not
            # the decision. Treat it as a retryable failure — NOT content — so the
            # id is left un-checkpointed rather than cached as an empty stub and
            # skipped forever.
            if not r.ok:
                print(f"  ! http {r.status_code} for {url}", file=sys.stderr)
                return None, False
            # The pages are UTF-8 Devanagari but the server omits/mis-declares
            # charset, so requests guesses Latin-1 → mojibake. Force UTF-8 (lxml
            # in the parser also re-decodes, but decode here so the text is right
            # regardless of transport).
            r.encoding = "utf-8"
            return r.text, False
        except Exception as e:  # noqa: BLE001
            print(f"  ! http error {url}: {str(e)[:100]}", file=sys.stderr)
            return None, False

    def close(self) -> None:
        self.s.close()


class PlaywrightFetcher:
    """Browser transport (fallback) — solves a live F5 JS challenge if one returns."""

    def __init__(self, headful: bool):
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=not headful)
        self._ctx = self._browser.new_context(user_agent=UA, locale="ne-NP", ignore_https_errors=True)
        self.page = self._ctx.new_page()

    def warm(self) -> None:
        try:
            self.page.goto(BASE + "/", wait_until="domcontentloaded", timeout=45000)
        except Exception:  # noqa: BLE001
            pass

    def get(self, url: str) -> tuple[str | None, bool]:
        try:
            response = self.page.goto(url, wait_until="domcontentloaded", timeout=45000)
            bounced = _is_bounce(self.page.url)
            if bounced:
                return None, True
            # A transient/error status is an error page, not the decision — treat
            # it as a retryable failure so the id isn't cached as an empty stub.
            if response is not None and not response.ok:
                print(f"  ! nav {response.status} for {url}", file=sys.stderr)
                return None, False
            return self.page.content(), False
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            bounced = "ERR_TOO_MANY_REDIRECTS" in msg or "challenge" in msg.lower()
            if not bounced:
                print(f"  ! nav error {url}: {msg[:100]}", file=sys.stderr)
            return None, bounced

    def close(self) -> None:
        self._browser.close()
        self._pw.stop()


class MaterialApiClient:
    """Posts a shaped material to the platform material API (the ingest path).

    ``POST <api_base>/api/materials/`` with a ``{"material": <json-ld>,
    "material_type": ...}`` body, Bearer-authenticated (NGM-role gated). Upsert by
    ``@id`` is idempotent server-side, so re-posting is safe. Raises
    :class:`MaterialApiError` on a non-2xx so the caller can leave the id
    un-checkpointed and retry on the next run.
    """

    def __init__(self, api_base: str, token: str | None):
        import requests

        self.url = api_base.rstrip("/") + "/api/materials/"
        self.s = requests.Session()
        # The material API serves JSON-LD-shaped bodies through DRF's JSONRenderer
        # (media type application/json) — it does not register an
        # application/ld+json renderer, so asking only for ld+json fails content
        # negotiation with 406. Accept plain JSON (the wire shape is identical).
        self.s.headers.update({"Accept": "application/json"})
        if token:
            self.s.headers["Authorization"] = f"Bearer {token}"

    def post(self, doc: dict[str, Any], material_type: str) -> None:
        r = self.s.post(
            self.url,
            json={"material": doc, "material_type": material_type},
            timeout=60,
        )
        if r.status_code >= 300:
            raise MaterialApiError(f"{r.status_code}: {r.text[:200]}")

    def close(self) -> None:
        self.s.close()


class MaterialApiError(Exception):
    """A material POST returned a non-2xx status."""


class NkpCrawler:
    def __init__(self, args):
        self.args = args
        self.cache_path = Path(args.cache)
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cp = Checkpoint(self.cache_path)
        self.fh = self.cache_path.open("a", encoding="utf-8")
        self.new_count = 0
        self.posted_count = 0
        self.expected_total = 0
        self.fetcher = None
        # Ingest client — required unless --dry-run (scrape-to-cache only).
        self.api: MaterialApiClient | None = None
        if not args.dry_run:
            self.api = MaterialApiClient(args.api_base, args.token)

    # --- pacing / fetch -----------------------------------------------------

    def _pause(self, factor: float = 1.0) -> None:
        base = self.args.delay * factor
        time.sleep(base + random.uniform(0, base * 0.5))

    def _get(self, url: str, tries: int = 4) -> str | None:
        """Fetch HTML via the active transport, retrying past bounces w/ backoff."""
        for attempt in range(1, tries + 1):
            html, bounced = self.fetcher.get(url)
            if html is not None:
                return html
            if not bounced:
                if attempt == tries:
                    return None
                self._pause(attempt)
                continue
            backoff = min(60, 5 * attempt)
            print(f"  ~ bounce; re-warming, backoff {backoff}s", file=sys.stderr)
            time.sleep(backoff)
            self.fetcher.warm()
            self._pause(1.0)
        return None

    # --- crawl levels -------------------------------------------------------

    def crawl(self) -> None:
        try:
            if self.args.from_cache:
                self._ingest_cache()
            else:
                self._run()
        finally:
            if self.fetcher is not None:
                self.fetcher.close()
            if self.api is not None:
                self.api.close()
            self.fh.close()

    def _ingest_cache(self) -> None:
        """Post every cached decision to the API without re-scraping the site.

        For re-driving ingestion from an existing ``decisions.jsonl`` (e.g. after
        an API outage) — no fetch transport is opened. Posting is idempotent, so
        already-ingested ids simply upsert; the checkpoint still skips them.
        """
        if self.api is None:
            print("FATAL: --from-cache needs the API (remove --dry-run).", file=sys.stderr)
            return
        posted = 0
        with self.cache_path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    dec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if dec.get("empty"):
                    continue  # metadata-only stub, nothing to ingest
                if self._post_decision(dec):
                    posted += 1
        print(f"from-cache: posted {posted} decisions to the API.", file=sys.stderr)

    def _run(self) -> None:
        self.fetcher = (
            PlaywrightFetcher(self.args.headful)
            if self.args.transport == "playwright"
            else RequestsFetcher()
        )
        mode = "dry-run (cache only)" if self.args.dry_run else f"→ {self.args.api_base}"
        print(
            f"warming session (transport={self.args.transport}); ingest {mode}",
            file=sys.stderr,
        )
        self.fetcher.warm()

        browse_html = self._get(BASE + "/browse")
        if not browse_html:
            print("FATAL: could not load /browse (site down / blocked?)", file=sys.stderr)
            return
        years = parse_browse(browse_html)
        self.expected_total = sum(y["expected"] or 0 for y in years)
        years = self._select_years(years)
        print(
            f"/browse: {len(years)} years in scope; "
            f"{self.expected_total} decisions expected corpus-wide",
            file=sys.stderr,
        )

        for y in years:
            self._crawl_year(y["year"])
            self.cp.save()
            if self._hit_limit():
                break

        print(
            f"done: +{self.new_count} new decisions scraped, {self.posted_count} posted; "
            f"{len(self.cp.done_ids)} total done / {self.expected_total} expected",
            file=sys.stderr,
        )

    def _select_years(self, years: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Apply --year / --year-min / --year-max sharding filters.

        Non-numeric year buckets (e.g. the ``1111`` स्वर्ण edition) are kept only
        when no range filter is set, so a numeric shard range doesn't silently
        swallow or drop them.
        """
        if self.args.year:
            return [y for y in years if y["year"] == str(self.args.year)]
        lo, hi = self.args.year_min, self.args.year_max
        if lo is None and hi is None:
            return years
        out = []
        for y in years:
            try:
                yr = int(y["year"])
            except ValueError:
                continue  # skip non-numeric buckets when a numeric range is set
            if (lo is None or yr >= lo) and (hi is None or yr <= hi):
                out.append(y)
        return out

    def _crawl_year(self, year: str) -> None:
        self._pause()
        html = self._get(f"{BASE}/browse_monthly/?Submit=Yes&year={year}")
        if not html:
            return
        months = parse_year_months(html)
        print(f"year {year}: {len(months)} months", file=sys.stderr)
        for month in months:
            key = self.cp.listing_key(year, month)
            if key in self.cp.done_listings:
                continue
            complete = self._crawl_listing(year, month)
            # Only checkpoint the month as DONE when its listing was fully walked
            # AND every decision was scraped. Marking it done after a partial walk
            # (a fetch failure on page 2+, or a dropped detail) would make a resume
            # skip the month wholesale and permanently lose the un-fetched rows.
            if complete:
                self.cp.done_listings.add(key)
            self.cp.save()
            if self._hit_limit():
                return

    def _crawl_listing(self, year: str, month: str) -> bool:
        """Walk ALL pages of a month listing (per_page row-offset) and scrape.

        Pagination is a ``per_page`` ROW OFFSET (page 1 = none, page 2 =
        ``per_page=<page_size>``, page 3 = ``per_page=2*page_size`` …). The stride
        is the number of result rows page 1 returns (the site's page size), taken
        from actual data rather than the DOM order of pager links. We stop when
        we've collected the page's stated ``total``, or a page returns no NEW
        result ids (empty over-run — the sidebar past the last page, which
        ``extract_listing`` excludes), or a fetch fails.

        Returns ``True`` only if the month is fully done: every listing page was
        fetched, the collected count matched the page's stated ``total`` (when
        known), and every decision was successfully scraped. A ``False`` return
        leaves the month un-checkpointed so a later run retries it.
        """
        seen_ids: list[str] = []
        total: int | None = None
        stride = 0
        base = f"{BASE}/advance_search/?Submit=Yes&year={year}&month={month}"
        offset = 0
        listing_complete = True
        max_pages = 100  # hard stop (2000 results/month is far beyond any real month)
        for _ in range(max_pages):
            url = base if offset == 0 else f"{base}&per_page={offset}"
            self._pause()
            html = self._get(url)
            if not html:
                listing_complete = False  # fetch failed mid-walk — not fully listed
                break
            listing = extract_listing(html, url)
            if total is None:
                total = listing["total"]
            new = [d for d in listing["detail_ids"] if d not in seen_ids]
            if not new:
                # Empty / over-run page → done. Assumes an over-run offset renders
                # only the sidebar (zero result rows), which extract_listing
                # excludes. If the site ever CLAMPED an out-of-range offset and
                # re-served page 1, this would stop early — but the total-vs-collected
                # check below catches that shortfall (marks the month incomplete →
                # retried on resume), so it can't silently truncate.
                break
            seen_ids.extend(new)
            # Stride = the first (full) page's result count — the true page size,
            # derived from data not pager-link order.
            if stride == 0:
                stride = len(listing["detail_ids"]) or (listing["page_size"] or 20)
            if total is not None and len(seen_ids) >= total:
                break
            offset += stride

        complete = listing_complete
        if total is not None and len(seen_ids) != total:
            print(
                f"  listing {year}/{month}: {len(seen_ids)} decisions  "
                f"⚠ page states total={total} (incomplete — will retry on resume)",
                file=sys.stderr,
            )
            complete = False
        else:
            print(f"  listing {year}/{month}: {len(seen_ids)} decisions", file=sys.stderr)

        for did in seen_ids:
            if did in self.cp.done_ids:
                continue
            scraped = self._crawl_detail(did)
            if not scraped:
                complete = False  # a decision we couldn't scrape — retry the month
            if self._hit_limit():
                return False  # stopped early by --max-decisions; month not finished
        return complete

    def _crawl_detail(self, detail_id: str) -> bool:
        """Fetch → cache → POST one decision. Returns True iff it was fully handled
        (so the caller can tell a complete month from one with dropped/failed rows).

        Steps: fetch the page; POST it to the material API (unless ``--dry-run``);
        cache to the JSONL and checkpoint the id only after a successful post. A
        fetch failure/bounce or an API error returns False → the id is neither
        cached nor checkpointed and is retried on the next run (posting is
        idempotent). A page with no content column is a genuine empty decision (a
        handful exist site-side): it is cached as a metadata-only stub and counted
        done (not posted, nothing to ingest) rather than retried forever.
        """
        self._pause()
        url = f"{BASE}/full_detail/{detail_id}"
        html = self._get(url)
        if not html:
            return False  # fetch failed / bounced — retryable, do not mark done
        item = parse_detail(html, detail_id, url)
        empty = item is None
        if empty:
            print(f"  ! no content for full_detail/{detail_id} (cached as empty)", file=sys.stderr)
            item = {"detail_id": detail_id, "source_url": url, "empty": True}
        item["scraped_at"] = _now_iso()

        # An empty decision has nothing to POST: cache the metadata-only stub and
        # mark it done (a handful exist site-side; retrying forever is pointless).
        if empty:
            self._cache(item)
            self.cp.done_ids.add(detail_id)
            return True

        # Cache only AFTER a successful POST. ``Checkpoint._load`` seeds
        # ``done_ids`` from the cached JSONL, so a row written before a failed POST
        # would be treated as done on the next run and its ingestion never retried.
        # Posting is idempotent by ``@id``, so re-fetch-and-post on resume is safe.
        if not self._post_decision(item):
            return False  # API failure — retry the id (and the month) next run
        self._cache(item)
        self.cp.done_ids.add(detail_id)
        return True

    def _cache(self, item: dict[str, Any]) -> None:
        """Append one scraped item to the JSONL cache (flushed for crash-safety)."""
        self.fh.write(json.dumps(item, ensure_ascii=False) + "\n")
        self.fh.flush()
        self.new_count += 1

    def _post_decision(self, decision: dict[str, Any]) -> bool:
        """Shape a cached decision and POST it to the material API.

        ``--dry-run`` skips the POST (scrape-to-cache only). Returns True on a
        successful post (or dry-run), False on an API error (left for retry).
        """
        if self.api is None:  # dry-run
            return True
        doc, material_type = nkp_decision_to_jsonld(decision)
        try:
            self.api.post(doc, material_type)
        except MaterialApiError as e:
            print(f"  ! API post failed for {decision.get('detail_id')}: {e}", file=sys.stderr)
            return False
        self.posted_count += 1
        return True

    def _hit_limit(self) -> bool:
        return bool(self.args.max_decisions) and self.new_count >= self.args.max_decisions


def _now_iso() -> str:
    # Kathmandu-naive scraped_at (stdlib zoneinfo — no pytz dep).
    import datetime
    from zoneinfo import ZoneInfo

    return datetime.datetime.now(ZoneInfo("Asia/Kathmandu")).replace(tzinfo=None).isoformat()


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Crawl nkp.gov.np precedents and POST them to the material API."
    )
    ap.add_argument("--cache", required=True, help="decisions.jsonl cache path (appended; resume source)")
    ap.add_argument("--api-base", help="Platform base URL, e.g. https://api.jawafdehi.org (required unless --dry-run)")
    ap.add_argument("--token", help="Bearer token for the NGM-role-gated material API")
    ap.add_argument("--dry-run", action="store_true", help="Scrape to the cache only; do NOT post to the API")
    ap.add_argument("--from-cache", action="store_true", help="Post the existing cache to the API without re-scraping")
    ap.add_argument("--year", help="Limit to one BS year (default: whole corpus)")
    ap.add_argument("--year-min", type=int, default=None, help="Shard: lowest BS year (inclusive)")
    ap.add_argument("--year-max", type=int, default=None, help="Shard: highest BS year (inclusive)")
    ap.add_argument("--delay", type=float, default=3.0, help="Base seconds between actions (default 3)")
    ap.add_argument(
        "--transport", choices=["requests", "playwright"], default="requests",
        help="Fetch via a plain HTTP session (default) or a real browser "
        "(only needed if the F5 JS challenge returns).",
    )
    ap.add_argument("--headful", action="store_true", help="Show the browser (playwright transport only)")
    ap.add_argument("--max-decisions", type=int, default=0, help="Stop after N new decisions (0=all)")
    args = ap.parse_args(argv)
    if not args.dry_run and not args.api_base:
        ap.error("--api-base is required unless --dry-run is set.")
    NkpCrawler(args).crawl()


if __name__ == "__main__":
    main()
