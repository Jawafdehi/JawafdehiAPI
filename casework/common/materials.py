# casework/common/materials.py
"""Resolve a case's evidence to extractable text.

Replaces the donor's DocumentSource-shaped content_from_evidence_entry /
source_content. The current payload is
{material_iri, additional_details, material: {material_type, urls:[{link, role}]}}
and `material` resolves ONLY on the case DETAIL endpoint.
"""
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


@dataclass
class ProbeResult:
    """Outcome of one existence probe, with enough detail to log an audit line.

    ``verdict``: True (exists) / False (absent) / None (uncertain).
    ``status``: the HTTP code (200/400/404/5xx) or None on a transport error.
    ``path``: the exact request path probed, for the log.
    """
    source: str
    ident: str
    path: str
    status: int | None
    verdict: bool | None


def probe_material(api, source, ident, timeout=45):
    """Probe GET /materials/<source>/<ident>/ and return a ProbeResult.

    verdict is True (200), False (400/404 -- definitively absent), or None
    (5xx/timeout/transport error -- uncertain: the caller MUST NOT bind, and
    MUST NOT write a partial list on its account, since the whole-list replace
    is destructive). Reads go through the api client (never write-guarded),
    inheriting its auth and browser UA. The server validates IRI *grammar*
    only and never checks material existence on write, so this client-side
    probe is the only thing between a typo'd ident and a bound stub.
    """
    path = f"/materials/{source}/{urllib.parse.quote(ident, safe='')}/"
    try:
        api.get(path, timeout=timeout)
        return ProbeResult(source, ident, path, 200, True)
    except urllib.error.HTTPError as exc:
        verdict = False if exc.code in (400, 404) else None
        return ProbeResult(source, ident, path, exc.code, verdict)
    except Exception:  # noqa: BLE001 - any probe failure means 'not resolvable'
        return ProbeResult(source, ident, path, None, None)


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


def typed_materials(case, types=None):
    """[(material_type, material_iri, material)] for the same entries
    `materials_of_type` selects.

    Exists because the `material_iri` lives on the EVIDENCE ENTRY, not inside
    the resolved `material` dict (see `cases/serializers.py`: an entry is
    `{material_iri, additional_details, material}` and `material` carries
    `material_type` + `urls`, no `@id`). `materials_of_type` returns the inner
    dict and so discards the only identifier a reader can use to find the
    document again -- which the human review file has to print.
    """
    out = []
    for entry in case.get("evidence") or []:
        material = entry.get("material") or {}
        if not material:
            continue
        mtype = material.get("material_type")
        if types and mtype not in types:
            continue
        out.append((mtype or "?", entry.get("material_iri") or "", material))
    return out


#: Language tag every value this package writes into a material's language maps.
#: The corpus is Nepali; `materials/jsonld.py` tags stored Devanagari as `ne`.
MATERIAL_LANG = "ne"
DESCRIPTION_PATH = "/description"


def lang_text(raw):
    """Flatten a JSON-LD language map to plain text, or `""`.

    `MATERIAL_CONTEXT` in `materials/jsonld.py` declares `name`, `text` AND
    `description` as `{"@container": "@language"}`, so every one of them is a
    `{"ne": "..."}` map rather than a string. Reading any of them as a string
    yields `""` for a populated field -- which, for `description`, would make a
    described material look blank and invite the overwrite that the "only when
    blank" rule exists to prevent.

    Tolerates the three shapes the store actually holds: the `ne` map (what the
    shapers write), a map in some other language, and a legacy bare string.
    Whitespace-only reads as blank.
    """
    if isinstance(raw, str):
        return raw.strip()
    if isinstance(raw, dict):
        # `ne` first (the corpus language), then any other tag that carries
        # text -- an English value still means "not blank".
        for key in (MATERIAL_LANG, *sorted(k for k in raw if k != MATERIAL_LANG)):
            value = raw.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def description_text(material_doc):
    """The material's stored abstract as plain text, or `""` when it has none.

    Thin read over :func:`lang_text` -- see there for why the shape matters.
    """
    return lang_text((material_doc or {}).get("description"))


def description_ops(text, lang=MATERIAL_LANG):
    """RFC-6902 ops writing `text` as the material's abstract.

    `add`, NOT `replace`: RFC-6902 `replace` requires the target path to already
    exist, and the materials this stage writes to have no `description` key at
    all (verified on production press releases and court orders, 2026-08-05).
    `add` creates the key when it is missing and replaces the value when it is
    present, so one op covers both. The value is a language map for the reason
    `description_text` documents.
    """
    return [{"op": "add", "path": DESCRIPTION_PATH, "value": {lang: text}}]


def fetch_markdown(link, timeout=60):
    req = urllib.request.Request(link, headers={"User-Agent": BROWSER_UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def source_chunks(case, api=None, types=None):
    """Return ([(material_type, material_iri, text)], unmet_reasons).

    The typed half of :func:`source_text` -- same fetch, same unmet reporting,
    but the caller keeps track of WHICH material each chunk came from.
    `enrich_description` needs that twice: to order the sources by usefulness
    (charge sheet, then press release, then verdict) rather than by whatever
    order evidence happens to be in, and to name the material IRI beside the
    passage in the human review file.

    A material without a MARKDOWN role is NEVER fabricated or guessed at --
    it is reported as an unmet prerequisite so the run summary can show it.
    """
    chunks, unmet = [], []
    # An evidence entry whose `material` is null means the payload came from
    # the LIST endpoint, which never resolves materials. Without this check
    # typed_materials() drops those entries and this returns ([], []) --
    # indistinguishable from a genuinely evidence-free case, and exactly the
    # silent false-parity failure this module exists to prevent.
    unresolved = sum(
        1 for e in (case.get("evidence") or []) if not (e.get("material") or {})
    )
    if unresolved:
        unmet.append(
            f"{unresolved} evidence entries with an UNRESOLVED material -- the "
            "list endpoint returns material:null; use the case DETAIL endpoint"
        )
    for mtype, iri, material in typed_materials(case, types):
        link = markdown_link(material)
        if not link:
            unmet.append(f"{mtype}: no MARKDOWN role (has {len(raw_links(material))} RAW)")
            continue
        try:
            text = fetch_markdown(link)
        except Exception as exc:  # noqa: BLE001 - fetch failure becomes an unmet reason, not a crash
            unmet.append(f"{mtype}: MARKDOWN fetch failed ({exc})")
            continue
        if text.strip():
            chunks.append((mtype, iri, text))
        else:
            unmet.append(f"{mtype}: MARKDOWN empty")
    return chunks, unmet


def source_text(case, api=None, types=None):
    """Return (joined_text, unmet_reasons). Thin join over `source_chunks`.

    Kept as the entry point for every caller that only wants one blob of text
    (`enrich_allegations`, `enrich_timeline`, `enrich_related_entities`,
    `enrich_missing_bigo`) so there is exactly ONE implementation of the fetch
    and of the unmet-reason wording.
    """
    chunks, unmet = source_chunks(case, api, types)
    return "\n\n".join(text for _, _, text in chunks), unmet
