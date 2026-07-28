"""Parse the PPMO (Public Procurement Monitoring Office) blacklist API.

The pure parse/shape half of the ``scrape_ppmo_blacklist`` command. PPMO rebuilt
its blacklist as a React/Yii2 SPA (``blacklist.ppmo.gov.np``) — the retired
spider's ``old.ppmo.gov.np`` HTML tables are dead. The public, unauthenticated
JSON endpoint returns EVERY blacklisted firm in one response, so this is a pure
JSON→row transform: no pagination, no detail-page walk.

Source dates are Gregorian (``start_date`` / ``end_date``); the corpus keys on
Bikram Sambat, so the BS natural-key + effective-until are derived via
``ad_to_bs``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from courts.scraper.text import normalize_whitespace
from jawafdehi_shared.dates import ad_to_bs

#: Public, unauthenticated feed — every blacklisted firm in one JSON response.
API_URL = "https://blacklist.ppmo.gov.np/api/info/company-list"

_TAG_RE = re.compile(r"<[^>]+>")
_HONORIFIC_RE = re.compile(r"^\s*श्री\s*")


@dataclass
class ParsedFirm:
    """One blacklisted firm, mapped from a company-list JSON row."""

    firm_name: str
    proprietor_name: str | None = None
    address: str | None = None
    reason: str | None = None
    recommending_office: str | None = None
    blacklist_date_bs: str | None = None
    effective_until_bs: str | None = None
    blacklist_date_ad: date | None = None
    effective_until_ad: date | None = None
    duration: str | None = None


def parse_company_list(payload: dict | list) -> list[ParsedFirm]:
    """Map the ``/api/info/company-list`` JSON to firm rows.

    Accepts the ``{"success", "data": [...]}`` envelope (or a bare list). A row
    is skipped when it lacks a company name, a parseable ``start_date``, or a
    derivable BS date (the ``(firm_name, blacklist_date_bs)`` natural key must be
    formable). ``start_date`` / ``end_date`` are AD (ISO); BS is derived.
    """
    items = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        return []

    firms: list[ParsedFirm] = []
    for row in items:
        if not isinstance(row, dict):
            continue
        name = normalize_whitespace(row.get("company_name"))
        start_ad = _parse_ad(row.get("start_date"))
        if not name or start_ad is None:
            continue
        blacklist_bs = ad_to_bs(start_ad)
        if not blacklist_bs:  # can't form the natural key
            continue
        end_ad = _parse_ad(row.get("end_date"))
        until_bs = ad_to_bs(end_ad) if end_ad else None
        firms.append(
            ParsedFirm(
                firm_name=name,
                proprietor_name=_clean(row.get("owner")),
                address=_clean(row.get("address")),
                reason=_strip_html(row.get("remark")),
                recommending_office=_clean(row.get("public_entity_name")),
                blacklist_date_bs=blacklist_bs,
                effective_until_bs=until_bs,
                blacklist_date_ad=start_ad,
                effective_until_ad=end_ad,
                duration=f"{blacklist_bs} to {until_bs}" if until_bs else blacklist_bs,
            )
        )
    return firms


def _parse_ad(value) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _clean(value) -> str | None:
    """Whitespace-collapse and drop a leading श्री honorific."""
    text = _HONORIFIC_RE.sub("", normalize_whitespace(value)).strip()
    return text or None


def _strip_html(value) -> str | None:
    """The ``remark`` (legal basis) is HTML; flatten to text."""
    text = normalize_whitespace(_TAG_RE.sub(" ", str(value or "")))
    return text or None


def to_payload(firm: ParsedFirm) -> dict:
    """Serialize a firm to the ``/ingestion/firms`` item shape (JSON-able).

    Dates become ISO strings; ``None`` detail fields are omitted so the idempotent
    upsert never clobbers a stored value with an explicit null.
    """
    payload = {
        "firm_name": firm.firm_name,
        "blacklist_date_bs": firm.blacklist_date_bs,
    }
    optional = {
        "proprietor_name": firm.proprietor_name,
        "address": firm.address,
        "duration": firm.duration,
        "reason": firm.reason,
        "recommending_office": firm.recommending_office,
        "effective_until_bs": firm.effective_until_bs,
        "blacklist_date_ad": firm.blacklist_date_ad.isoformat() if firm.blacklist_date_ad else None,
        "effective_until_ad": firm.effective_until_ad.isoformat() if firm.effective_until_ad else None,
    }
    payload.update({key: value for key, value in optional.items() if value})
    return payload
