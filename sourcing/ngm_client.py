"""Reusable client for the NGM (Nepal Governance Modernization) judicial DB.

Talks to the read-only NGM court-case endpoint
(``GET {JAWAFDEHI_API_BASE}/ngm/court_case/<court>:<case_number>``), which
returns a single court case with its hearings and entities in one call. This is
preferred over the raw ``/ngm/query_judicial`` SQL endpoint: it needs no SQL,
takes the ``court:case_number`` ref verbatim (the exact form stored in a
Jawafdehi case's ``court_cases`` field, e.g. ``"special:081-CR-0079"``), and
server-side normalizes the case number.

Intentionally generic so it can back several review rules and future checks
(e.g. verifying a case's start/end dates against ``registration_date_ad`` /
``verdict_date_ad`` on the returned record).
"""

import re
from urllib.parse import quote

import requests
from django.conf import settings


class NgmError(Exception):
    pass


class NgmNotFound(NgmError):
    """The court case ref was not found in NGM (HTTP 404)."""


def _base():
    return getattr(
        settings, "JAWAFDEHI_API_BASE", "https://portal.jawafdehi.org/api"
    ).rstrip("/")


def _token():
    return getattr(settings, "JAWAFDEHI_API_TOKEN", "") or ""


# Court refs come from case data (operator-entered), so validate strictly before
# they ever reach a URL: identifier is lowercase alphanumerics; case number is
# the NNN-XX-NNNN court-case form (alphanumerics + hyphens). This rejects path
# traversal / query-injection chars (/, .., ?, #, whitespace) up front.
_COURT_ID_RE = re.compile(r"^[a-z0-9]+$")
_CASE_NUMBER_RE = re.compile(r"^[A-Za-z0-9-]+$")


def parse_court_ref(ref):
    """Parse a ``"<court_identifier>:<case_number>"`` ref into a (court, number) tuple.

    Returns None if the ref is not in that shape or contains unsafe characters.
    """
    if not ref or ":" not in str(ref):
        return None
    court, _, number = str(ref).partition(":")
    court, number = court.strip(), number.strip()
    if not court or not number:
        return None
    if not _COURT_ID_RE.match(court) or not _CASE_NUMBER_RE.match(number):
        return None
    return court, number


def court_refs_for_case(case):
    """All ``"<court>:<case_number>"`` refs from a case's court_cases field."""
    refs = []
    for ref in case.get("court_cases") or []:
        if parse_court_ref(ref):
            refs.append(str(ref).strip())
    return refs


def get_court_case(case_ref, timeout=30):
    """Fetch one NGM court case (with hearings + entities) by ``court:number``.

    Returns the full record dict, or raises NgmNotFound (404) / NgmError.
    The endpoint is public (no auth required); we send the token if present.
    """
    parsed = parse_court_ref(case_ref)
    if not parsed:
        raise NgmError(
            f"Invalid court ref '{case_ref}'; expected '<court>:<case_number>'."
        )
    court, number = parsed

    headers = {"Accept": "application/json"}
    if _token():
        headers["Authorization"] = f"Token {_token()}"
    # Rebuild + percent-encode from the validated parts (never interpolate the
    # raw ref) so the path segment can't break out into the URL path/query.
    safe_ref = quote(f"{court}:{number}", safe="")
    url = f"{_base()}/ngm/court_case/{safe_ref}"
    try:
        r = requests.get(url, headers=headers, timeout=timeout)
    except requests.RequestException as e:
        raise NgmError(f"NGM request failed: {e}") from e
    if r.status_code == 404:
        raise NgmNotFound(f"Court case '{case_ref}' not found in NGM.")
    if r.status_code != 200:
        raise NgmError(f"NGM court_case HTTP {r.status_code}: {r.text[:300]}")
    return r.json()


def get_case_record(case_ref, timeout=30):
    """The court case record (dates, status, parties) without hearings/entities.

    Useful beyond the entity rules — e.g. verifying a Jawafdehi case's
    start/end dates against registration_date_ad / verdict_date_ad.
    Returns None if the case is not found.
    """
    try:
        data = get_court_case(case_ref, timeout=timeout)
    except NgmNotFound:
        return None
    return {k: v for k, v in data.items() if k not in ("hearings", "entities")}


def get_case_entities(case_ref, timeout=30):
    """Entity rows (plaintiffs + defendants) for one court case."""
    try:
        data = get_court_case(case_ref, timeout=timeout)
    except NgmNotFound:
        return []
    return data.get("entities") or []


def defendants_for_case(case, timeout=30):
    """Collect NGM defendant names across all of a Jawafdehi case's court refs.

    Returns a dict with the matched refs and the deduped defendant-name list,
    plus any per-ref errors so a caller can tell "no court ref" (cannot verify)
    apart from "queried, found nothing".
    """
    refs = court_refs_for_case(case)
    out = {"refs": refs, "defendants": [], "errors": []}
    seen = set()
    for ref in refs:
        try:
            entities = get_case_entities(ref, timeout=timeout)
        except NgmError as e:
            out["errors"].append(f"{ref}: {e}")
            continue
        for row in entities:
            if (row.get("side") or "").lower() != "defendant":
                continue
            name = (row.get("name") or "").strip()
            key = re.sub(r"\s+", " ", name)
            if name and key not in seen:
                seen.add(key)
                out["defendants"].append(name)
    return out
