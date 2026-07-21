"""HTTP fetcher for the court-portal crawlers.

Extracted from the ``scrape_courtcases`` command so both that command and the
``court_scrape`` job handler share one fetch + decode path. ``Fetcher`` is a
callable: ``fetch(url, data=None) -> html`` — POST when ``data`` is given, else
GET.
"""

from __future__ import annotations

_UA = "Mozilla/5.0 (X11; Linux x86_64) Jawafdehi-courts-crawler"


def decode(resp) -> str:
    """Body as text, forcing UTF-8 when the portal omits the charset.

    supremecourt.gov.np serves UTF-8 Devanagari but sends ``Content-Type:
    text/html`` with NO charset, so ``requests`` falls back to ISO-8859-1 and
    mojibakes every page (the parsers then see zero Devanagari). Honor an
    explicit header charset when present; otherwise decode as UTF-8 —
    ``utf-8-sig`` also strips the BOM some endpoints emit.
    """
    if "charset=" not in resp.headers.get("content-type", "").lower():
        resp.encoding = "utf-8-sig"
    return resp.text


class Fetcher:
    """``fetch(url, data=None) -> html``: POST when ``data`` is given, else GET."""

    def __init__(self, timeout: int = 60):
        import requests

        self._s = requests.Session()
        self._s.headers.update({
            "User-Agent": _UA,
            "Referer": "https://supremecourt.gov.np/",
            "Origin": "https://supremecourt.gov.np",
        })
        self._timeout = timeout

    def __call__(self, url, data=None):
        resp = (
            self._s.post(url, data=data, timeout=self._timeout)
            if data is not None
            else self._s.get(url, timeout=self._timeout)
        )
        resp.raise_for_status()
        return decode(resp)
