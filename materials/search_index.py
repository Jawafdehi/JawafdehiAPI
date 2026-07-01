"""Unified-search indexer for NGM materials (``ngm-materials`` index).

Projects a ``Material`` (schema.org JSON-LD document keyed by its ``@id`` IRI)
into the common index doc. Mirrors the NES entity indexer; the material doc adds
``text`` (OCR/full-text body), the material ``identifier``/``ident`` to the
``identifiers`` field, and the dates (``dateCreated``/``datePublished`` and the
Bikram Sambat ``jawafdehi:registrationDateBS`` carried verbatim into
``date_bs``).

Best-effort: an OpenSearch error is logged and swallowed.
"""

from __future__ import annotations

from typing import Any

from jawafdehi_shared.search.indexing import (
    best_effort,
    delete_doc,
    name_to_titles,
    title_translit,
    upsert_doc,
)
from jawafdehi_shared.search.opensearch import MATERIAL_INDEX, make_client

SOURCE_APP = "ngm"


def _flatten_strings(value: Any) -> list[str]:
    out: list[str] = []
    if isinstance(value, str):
        if value.strip():
            out.append(value.strip())
    elif isinstance(value, dict):
        for v in value.values():
            out.extend(_flatten_strings(v))
    elif isinstance(value, (list, tuple)):
        for item in value:
            out.extend(_flatten_strings(item))
    return out


def _type_token(atype: Any) -> str:
    if isinstance(atype, list):
        return ",".join(str(t) for t in atype)
    return str(atype) if atype is not None else ""


def build_doc(obj: Any) -> dict[str, Any]:
    """Map a ``Material`` to the common index doc. Pure: no OpenSearch calls."""
    data: dict[str, Any] = getattr(obj, "data", None) or {}
    iri = getattr(obj, "iri", None) or data.get("@id")

    title_ne, title_en = name_to_titles(data.get("name"))

    # body: free-text / OCR. schema.org ``text`` is the full-text body; also
    # fold ``description`` in (bilingual-friendly).
    body_parts = _flatten_strings(data.get("text"))
    body_parts += _flatten_strings(data.get("description"))
    body = " ".join(body_parts) or None

    keywords = [k for k in (data.get("keywords") or []) if isinstance(k, str)]

    identifiers: list[str] = [iri] if iri else []
    # The promoted ``ident`` column + the JSON-LD ``identifier`` + court/case_no.
    for candidate in (
        getattr(obj, "ident", None),
        getattr(obj, "source", None),
        data.get("identifier"),
        data.get("jawafdehi:caseNumber"),
        data.get("jawafdehi:court"),
    ):
        for ident in _flatten_strings(candidate):
            if ident not in identifiers:
                identifiers.append(ident)

    doc: dict[str, Any] = {
        "iri": iri,
        "type": _type_token(data.get("@type")),
        "source_app": SOURCE_APP,
        "title_ne": title_ne,
        "title_en": title_en,
        "title_translit": title_translit(title_ne, title_en),
        "body": body,
        "keywords": keywords,
        "identifiers": identifiers,
        "raw": data,
    }

    # Gregorian dates (ISO). Carry Bikram Sambat verbatim (never coerced).
    date = data.get("datePublished") or data.get("dateCreated")
    if date:
        doc["date"] = str(date)
    date_bs = data.get("jawafdehi:registrationDateBS") or data.get(
        "jawafdehi:publicationDateBS"
    )
    if date_bs:
        doc["date_bs"] = str(date_bs)

    created = getattr(obj, "created_at", None)
    updated = getattr(obj, "updated_at", None)
    if created is not None:
        doc["created_at"] = created.isoformat() if hasattr(created, "isoformat") else created
    if updated is not None:
        doc["updated_at"] = updated.isoformat() if hasattr(updated, "isoformat") else updated
    return doc


@best_effort("index material")
def index(obj: Any, *, client=None) -> None:
    """Upsert the material's doc into ``ngm-materials`` (best-effort)."""
    upsert_doc(client or make_client(), MATERIAL_INDEX, build_doc(obj))


@best_effort("delete material")
def delete(obj: Any, *, client=None) -> None:
    """Delete the material's doc from ``ngm-materials`` (best-effort)."""
    iri = getattr(obj, "iri", None) or (getattr(obj, "data", None) or {}).get("@id")
    if iri:
        delete_doc(client or make_client(), MATERIAL_INDEX, iri)
