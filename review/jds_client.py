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

import requests
from django.conf import settings

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36 CaseworkReview/1.0"
)


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


def get_case(slug, timeout=30):
    """Fetch the full case detail object for a slug from the remote JDS API."""
    url = f"{_base()}/cases/{slug}/"
    r = requests.get(url, headers=_headers(), timeout=timeout)
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
        r = requests.get(url, headers=_headers(), params=params, timeout=timeout)
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
        sources[key] = {
            "source_id": sid,
            "title": src.get("title", ""),
            "source_type": src.get("source_type", ""),
            "url": src.get("url", []) or [],
            "evidence_description": ev.get("description", ""),
            # markdown attached on the source already? (future-proof)
            "markdown": src.get("markdown") or ev.get("markdown"),
        }
    return list(sources.values())


def download_source_file(url, timeout=60):
    """Download a source artifact (usually a PDF) and return (bytes, content_type)."""
    r = requests.get(url, headers={"User-Agent": UA}, timeout=timeout)
    if r.status_code != 200:
        raise JdsError(f"Source download failed for {url}: HTTP {r.status_code}")
    return r.content, r.headers.get("Content-Type", "")
