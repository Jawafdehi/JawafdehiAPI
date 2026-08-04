"""Fake Special-Court HTML + fetcher shared by the crawl and job-scrape tests.

`test_scraper_crawl.py` (end-to-end orchestration) and `test_job_scrape.py`
(queue wiring) both drive the whole fetch → parse → upsert chain against a fake
fetcher, and carried byte-identical copies of these three HTML blobs plus
`fake_fetch`. Both files already say they mirror each other; this is that shared
setup in one place.

Sized deliberately small: one date, one bench, one case, one hearing -- enough
for the chain to produce exactly one `CourtCase`, which is what both callers
assert on. Growing it will change their expected counts.

NOT shared with `test_scraper_special.py`, which defines its own richer
`_BENCH_PAGE`/`_DETAIL`: those exercise the parsers directly (multiple rows,
edge-case cells) and are not interchangeable with these.
"""

BENCH_SELECT = """<select name="bench_type">
  <option value="">--</option><option value="1">इजलास १</option></select>"""

BENCH_PAGE = """<table width="100%" border="1">
  <tr><th>क्र</th><th>फाँट</th><th>मिति</th><th>मुद्दा</th><th>नं</th><th>वादी</th>
      <th>प्रतिवादी</th><th>मूल</th><th>कैफियत</th><th>स्थिति</th><th>किसिम</th></tr>
  <tr><td>१</td><td>क</td><td>२०८१/०५/१२</td><td>भ्रष्टाचार</td><td>०८२-CR-००१५</td>
      <td>नेपाल सरकार</td><td>क</td><td></td><td>-</td><td>पेशी</td><td>-</td></tr>
</table>"""

DETAIL = """<table width="100%" border="0" cellspacing="0" cellpadding="1">
  <tr><td class="caption">मुद्दा</td><td>भ्रष्टाचार</td></tr>
  <tr><td class="caption">मुद्दाको स्थिती</td><td>आदेश /फैसलाको किसिम</td></tr>
</table>
<table><tr><td>पेशी को विवरण</td></tr>
<tr><td><table class="utivtbl"><tr><th>x</th><th>x</th><th>x</th><th>x</th></tr>
  <tr><td>२०८२/०९/२८</td><td>राम</td><td>फैसला</td><td>सफाई</td></tr></table></td></tr></table>"""


def fake_fetch(url, data=None):
    """Route a scraper fetch to the blob its (url, mode) pair implies."""
    data = data or {}
    if data.get("mode") == "showbench":
        return BENCH_SELECT
    if "case_details" in url:
        return DETAIL
    if data.get("mode") == "show":
        return BENCH_PAGE
    return ""
