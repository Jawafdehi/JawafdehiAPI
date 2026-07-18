# casework/common/materials.py
"""Resolve a case's evidence to extractable text.

Replaces the donor's DocumentSource-shaped content_from_evidence_entry /
source_content. The current payload is
{material_iri, additional_details, material: {material_type, urls:[{link, role}]}}
and `material` resolves ONLY on the case DETAIL endpoint.
"""
import urllib.request

MARKDOWN_ROLE = "MARKDOWN"
CONVERTIBLE_ROLES = ("RAW", "ALTERNATE", "SOURCE_PAGE")


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
    with urllib.request.urlopen(link, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def source_text(case, api=None, types=None):
    """Return (joined_text, unmet_reasons).

    A material without a MARKDOWN role is NEVER fabricated or guessed at --
    it is reported as an unmet prerequisite so the run summary can show it.
    """
    chunks, unmet = [], []
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
