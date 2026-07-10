"""Resumable crawler for nkp.gov.np (Nepal Law Journal) precedents.

A one-time / periodic data-acquisition tool (dev/ops, not request-path product).
It crawls the whole NKP precedent corpus (~10.5k decisions) into a JSONL file
that ``manage.py ingest_nkp_decisions`` then lands as ``precedent`` Materials.

Design notes learned the hard way:

- **Transport:** default ``requests`` (plain HTTP works when the site is healthy).
  A ``playwright`` transport is kept as a fallback for the F5 BIG-IP JS challenge
  the site serves during maintenance windows (all routes 302→``/web/``); that was
  a transient site-wide outage, not a persistent bot-wall.
- **Pagination:** month listings paginate via a ``per_page`` ROW OFFSET (≈20/page)
  and print their exact total ("<N> खोजी नतिजाहरु") — the crawler steps the offset
  and stops at the total (an early bug read only page 1 → capped at 20/month).
- **Encoding:** the pages are UTF-8 Devanagari but the server mis-declares charset,
  so the requests transport forces ``utf-8`` (else mojibake).
- **Resumable:** every scraped decision id + finished (year, month) is checkpointed
  (``<out>`` + ``<out>.state.json``), so a re-run only fetches what's missing.

Prefer the ``crawl_nkp`` management command; this module is also runnable directly.

Usage::

    python manage.py crawl_nkp --out /path/to/nkp/decisions.jsonl [--year 2082] \\
        [--year-min 2076 --year-max 2082] [--delay 3.0] [--transport requests]
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
            for line in self.out_path.read_text(encoding="utf-8").splitlines():
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
            self.page.goto(url, wait_until="domcontentloaded", timeout=45000)
            bounced = _is_bounce(self.page.url)
            return (None if bounced else self.page.content()), bounced
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            bounced = "ERR_TOO_MANY_REDIRECTS" in msg or "challenge" in msg.lower()
            if not bounced:
                print(f"  ! nav error {url}: {msg[:100]}", file=sys.stderr)
            return None, bounced

    def close(self) -> None:
        self._browser.close()
        self._pw.stop()


class NkpCrawler:
    def __init__(self, args):
        self.args = args
        self.out_path = Path(args.out)
        self.out_path.parent.mkdir(parents=True, exist_ok=True)
        self.cp = Checkpoint(self.out_path)
        self.fh = self.out_path.open("a", encoding="utf-8")
        self.new_count = 0
        self.expected_total = 0
        self.fetcher = None

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
        if self.args.transport == "playwright":
            self.fetcher = PlaywrightFetcher(self.args.headful)
        else:
            self.fetcher = RequestsFetcher()
        try:
            self._run()
        finally:
            self.fetcher.close()
            self.fh.close()

    def _run(self) -> None:
        print(f"warming session (transport={self.args.transport})…", file=sys.stderr)
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
            f"done: +{self.new_count} new decisions this run; "
            f"{len(self.cp.done_ids)} total banked / {self.expected_total} expected",
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
        """Fetch + parse one decision, append it, mark it done. Returns True if the
        decision was recorded (so the caller can tell a fully-scraped month from
        one with dropped detail fetches).

        A fetch failure/bounce (``html is None``) returns False → the month is not
        checkpointed and the id is retried on resume. A page that parses to no
        content column returns None from ``parse_detail``; that is a real but empty
        decision (a handful exist site-side), so it is recorded (with metadata-only
        via a minimal item) and counted done rather than retried forever.
        """
        self._pause()
        url = f"{BASE}/full_detail/{detail_id}"
        html = self._get(url)
        if not html:
            return False  # fetch failed / bounced — retryable, do not mark done
        item = parse_detail(html, detail_id, url)
        if item is None:
            # Page loaded but has no decision content column. Distinguish a genuine
            # empty decision (record it, move on) from a bounce shell that slipped
            # through: a real decision page carries the site chrome, a bounce does
            # not. We conservatively record a metadata-only stub so the id is not
            # re-fetched every resume, and log it for review.
            print(f"  ! no content for full_detail/{detail_id} (recorded as empty)", file=sys.stderr)
            item = {"detail_id": detail_id, "source_url": url, "empty": True}
        item["scraped_at"] = _now_iso()
        self.fh.write(json.dumps(item, ensure_ascii=False) + "\n")
        self.fh.flush()
        self.cp.done_ids.add(detail_id)
        self.new_count += 1
        return True

    def _hit_limit(self) -> bool:
        return bool(self.args.max_decisions) and self.new_count >= self.args.max_decisions


def _now_iso() -> str:
    # Kathmandu-naive scraped_at (stdlib zoneinfo — no pytz dep).
    import datetime
    from zoneinfo import ZoneInfo

    return datetime.datetime.now(ZoneInfo("Asia/Kathmandu")).replace(tzinfo=None).isoformat()


def main(argv=None):
    ap = argparse.ArgumentParser(description="Resumable Playwright crawler for nkp.gov.np")
    ap.add_argument("--out", required=True, help="decisions.jsonl path (appended; resume source)")
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
    NkpCrawler(args).crawl()


if __name__ == "__main__":
    main()
