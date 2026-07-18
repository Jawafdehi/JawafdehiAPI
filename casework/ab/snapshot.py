"""Read-only production snapshot for the A/B sample. NEVER writes to production."""
import json
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path

PROD_BASE = "https://api.jawafdehi.org"
BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
SAMPLE_FISCAL_YEARS = ("080", "081")
GOLDEN_FIELDS = ("bigo", "tags", "timeline", "key_allegations", "missing_details")


def case_matches_fiscal_years(case, fiscal_years=SAMPLE_FISCAL_YEARS):
    """True when any court_cases IRI names one of the fiscal years.

    The canonical IRI is https://<authority>/courtcase/<court>/<number> and it
    LOWERCASES the number (075-wf-0005). Matching case-sensitively silently
    selects zero cases, which is indistinguishable from "no work to do".
    """
    joined = " ".join(str(r) for r in (case.get("court_cases") or [])).lower()
    return any(f"{fy}-cr" in joined for fy in fiscal_years)


def select_sample_cases(cases, fiscal_years=SAMPLE_FISCAL_YEARS):
    return [c for c in cases if case_matches_fiscal_years(c, fiscal_years)]


def extract_golden(cases):
    """June's shipped values, keyed by slug — Arm G."""
    return {c["slug"]: {f: c.get(f) for f in GOLDEN_FIELDS} for c in cases}


def _get(path, token, base=PROD_BASE, timeout=60, attempts=3):
    req = urllib.request.Request(
        base + path,
        headers={
            "Authorization": f"Bearer {token}",
            "User-Agent": BROWSER_UA,
            "Accept": "application/json",
        },
    )
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except Exception:
            if attempt == attempts - 1:
                raise
            time.sleep(2)


def iter_all_cases(token, base=PROD_BASE, page_size=100, max_pages=40):
    page = 1
    while page <= max_pages:
        data = _get(f"/api/cases/?page_size={page_size}&page={page}", token, base)
        for case in data.get("results", []):
            yield case
        if not data.get("next"):
            return
        page += 1


def snapshot_sample(token, out_dir, base=PROD_BASE):
    """Snapshot the sample read-only. Returns run stats."""
    out = Path(out_dir)
    (out / "cases").mkdir(parents=True, exist_ok=True)
    summaries = select_sample_cases(list(iter_all_cases(token, base)))
    details = []
    for summary in summaries:
        slug = summary.get("slug")
        if not slug:
            continue
        detail = _get("/api/cases/" + urllib.parse.quote(slug) + "/", token, base)
        (out / "cases" / f"{slug}.json").write_text(
            json.dumps(detail, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        details.append(detail)
    (out / "golden.json").write_text(
        json.dumps(extract_golden(details), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {"selected": len(summaries), "snapshotted": len(details)}
