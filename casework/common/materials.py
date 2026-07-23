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
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

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
    SHOULD opt in, and both current ones do: ``bind_materials.plan_case`` and
    ``source_abhiyog.build_rows`` each forward their own ``--probe-retries`` /
    ``--probe-interval`` (CLI default 4 retries at a 1.0s base). That matters
    most in the binder, where an uncertain probe escalates to "abort the whole
    case" -- without retry a rate-limit burst aborts cases whose materials are
    perfectly present. Note ``material_exists`` deliberately keeps the default:
    it is the thin tri-state wrapper and takes no retry controls.
    """
    path = material_path(source, ident)
    result = None
    # `max(retries, 0)`: a negative count (argparse accepts `--probe-retries -1`)
    # would make the range EMPTY, so nothing was ever probed and this returned
    # the `None` initializer -- breaking the documented tri-state for every
    # caller (`material_exists` and `build_rows` both do `.verdict` on it).
    for attempt in range(max(retries, 0) + 1):
        result = _probe_once(api, source, ident, path, timeout)
        if result.verdict is not None:
            return result
        if attempt < retries:
            time.sleep(_backoff_s(result.retry_after, attempt, interval))
    return result


def _retry_after_s(raw, *, now=None):
    """Seconds to wait per a ``Retry-After`` header, or None if unusable.

    RFC 9110 10.2.3 gives the header TWO forms -- ``delta-seconds`` (an integer)
    and an ``HTTP-date`` -- and a server may send either. Reading only digits
    silently discards the date form, so the probe throws away the one number the
    server actually gave it and retries on its local schedule instead. Our own
    control plane sends the integer form (DRF's throttling), but the 429s that
    matter can also come from an intermediary (WAF/CDN), which is exactly where
    the date form shows up.

    A date already in the past clamps to 0 rather than going negative: clock
    skew between us and the server is normal, and ``time.sleep()`` raises on a
    negative argument. Anything unparseable returns None -- fall back to the
    exponential schedule; a malformed header must not become a traceback on the
    retry path. The caller still runs the result through :func:`_backoff_s`, so
    a server asking for an hour is capped like any other wait.
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        return float(int(text))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:  # RFC 9110 says GMT; an omitted zone means the same
        when = when.replace(tzinfo=timezone.utc)
    reference = now or datetime.now(timezone.utc)
    return max((when - reference).total_seconds(), 0.0)


def _probe_once(api, source, ident, path, timeout):
    try:
        api.get(path, timeout=timeout)
        return ProbeResult(source, ident, path, 200, True)
    except urllib.error.HTTPError as exc:
        verdict = False if exc.code in (400, 404) else None
        retry_after = None
        if exc.code == _RATE_LIMITED:
            retry_after = _retry_after_s((exc.headers or {}).get("Retry-After"))
        return ProbeResult(source, ident, path, exc.code, verdict, retry_after)
    except Exception:
        return ProbeResult(source, ident, path, None, None)


def _backoff_s(retry_after, attempt, interval):
    """Server-advertised Retry-After wins; else exponential, capped.

    Clamped to >= 0 for the same reason ``retries`` is clamped in
    :func:`probe_material`: ``time.sleep()`` raises ``ValueError`` on a negative
    argument, so a negative ``interval`` reaching here would abort the walk with
    a traceback partway through rather than degrade. The CLIs also reject it at
    the flag boundary (``cli.nonneg_float``); this is the library-level guard for
    a direct caller.
    """
    if retry_after is not None:
        return min(max(retry_after, 0), _MAX_BACKOFF_S)
    return min(max(interval, 0) * (2 ** attempt), _MAX_BACKOFF_S)


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
