"""Thin client for the Jawafdehi (JDS) public API.

Used in two places:
  1. The review pipeline's source converter downloads source artifacts (PDFs)
     for likhit/MarkItDown conversion (``download_source_file``). This works for
     both the local and remote case providers, because converted markdown is
     produced from the public source URLs either way.
  2. The ``seed_jawafdehi`` management command pulls cases / sources / entities
     from a remote JDS server to populate the LOCAL database, after which the
     review system runs fully offline.
"""

import time

import requests
from django.conf import settings

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36 CaseworkReview/1.0"
)

# Status codes worth retrying: rate limiting + transient server errors.
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


class JdsError(Exception):
    pass


def _base():
    return getattr(
        settings, "JAWAFDEHI_API_BASE", "https://portal.jawafdehi.org/api"
    ).rstrip("/")


def _token():
    return getattr(settings, "JAWAFDEHI_API_TOKEN", "") or ""


def _headers(auth=True):
    h = {"User-Agent": UA, "Accept": "application/json"}
    if auth and _token():
        h["Authorization"] = f"Token {_token()}"
    return h


def _retry_after_seconds(response, attempt, base_delay):
    """Seconds to wait before the next attempt.

    Honor the server's ``Retry-After`` header when present (the API tells us how
    long to back off); otherwise use exponential backoff (base_delay * 2**attempt)
    capped at 60s.
    """
    header = response.headers.get("Retry-After") if response is not None else None
    if header:
        try:
            return min(float(header), 60.0)
        except ValueError:
            pass
    return min(base_delay * (2**attempt), 60.0)


def _get(url, *, params=None, timeout=60, auth=True):
    """GET with retry/backoff on rate-limit (429) and transient 5xx errors.

    Max attempts and base backoff are configurable via settings
    ``JDS_MAX_RETRIES`` (default 5) and ``JDS_RETRY_BASE_DELAY`` (default 1.0s).
    Raises ``JdsError`` if every attempt is rate-limited/5xx.
    """
    max_retries = int(getattr(settings, "JDS_MAX_RETRIES", 5))
    base_delay = float(getattr(settings, "JDS_RETRY_BASE_DELAY", 1.0))
    last = None
    for attempt in range(max_retries + 1):
        r = requests.get(
            url, headers=_headers(auth=auth), params=params, timeout=timeout
        )
        if r.status_code not in _RETRY_STATUSES:
            return r
        last = r
        if attempt < max_retries:
            time.sleep(_retry_after_seconds(r, attempt, base_delay))
    # Exhausted retries — return the last (still-failing) response so the caller
    # raises its normal HTTP error with the real status code.
    return last


def get_case(slug, timeout=30):
    """Fetch the full case detail object for a slug from the remote JDS API."""
    url = f"{_base()}/cases/{slug}/"
    r = _get(url, timeout=timeout)
    if r.status_code == 404:
        raise JdsError(f"Case '{slug}' not found (404).")
    if r.status_code != 200:
        raise JdsError(f"JDS case fetch failed: HTTP {r.status_code}")
    return r.json()


def iter_paginated(path, params=None, timeout=60):
    """Yield every item across a paginated DRF list endpoint (``path`` under base)."""
    url = f"{_base()}/{path.lstrip('/')}"
    params = dict(params or {})
    while url:
        r = _get(url, params=params, timeout=timeout)
        if r.status_code != 200:
            raise JdsError(f"JDS list fetch failed for {url}: HTTP {r.status_code}")
        data = r.json()
        if isinstance(data, list):
            for item in data:
                yield item
            return
        for item in data.get("results", []):
            yield item
        url = data.get("next")
        params = {}  # next already carries the query string


def extract_sources(case):
    """Return the unique source objects referenced by a case's evidence.

    Each evidence item carries a nested `source` dict (title, source_type,
    url[]) and a `source_id`. We dedupe by source_id.
    """
    sources = {}
    for ev in case.get("evidence", []) or []:
        sid = ev.get("source_id")
        src = ev.get("source") or {}
        if not sid and not src:
            continue
        key = sid or src.get("title")
        if key in sources:
            continue
        # Prefer the new role-tagged `urls` (list of {link, role}); fall back to
        # the deprecated plain `url` string list. We keep BOTH: `url` (strings)
        # for conversion/download, and `urls` (with roles) so callers can tell
        # whether a MARKDOWN link already exists.
        urls = src.get("urls") or []
        url_strings = src.get("url", []) or []
        if not url_strings and urls:
            url_strings = [
                u.get("link") for u in urls if isinstance(u, dict) and u.get("link")
            ]
        # If a MARKDOWN link is already attached, surface its text as markdown.
        existing_md_link = next(
            (
                u.get("link")
                for u in urls
                if isinstance(u, dict) and u.get("role") == "MARKDOWN"
            ),
            None,
        )
        sources[key] = {
            "source_id": sid,
            "title": src.get("title", ""),
            "source_type": src.get("source_type", ""),
            "url": url_strings,
            "urls": urls,
            "evidence_description": ev.get("description", ""),
            # markdown attached on the source already? (future-proof)
            "markdown": src.get("markdown") or ev.get("markdown"),
            "markdown_url": existing_md_link,
        }
    return list(sources.values())


def download_source_file(url, timeout=60):
    """Download a source artifact (usually a PDF) and return (bytes, content_type)."""
    r = requests.get(url, headers={"User-Agent": UA}, timeout=timeout)
    if r.status_code != 200:
        raise JdsError(f"Source download failed for {url}: HTTP {r.status_code}")
    return r.content, r.headers.get("Content-Type", "")
