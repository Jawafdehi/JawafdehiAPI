# casework/common/materials.py
"""Resolve a case's evidence to extractable text.

Replaces the donor's DocumentSource-shaped content_from_evidence_entry /
source_content. The current payload is
{material_iri, additional_details, material: {material_type, urls:[{link, role}]}}
and `material` resolves ONLY on the case DETAIL endpoint.
"""
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from casework.common.api import BROWSER_UA

MARKDOWN_ROLE = "MARKDOWN"
CONVERTIBLE_ROLES = ("RAW", "ALTERNATE", "SOURCE_PAGE")

# Canonical material IRI host. Material @ids are `https://jawafdehi.org/material
# /<source>/<ident>` regardless of which API host serves them.
MATERIAL_HOST = "https://jawafdehi.org"


def material_iri(source, ident):
    """Build the canonical `@id` for a material. Callers pass an ident already
    in its final form (see the source-specific builders below)."""
    return f"{MATERIAL_HOST}/material/{source}/{ident}"


def press_release_ident(release_id):
    """CIAA press release: the ident IS the dataset release_id."""
    return str(release_id).strip()


def court_order_ident(court, case_no):
    """Special/Supreme court order ident = `<court>.<case-no LOWERCASED>`.

    The case number is self-identifying, but the server returns HTTP 400 for an
    UPPERCASE ident -- so lowercasing is mandatory, not cosmetic.
    """
    return f"{court}.{case_no.strip().lower()}"


def ag_ident(record_id):
    """AG अभियोगपत्र (indictment): the ident IS the AG portal record id.

    `materials/sourcing/ag/shaper.py` deliberately keys the IRI on `record_id`
    and NOT on the case number: ~969 case numbers repeat across AG offices, so
    keying on the case number would collide distinct indictments onto one `@id`
    and silently overwrite them on upsert. That choice is what makes case-number
    recovery a DIRECT id join -- `/material/ag/<record_id>` round-trips straight
    back to the portal record it was scraped from (no sha256 content-join).
    """
    return str(record_id).strip()


@dataclass
class ProbeResult:
    """Outcome of one existence probe, with enough detail to log an audit line.

    ``verdict``: True (exists) / False (absent) / None (uncertain).
    ``status``: the HTTP code (200/400/404/5xx) or None on a transport error.
    ``path``: the exact request path probed, for the log.
    ``retry_after``: seconds the server asked us to wait (429 only), else None.
    """
    source: str
    ident: str
    path: str
    status: int | None
    verdict: bool | None
    retry_after: float | None = None


def material_path(source, ident):
    """The control-plane path for one material. Single definition so a read and
    the subsequent write can never disagree about how an ident is escaped."""
    return f"/materials/{source}/{urllib.parse.quote(ident, safe='')}/"


#: HTTP 429. The production materials API rate-limits under bursts, and a
#: throttled read is INDISTINGUISHABLE from a 5xx once it collapses to the
#: "uncertain" verdict -- which callers treat as a hard stop (bind_materials
#: aborts the whole case). So it is retried here, in the shared primitive,
#: rather than in any one caller.
_RATE_LIMITED = 429
_MAX_BACKOFF_S = 30


def probe_material(api, source, ident, timeout=45, *, retries=0, interval=1.0):
    """Probe GET /materials/<source>/<ident>/ and return a ProbeResult.

    verdict is True (200), False (400/404 -- definitively absent), or None
    (5xx/timeout/transport error -- uncertain: the caller MUST NOT bind, and
    MUST NOT write a partial list on its account, since the whole-list replace
    is destructive). Reads go through the api client (never write-guarded),
    inheriting its auth and browser UA. The server validates IRI *grammar*
    only and never checks material existence on write, so this client-side
    probe is the only thing between a typo'd ident and a bound stub.

    An uncertain outcome is RETRIED with exponential backoff (``retries``
    extra attempts, honouring ``Retry-After`` on a 429) before it is reported.
    Without this a burst of probes against production returns a spray of
    "uncertain" that is really just throttling, which callers then act on as
    if the lake were unreachable.

    ``retries`` defaults to 0 -- a single shot, i.e. exactly the historical
    behaviour -- so enabling backoff never silently changes the timing of an
    existing caller. Any caller that walks a large cohort against production
    SHOULD opt in. NOTE: ``bind_materials`` does not yet, and it escalates an
    uncertain probe to "abort the whole case", so a rate-limit burst there
    aborts cases whose materials are actually present; wiring it up is a
    deliberate behaviour change and is left to its owner.
    """
    path = material_path(source, ident)
    result = None
    for attempt in range(retries + 1):
        result = _probe_once(api, source, ident, path, timeout)
        if result.verdict is not None:
            return result
        if attempt < retries:
            time.sleep(_backoff_s(result.retry_after, attempt, interval))
    return result


def _probe_once(api, source, ident, path, timeout):
    try:
        api.get(path, timeout=timeout)
        return ProbeResult(source, ident, path, 200, True)
    except urllib.error.HTTPError as exc:
        verdict = False if exc.code in (400, 404) else None
        retry_after = None
        if exc.code == _RATE_LIMITED:
            raw = (exc.headers or {}).get("Retry-After")
            retry_after = float(raw) if raw and str(raw).isdigit() else None
        return ProbeResult(source, ident, path, exc.code, verdict, retry_after)
    except Exception:
        return ProbeResult(source, ident, path, None, None)


def _backoff_s(retry_after, attempt, interval):
    """Server-advertised Retry-After wins; else exponential, capped."""
    if retry_after is not None:
        return min(retry_after, _MAX_BACKOFF_S)
    return min(interval * (2 ** attempt), _MAX_BACKOFF_S)


def material_exists(api, source, ident, timeout=45):
    """True / False / None -- the verdict of :func:`probe_material`. Thin
    wrapper kept for callers that only need the tri-state, not the audit
    detail."""
    return probe_material(api, source, ident, timeout).verdict


def _urls(material):
    return [u for u in (material.get("urls") or []) if isinstance(u, dict)]


def markdown_link(material):
    for u in _urls(material):
        if u.get("role") == MARKDOWN_ROLE and u.get("link"):
            return u["link"]
    return None


def raw_links(material):
    return [u["link"] for u in _urls(material)
            if u.get("role") in CONVERTIBLE_ROLES and u.get("link")]


def materials_of_type(case, types=None):
    out = []
    for entry in case.get("evidence") or []:
        material = entry.get("material") or {}
        if not material:
            continue
        if types and material.get("material_type") not in types:
            continue
        out.append(material)
    return out


def fetch_markdown(link, timeout=60):
    req = urllib.request.Request(link, headers={"User-Agent": BROWSER_UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def source_text(case, api=None, types=None):
    """Return (joined_text, unmet_reasons).

    A material without a MARKDOWN role is NEVER fabricated or guessed at --
    it is reported as an unmet prerequisite so the run summary can show it.
    """
    chunks, unmet = [], []
    # An evidence entry whose `material` is null means the payload came from
    # the LIST endpoint, which never resolves materials. Without this check
    # materials_of_type() drops those entries and source_text returns
    # ("", []) -- indistinguishable from a genuinely evidence-free case, and
    # exactly the silent false-parity failure this module exists to prevent.
    unresolved = sum(
        1 for e in (case.get("evidence") or []) if not (e.get("material") or {})
    )
    if unresolved:
        unmet.append(
            f"{unresolved} evidence entries with an UNRESOLVED material -- the "
            "list endpoint returns material:null; use the case DETAIL endpoint"
        )
    for material in materials_of_type(case, types):
        mtype = material.get("material_type") or "?"
        link = markdown_link(material)
        if not link:
            unmet.append(f"{mtype}: no MARKDOWN role (has {len(raw_links(material))} RAW)")
            continue
        try:
            text = fetch_markdown(link)
        except Exception as exc:
            unmet.append(f"{mtype}: MARKDOWN fetch failed ({exc})")
            continue
        if text.strip():
            chunks.append(text)
        else:
            unmet.append(f"{mtype}: MARKDOWN empty")
    return "\n\n".join(chunks), unmet
