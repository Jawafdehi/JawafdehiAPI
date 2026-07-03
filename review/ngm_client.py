"""Reusable client for the NGM (Nepal Governance Modernization) judicial DB.

Talks to the in-process NGM read plane (``courts``), mounted at
``<host>/api/`` on the same platform that serves the Jawafdehi API. The old
``/api/courtcases/<court>:<case_number>`` proxy (which forwarded to a
standalone NGM service) was retired in the service consolidation; this client now
targets the read plane directly:

  * ``GET {ngm_base}/courtcases/{court}/{number}``            → the case record
  * ``GET {ngm_base}/courtcases/{court}/{number}/entities``   → parties (paginated)
  * ``GET {ngm_base}/courtcases/{court}/{number}/hearings``   → hearings (paginated)

It still speaks HTTP (not the ORM) because casework can run against a REMOTE
portal (``JAWAFDEHI_API_BASE`` may point at portal.jawafdehi.org while the
review worker runs elsewhere). ``get_court_case`` reassembles the case +
hearings + entities into one dict so its callers keep the previous contract.

Case dicts carry court refs as canonical court-case @id IRIs
(``https://<base>/courtcase/special/081-cr-0079``); internally this module
also round-trips its own compact ``"special:081-CR-0079"`` spelling (used for
read-plane lookups, judge prompts, and error messages). The read plane
best-effort normalizes case numbers server-side.

Intentionally generic so it can back several review rules and future checks
(e.g. verifying a case's start/end dates against ``registration_date_ad`` /
``verdict_date_ad`` on the returned record).
"""

import re
from urllib.parse import quote

import requests
from django.conf import settings

from jawafdehi_shared.entities.ids import parse_courtcase_iri
from review.oidc_client_credentials import OIDCTokenError, bearer_header

# Cap how many pages of a paginated sub-resource (entities/hearings) we follow,
# so a pathological case can't make the review worker loop unboundedly. Court
# cases have a handful of parties/hearings; the platform page size is 50.
_MAX_SUBRESOURCE_PAGES = 20


class NgmError(Exception):
    pass


class NgmNotFound(NgmError):
    """The court case ref was not found in NGM (HTTP 404)."""


def _ngm_base():
    """Base URL of the unified ``/api`` surface (``<host>/api``).

    Court-case records live at ``<host>/api/courtcases/...`` on the same platform
    that serves the Jawafdehi API. Derived from ``JAWAFDEHI_API_BASE``, which is
    already the ``/api`` base; normalize to end in exactly ``/api``.
    """
    base = getattr(
        settings, "JAWAFDEHI_API_BASE", "https://portal.jawafdehi.org/api"
    ).rstrip("/")
    if not base.endswith("/api"):
        base = f"{base}/api"
    return base


# Court refs come from case data (operator-entered), so validate strictly before
# they ever reach a URL: identifier is lowercase alphanumerics; case number is
# the case-insensitive mirror of the shared court-case IRI grammar
# (jawafdehi_shared.entities.ids._CASE_NUMBER: letter/digit-led, then
# letters/digits/._-), so every storable reference stays queryable here. The
# leading alphanumeric rejects path traversal ("..", leading dots) and the
# charset excludes /, ?, #, and whitespace up front.
_COURT_ID_RE = re.compile(r"^[a-z0-9]+$")
_CASE_NUMBER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def parse_court_ref(ref):
    """Parse a court-case ref into a (court, number) tuple.

    Accepts the canonical court-case @id IRI
    (``https://<base>/courtcase/<court>/<case_number>`` — the form stored on
    cases) as well as this module's own compact
    ``"<court_identifier>:<case_number>"`` spelling (what
    ``court_refs_for_case`` emits and callers hand back for lookups). Returns
    None if the ref is neither, or contains unsafe characters.
    """
    if not ref:
        return None
    ref = str(ref).strip()
    try:
        parsed = parse_courtcase_iri(ref)
        court, number = parsed.court, parsed.case_number
    except ValueError:
        if "://" in ref or ":" not in ref:
            return None
        court, _, number = ref.partition(":")
        court, number = court.strip(), number.strip()
    if not court or not number:
        return None
    if not _COURT_ID_RE.match(court) or not _CASE_NUMBER_RE.match(number):
        return None
    return court, number


def court_refs_for_case(case):
    """All court refs from a case's court_cases field, as ``"<court>:<case_number>"``.

    The stored form is the canonical @id IRI; refs are re-emitted in the short
    form (case number uppercased) — the compact spelling used for read-plane
    lookups, judge prompts, and error messages.
    """
    refs = []
    for ref in case.get("court_cases") or []:
        parsed = parse_court_ref(ref)
        if parsed:
            refs.append(f"{parsed[0]}:{parsed[1].upper()}")
    return refs


def _auth_headers():
    """Accept + (best-effort) OIDC bearer headers for an NGM read-plane call.

    The read plane is public, but the API is OIDC-only for anything gated: send
    the casework service account's Zitadel bearer when its client-credentials
    are configured, otherwise call unauthenticated.
    """
    headers = {"Accept": "application/json"}
    try:
        headers.update(bearer_header())
    except OIDCTokenError:
        pass
    return headers


def _get(url, timeout, *, allow_404=False):
    """GET ``url`` with auth headers; return parsed JSON (or None on allowed 404)."""
    try:
        r = requests.get(url, headers=_auth_headers(), timeout=timeout)
    except requests.RequestException as e:
        raise NgmError(f"NGM request failed: {e}") from e
    if r.status_code == 404 and allow_404:
        return None
    if r.status_code == 404:
        raise NgmNotFound(f"NGM resource not found: {url}")
    if r.status_code != 200:
        raise NgmError(f"NGM HTTP {r.status_code}: {r.text[:300]}")
    return r.json()


def _collect_paginated(url, timeout):
    """Follow a ``{results, next}`` cursor-paginated list into a flat list."""
    items = []
    pages = 0
    while url and pages < _MAX_SUBRESOURCE_PAGES:
        payload = _get(url, timeout)
        if isinstance(payload, dict):
            items.extend(payload.get("results") or [])
            url = payload.get("next")
        elif isinstance(payload, list):
            # Defensive: an unpaginated list response.
            items.extend(payload)
            url = None
        else:
            url = None
        pages += 1
    return items


def get_court_case(case_ref, timeout=30):
    """Fetch one NGM court case (with hearings + entities) by ``court:number``.

    Returns the case record dict augmented with ``hearings`` and ``entities``
    lists (the read plane serves these as separate sub-resources; this client
    reassembles them into one dict for callers). Raises NgmNotFound (404) /
    NgmError.
    """
    parsed = parse_court_ref(case_ref)
    if not parsed:
        raise NgmError(
            f"Invalid court ref '{case_ref}'; expected a court-case @id IRI or "
            "'<court>:<case_number>'."
        )
    court, number = parsed

    # Rebuild + percent-encode from the validated parts (never interpolate the
    # raw ref) so neither segment can break out into the URL path/query.
    base = f"{_ngm_base()}/courtcases/{quote(court, safe='')}/{quote(number, safe='')}"
    case = _get(base, timeout)
    if not isinstance(case, dict):
        raise NgmError(f"Unexpected NGM case payload for '{case_ref}'.")
    case["entities"] = _collect_paginated(f"{base}/entities", timeout)
    case["hearings"] = _collect_paginated(f"{base}/hearings", timeout)
    return case


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
