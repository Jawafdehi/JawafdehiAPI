"""Refresh the PPMO blacklist (blacklisted firms) into the platform.

The recurring replacement for the retired ``ppmo_blacklist`` Scrapy spider
(archived ``Jawafdehi/ngm``). Walks the paginated blacklist table on
``old.ppmo.gov.np``, follows each firm's detail page for address/cause, and
upserts a ``BlacklistedFirm`` row (ngm DB) keyed on ``(firm_name,
blacklist_date_bs)``, filling in missing detail fields on re-runs.

Dry-run by default (fetch + parse, report counts); ``--write`` persists.

    manage.py scrape_ppmo_blacklist                 # dry-run recon
    manage.py scrape_ppmo_blacklist --write         # the CronJob run
    manage.py scrape_ppmo_blacklist --limit 20      # cap firms (smoke test)

The pure parse/shape half lives in ``courts.scraper.ppmo`` (unit-tested); this
command adds the live HTTP, the pagination walk, and the ORM upsert. The legacy
spider also wrote a per-firm JSON blob to R2 to feed the retired DocumentSource
index — intentionally dropped here (that index is being retired). ``nes_id`` is
left null; firm→NES-entity linking is a separate enrichment step.
"""

from __future__ import annotations

import time
from urllib.parse import urljoin

from django.core.management.base import BaseCommand

from courts.models import BlacklistedFirm
from courts.scraper import ppmo as P

#: Safety cap on the pagination walk (the blacklist table is only a few pages).
_MAX_PAGES = 100

#: CharField ceilings on ``BlacklistedFirm`` — scraped strings are clamped so an
#: over-long cell can never fail the insert (the court scrapers hit exactly this).
_MAX = {
    "firm_name": 500,
    "proprietor_name": 500,
    "address": 500,
    "recommending_office": 500,
    "duration": 100,
    "blacklist_date_bs": 20,
    "effective_until_bs": 20,
}

#: Detail fields back-filled onto an existing row only when it lacks them.
_FILL_FIELDS = (
    "address",
    "proprietor_name",
    "reason",
    "recommending_office",
    "effective_until_bs",
    "effective_until_ad",
    "blacklist_date_ad",
)


class BlacklistHttpClient:
    """Live transport for the PPMO blacklist: GET only, TLS-verify off (the old
    subdomain serves a bad cert — the retired spider set
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


def build_http_client(timeout: int) -> BlacklistHttpClient:
    """Factory (a seam for tests to inject a fake transport)."""
    return BlacklistHttpClient(timeout=timeout)


def _clamp(value, field: str):
    limit = _MAX.get(field)
    if value and limit and len(value) > limit:
        return value[:limit]
    return value


class Command(BaseCommand):
    help = "Refresh the PPMO blacklist (blacklisted firms) into the platform."

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit", type=int, default=0,
            help="max firms to process (0 = no limit; the whole table)",
        )
        parser.add_argument(
            "--max-pages", type=int, default=_MAX_PAGES,
            help=f"pagination safety cap (default {_MAX_PAGES})",
        )
        parser.add_argument(
            "--delay", type=float, default=1.0,
            help="seconds between HTTP requests (default 1.0)",
        )
        parser.add_argument("--timeout", type=int, default=60, help="HTTP timeout (s)")
        parser.add_argument(
            "--write", action="store_true",
            help="persist (default: dry-run — fetch + parse only)",
        )

    def handle(self, *args, **o):
        write = o["write"]
        limit = o["limit"]
        delay = max(0.0, o["delay"])
        client = build_http_client(o["timeout"])

        mode = "WRITE" if write else "DRY-RUN"
        self.stdout.write(f"scrape_ppmo_blacklist [{mode}] limit={limit or '∞'}")

        tally = {k: 0 for k in ("added", "updated", "unchanged", "skipped")}
        seen = 0
        for firm in self._walk(client, o["max_pages"], delay):
            if limit and seen >= limit:
                break
            seen += 1
            if not P.resolve_dates(firm):
                tally["skipped"] += 1
                self.stdout.write(
                    f"  skip (implausible BS date): {firm.firm_name!r} {firm.duration!r}"
                )
                continue
            tally[self._persist(firm, write)] += 1

        self.stdout.write("done: " + " ".join(f"{k}={v}" for k, v in tally.items()))

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
                        # don't persist a half-empty row.
                        continue
                    for key, value in detail.items():
                        setattr(firm, key, value)
                yield firm
            url = urljoin(url, next_href) if next_href else None
            if url and delay:
                time.sleep(delay)

    def _persist(self, firm, write: bool) -> str:
        """Upsert on ``(firm_name, blacklist_date_bs)``, back-filling missing
        detail on re-runs. Returns ``added`` | ``updated`` | ``unchanged``."""
        existing = (
            BlacklistedFirm.objects.using("ngm")
            .filter(firm_name=firm.firm_name, blacklist_date_bs=firm.blacklist_date_bs)
            .first()
        )

        if existing is None:
            if write:
                BlacklistedFirm.objects.using("ngm").create(
                    firm_name=_clamp(firm.firm_name, "firm_name"),
                    proprietor_name=_clamp(firm.proprietor_name, "proprietor_name"),
                    address=_clamp(firm.address, "address"),
                    blacklist_date_bs=_clamp(firm.blacklist_date_bs, "blacklist_date_bs"),
                    blacklist_date_ad=firm.blacklist_date_ad,
                    effective_until_bs=_clamp(firm.effective_until_bs, "effective_until_bs"),
                    effective_until_ad=firm.effective_until_ad,
                    duration=_clamp(firm.duration, "duration"),
                    reason=firm.reason,
                    recommending_office=_clamp(firm.recommending_office, "recommending_office"),
                )
            self.stdout.write(f"  add{'' if write else ' [dry-run]'}: {firm.firm_name}")
            return "added"

        changed = []
        for field in _FILL_FIELDS:
            new = getattr(firm, field)
            if new and not getattr(existing, field):
                setattr(existing, field, _clamp(new, field))
                changed.append(field)
        if not changed:
            return "unchanged"
        if write:
            existing.save(using="ngm", update_fields=[*changed, "updated_at"])
        self.stdout.write(
            f"  update{'' if write else ' [dry-run]'}: {firm.firm_name} ({', '.join(changed)})"
        )
        return "updated"
