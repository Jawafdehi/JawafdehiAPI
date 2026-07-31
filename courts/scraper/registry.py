"""Per-court crawl specs: wire each court's portal fetch to its pure parser.

``run_crawl`` drives any object exposing ``LOOKBACK_DAYS``, ``court_ids(fetch)``,
``crawl_date(fetch, court_id, date_bs, nepali_date)`` and (optionally)
``crawl_detail(fetch, court_id, case_number)``. The Special module already
satisfies this; the others are thin adapters here so the agents' modules stay
pure (parse-only). ``fetch(url, data=None) -> html``: POST when ``data`` is given.

Fetch endpoints/params are ported from the retired ngm spiders. The parse + write
paths are unit/DB-tested; the live fetch is exercised only on a real run.
"""

from __future__ import annotations

from urllib.parse import urljoin

from django.utils import timezone

from courts.scraper import district, high, special, supreme
from courts.scraper.court_ids import DISTRICT_COURTS, HIGH_COURTS

_YEAR = 365


class _Supreme:
    LOOKBACK_DAYS = 15 * _YEAR
    LIST_URL = "https://supremecourt.gov.np/lic/sys.php?d=reports&f=weekly_suppli_public"
    DETAIL_URL = "https://supremecourt.gov.np/lic/sys.php?d=reports&f=case_details"

    def court_ids(self, fetch):
        return ["supreme"]

    def crawl_date(self, fetch, court_id, date_bs, nd):
        html = fetch(self.LIST_URL, data={
            "syy": str(nd.year), "smm": f"{nd.month:02d}", "sdd": f"{nd.day:02d}",
            "mode": "show", "yo": "1",
        })
        return supreme.parse_cause_list(html, date_bs=date_bs)

    def crawl_detail(self, fetch, court_id, case_number):
        """Two-stage: search by case number, then follow the result to the page.

        A ``regno`` search returns a RESULT LIST, not the detail page. Handing that
        list to ``parse_supreme_detail`` yields an entirely empty enrichment — which
        is why the Django enrichment path has never populated a single Supreme
        ``hearing_count``. Stage 2 follows the row's ``…&mode=view&caseno=<id>`` link,
        matched to ``case_number`` so a multi-row result can't enrich the wrong case.

        ``None`` means the court has no such docket. A response that isn't a search
        result at all raises ``UnexpectedPage`` rather than posing as one.
        """
        listing = fetch(self.DETAIL_URL, data={
            "syy": "", "smm": "", "sdd": "", "mode": "show", "list": "list",
            "regno": case_number, "tyy": "", "tmm": "", "tdd": "",
        })
        if not listing:
            return None
        href = supreme.parse_search_result_link(listing, case_number)
        if not href:
            return None  # "Total 0 Records Found" — no such docket
        # Joined against DETAIL_URL itself, so the two can never drift apart.
        html = fetch(urljoin(self.DETAIL_URL, href))
        return supreme.parse_supreme_detail(html) if html else None


class _District:
    LOOKBACK_DAYS = 10 * _YEAR
    LIST_URL = "https://supremecourt.gov.np/weekly_dainik/pesi/daily/{did}"
    DETAIL_URL = "https://supremecourt.gov.np/weekly_dainik/pesi/case_process_detail/{did}"
    _BY_CODE = {c["code_name"]: c for c in DISTRICT_COURTS}

    def court_ids(self, fetch):
        return [c["code_name"] for c in DISTRICT_COURTS]

    def _today_bs(self):
        from jawafdehi_shared.dates import ad_to_bs
        return ad_to_bs(timezone.localdate())

    def crawl_date(self, fetch, court_id, date_bs, nd):
        did = self._BY_CODE[court_id]["district_id"]
        html = fetch(self.LIST_URL.format(did=did), data={
            "todays_date": self._today_bs(), "pesi_date": date_bs, "submit": "खोज्नु होस्",
        })
        return district.parse_daily_list(html, court_identifier=court_id, date_bs=date_bs)

    def crawl_detail(self, fetch, court_id, case_number):
        from courts.scraper.text import roman_to_nepali_numerals
        did = self._BY_CODE[court_id]["district_id"]
        html = fetch(self.DETAIL_URL.format(did=did), data={
            "mudda_no": roman_to_nepali_numerals(case_number), "submit": "खोज्नु होस्",
        })
        return district.parse_district_detail(html) if html else None


class _High:
    LOOKBACK_DAYS = 10 * _YEAR
    BENCH_URL = "https://supremecourt.gov.np/court/{court}/bench_list?pesi_date={pesi}"
    DETAIL_URL = "https://supremecourt.gov.np/court/{court}/cause_list_detail"
    CASE_DETAIL_URL = "https://supremecourt.gov.np/court/{court}/case_details"

    def court_ids(self, fetch):
        return [c["identifier"] for c in HIGH_COURTS]

    def crawl_date(self, fetch, court_id, date_bs, nd):
        pesi = f"{nd.year:04d}%2F{nd.month:02d}%2F{nd.day:02d}"
        hearing_date = f"{nd.year:04d}{nd.month:02d}{nd.day:02d}"
        bench_html = fetch(self.BENCH_URL.format(court=court_id, pesi=pesi))
        rows: list = []
        for b in high.parse_bench_list(bench_html):
            page = fetch(self.DETAIL_URL.format(court=court_id), data={
                "bench_id": b["bench_id"], "bench_no": b["bench_no"],
                "hearing_date": hearing_date,
            })
            rows.extend(high.parse_bench_page(
                page, court_identifier=court_id, date_bs=date_bs,
                bench_id=b["bench_id"], bench_no=b["bench_no"], judge_name=b["judge_name"],
            ))
        return rows

    def crawl_detail(self, fetch, court_id, case_number):
        html = fetch(self.CASE_DETAIL_URL.format(court=court_id), data={"case_no": case_number})
        return high.parse_high_detail(html) if html else None


# court key → spec. Special is its own compliant module.
REGISTRY = {
    "special": special,
    "supreme": _Supreme(),
    "district": _District(),
    "high": _High(),
}


def resolve(name: str) -> list[str]:
    """Expand a --court value (a key or ``all``) to registry keys."""
    if name == "all":
        return list(REGISTRY)
    if name not in REGISTRY:
        raise KeyError(f"unknown court '{name}'; choose from {', '.join(REGISTRY)} or 'all'")
    return [name]
