"""schema.org JSON-LD shaping for NGM materials.

A *material* is any court document / order / manuscript / charge sheet / legal
corpus item / official report — anything in the CreativeWork family that NGM
publishes. The canonical stored/served form is a schema.org JSON-LD document
(``@context`` / ``@type`` / ``@id`` + properties), keyed by a material ``@id``
IRI (``https://<base>/material/<source>/<ident>``), exactly mirroring the NES
entity remodel (think-big/nes-schemaorg-remodel-plan.md, NGM-materials section).

This module is the single source of truth for:
- the ``@context`` (schema.org default + the ``jawafdehi:`` extension namespace),
- the material ``@type`` mapping table (``MATERIAL_TYPES``),
- ``media_objects_from_document_sources`` — the DocumentSource modality
  (roled links RAW/ALTERNATE/PERMALINK/...) → schema.org ``associatedMedia``
  ``MediaObject`` list,
- ``court_case_to_jsonld`` — a court-case ORM row → its CreativeWork JSON-LD,
- ``index_node_jsonld`` / ``manuscript_jsonld`` — the R2 published-index node
  shape (PART C): each manuscript/leaf node as a schema.org CreativeWork.

Pure functions, no Django/DB imports except the lazy ORM accessor in
``court_case_to_jsonld`` (so the index shaping is testable with no DB).
"""

from __future__ import annotations

from typing import Any

from jawafdehi_shared.entities.ids import build_material_iri, is_valid_material_iri

# ── namespaces / context ─────────────────────────────────────────────────────

JAWAFDEHI_NS = "https://jawafdehi.org/ns#"

#: The JSON-LD ``@context`` shared by every material document. schema.org is the
#: default vocabulary; ``jawafdehi:`` carries Nepal-specific terms with no
#: schema.org home (e.g. ``jawafdehi:CourtCase``, ``jawafdehi:ChargeSheet``).
#: ``text``/``name`` stay language-mapped so bilingual (Devanagari/Roman) text is
#: first-class — same convention as NES (``nes-schema-org.md`` §5).
MATERIAL_CONTEXT: list[Any] = [
    "https://schema.org",
    {
        "jawafdehi": JAWAFDEHI_NS,
        "name": {"@id": "schema:name", "@container": "@language"},
        "text": {"@id": "schema:text", "@container": "@language"},
        "description": {"@id": "schema:description", "@container": "@language"},
    },
]


# ── @type mapping ────────────────────────────────────────────────────────────
# Each NGM material kind → its schema.org @type plus an optional jawafdehi:
# additionalType where schema.org has no faithful term. Keyed by a stable NGM
# material_type token (the value persisted on Material.material_type).


class MaterialType:
    """Stable NGM material-type tokens (the ``Material.material_type`` values)."""

    COURT_CASE = "court_case"          # the case RECORD itself
    COURT_ORDER = "court_order"        # order / verdict / manuscript scan
    MANUSCRIPT = "manuscript"          # a scanned manuscript document
    CHARGE_SHEET = "charge_sheet"      # CIAA/AG अभियोगपत्र
    LEGAL_CORPUS = "legal_corpus"      # acts / laws / ordinances / constitution
    OFFICIAL_REPORT = "official_report"  # OAG / annual reports
    DOCUMENT = "document"              # generic court filing / misc document


#: material_type → (schema.org @type, jawafdehi additionalType | None).
#: @type may be a single string or a list (schema.org permits multi-typing).
MATERIAL_TYPES: dict[str, tuple[Any, str | None]] = {
    # Court case record: schema.org has no LegalCase, so CreativeWork +
    # jawafdehi:CourtCase additionalType.
    MaterialType.COURT_CASE: ("CreativeWork", "jawafdehi:CourtCase"),
    # Court order / verdict / manuscript scan → Manuscript (a digital scan of a
    # written legal document) + DigitalDocument multi-type.
    MaterialType.COURT_ORDER: (["Manuscript", "DigitalDocument"], None),
    MaterialType.MANUSCRIPT: (["Manuscript", "DigitalDocument"], None),
    # Charge sheet → DigitalDocument + jawafdehi:ChargeSheet.
    MaterialType.CHARGE_SHEET: ("DigitalDocument", "jawafdehi:ChargeSheet"),
    # Legal corpus (acts/laws/ordinances/constitution) → Legislation.
    MaterialType.LEGAL_CORPUS: ("Legislation", None),
    # Official report (OAG audit, annual reports) → Report.
    MaterialType.OFFICIAL_REPORT: ("Report", None),
    MaterialType.DOCUMENT: ("DigitalDocument", None),
}

#: The full set of accepted schema.org @type strings for materials (used by the
#: lightweight validator). Flattened from MATERIAL_TYPES plus the bare types the
#: index node shape may emit.
KNOWN_MATERIAL_SCHEMA_TYPES: frozenset[str] = frozenset(
    {
        "CreativeWork",
        "Manuscript",
        "DigitalDocument",
        "Legislation",
        "LegislationObject",
        "Report",
        "MediaObject",
    }
)


def type_for(material_type: str) -> tuple[Any, str | None]:
    """(@type, additionalType) for a material_type token. Defaults to DigitalDocument."""
    return MATERIAL_TYPES.get(material_type, ("DigitalDocument", None))


# ── DocumentSource modality → associatedMedia / MediaObject ──────────────────
# DocumentSource roled links (the `document_sources` JSONB on court_cases) take
# the shape: {"document_id": ..., "url": [{"link": str, "role": RAW|...}], ...}.
# Each link becomes a schema.org MediaObject; the per-source list becomes one
# CreativeWork carrying those as associatedMedia. Roles RAW/ALTERNATE/PERMALINK
# (+ SOURCE_PAGE/MARKDOWN) are preserved on jawafdehi:linkRole.

_ROLE_ENCODING_HINTS = {
    "RAW": None,        # original file; encodingFormat unknown from the link alone
    "ALTERNATE": None,
    "PERMALINK": None,
    "SOURCE_PAGE": "text/html",
    "MARKDOWN": "text/markdown",
}


def _media_object(link: dict[str, Any]) -> dict[str, Any] | None:
    """One roled link → a schema.org ``MediaObject``. ``None`` if no target."""
    url = (link.get("link") or "").strip()
    if not url:
        return None
    role = link.get("role") or "RAW"
    mo: dict[str, Any] = {
        "@type": "MediaObject",
        "contentUrl": url,
        "jawafdehi:linkRole": role,
    }
    fmt = _ROLE_ENCODING_HINTS.get(role)
    if fmt:
        mo["encodingFormat"] = fmt
    return mo


def media_objects_from_document_sources(
    document_sources: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Flatten the DocumentSource modality into a schema.org ``associatedMedia``
    list of ``MediaObject``s (roled links RAW/ALTERNATE/PERMALINK/...).

    Each DocumentSource entry contributes its roled ``url`` links as MediaObjects
    in order; the source's ``document_id`` (when present) rides on the MediaObject
    as ``identifier`` so the media trace back to their logical document.
    """
    media: list[dict[str, Any]] = []
    for src in document_sources or []:
        if not isinstance(src, dict):
            continue
        document_id = src.get("document_id")
        links = src.get("url")
        if not isinstance(links, list):
            continue
        for link in links:
            if not isinstance(link, dict):
                continue
            mo = _media_object(link)
            if mo is None:
                continue
            if document_id:
                mo["identifier"] = document_id
            media.append(mo)
    return media


# ── court-case record → JSON-LD ──────────────────────────────────────────────

#: ``source`` segment of a court material IRI (``/material/court/<ident>``).
COURT_SOURCE = "court"
#: ``source`` segment of a STANDALONE court-order material IRI
#: (``/material/court_order/<ident>``). Underscore — the material IRI source
#: grammar (``_SOURCE``) forbids hyphens, so the stored ``source`` column value
#: is ``court_order`` (matching ``_manuscript_material_iri``'s ``-``→``_`` slug).
COURT_ORDER_SOURCE = "court_order"


def court_case_material_iri(court_identifier: str, case_number: str) -> str:
    """The canonical material ``@id`` IRI for a court-case record.

    ident is ``<court_identifier>.<case_number>`` lowercased — the case natural
    key — so the IRI is stable + reconstructable from the relational row.
    """
    ident = f"{court_identifier}.{case_number}".lower()
    return build_material_iri(COURT_SOURCE, ident)


def court_order_material_iri(
    court_identifier: str, case_number: str, n: int | None = None
) -> str:
    """The canonical ``@id`` IRI for a STANDALONE court-order Material.

    ``/material/court_order/<court>.<case_number>[.<n>]`` (lowercased). One order
    on a case → no ``n`` suffix; multiple orders → a stable 1-based ``.n`` suffix
    (ordered by ``document_sources``). The importer OWNS this namespace; the
    Jawafdehi case-source converter (spec 06) reuses these IRIs for dedup so a
    court order cited by a case AND scraped by NGM is ONE Material.
    """
    ident = f"{court_identifier}.{case_number}".lower()
    if n is not None:
        ident = f"{ident}.{n}"
    return build_material_iri(COURT_ORDER_SOURCE, ident)


def case_order_sources(
    document_sources: list[dict[str, Any]] | None,
) -> list[tuple[dict[str, Any], int | None]]:
    """Pair each order ``DocumentSource`` on a case with its IRI ``n`` suffix.

    Returns ``[(document_source, n), ...]`` where ``n`` is ``None`` when the case
    has exactly one order (the sole order takes no suffix) and a stable 1-based
    index when it has several. The SINGLE source of the order↔suffix mapping, so
    ``court_case_to_jsonld`` (which emits the ``hasPart`` order refs) and the
    importer's ``_materialize_orders`` (which shapes the order Materials) agree on
    every ``@id``.
    """
    sources = [s for s in (document_sources or []) if isinstance(s, dict)]
    if not sources:
        return []
    if len(sources) == 1:
        return [(sources[0], None)]
    return [(src, i + 1) for i, src in enumerate(sources)]


def court_order_to_jsonld(
    document_source: dict[str, Any],
    *,
    court_identifier: str,
    case_number: str,
    n: int | None = None,
) -> dict[str, Any]:
    """Shape ONE court-order ``DocumentSource`` into its own Material JSON-LD.

    LOCKED #1: each order doc is a STANDALONE Material (``court_order``,
    ``@type [Manuscript, DigitalDocument]``) carrying the order's roled file links
    as ``associatedMedia``. It ``isPartOf`` the case record's Material
    (``court_case_material_iri``) — the inverse of that record's ``hasPart``. ``n``
    disambiguates multiple orders on one case (``None`` → the sole order).
    """
    schema_type, additional_type = type_for(MaterialType.COURT_ORDER)
    iri = court_order_material_iri(court_identifier, case_number, n)
    document_id = (
        document_source.get("document_id") if isinstance(document_source, dict) else None
    )
    doc: dict[str, Any] = {
        "@context": MATERIAL_CONTEXT,
        "@type": schema_type,  # ["Manuscript", "DigitalDocument"]
        "@id": iri,
        "name": {"ne": str(document_id or f"{case_number} आदेश")},
        "inLanguage": "ne",
        "isPartOf": {"@id": court_case_material_iri(court_identifier, case_number)},
        "jawafdehi:court": court_identifier,
        "jawafdehi:caseNumber": case_number,
    }
    if additional_type:
        doc["additionalType"] = additional_type
    if document_id:
        doc["identifier"] = document_id
    media = media_objects_from_document_sources([document_source])
    if media:
        doc["associatedMedia"] = media
    return doc


def court_case_to_jsonld(case: Any) -> dict[str, Any]:
    """Project a ``CourtCase`` ORM row into its schema.org CreativeWork JSON-LD.

    The case RECORD maps to ``CreativeWork`` + ``jawafdehi:CourtCase`` (schema.org
    has no LegalCase). Bilingual party/subject text rides in language-tagged
    ``name``/``description`` where known; each order in ``document_sources`` is
    REFERENCED as a standalone ``court_order`` Material via ``hasPart`` (LOCKED
    #1 — the order bytes hang off that order Material, not embedded here);
    resolved party IRIs (``nes_id`` on the case + on CaseEntity rows) ride as
    ``about`` entity references.
    """
    schema_type, additional_type = type_for(MaterialType.COURT_CASE)
    iri = court_case_material_iri(case.court_id, case.case_number)

    doc: dict[str, Any] = {
        "@context": MATERIAL_CONTEXT,
        "@type": schema_type,
        "@id": iri,
        "name": {"ne": f"{case.case_number}"},
        "identifier": case.case_number,
        "jawafdehi:court": case.court_id,
        "jawafdehi:caseNumber": case.case_number,
    }
    if additional_type:
        doc["additionalType"] = additional_type
    if case.case_type:
        doc["jawafdehi:caseType"] = case.case_type
    if case.case_status:
        doc["jawafdehi:caseStatus"] = case.case_status
    if case.registration_date_ad:
        doc["dateCreated"] = case.registration_date_ad.isoformat()
    if case.registration_date_bs:
        doc["jawafdehi:registrationDateBS"] = case.registration_date_bs

    # Parties as schema.org text (bilingual-friendly; stored Devanagari text).
    parties: list[str] = [p for p in (case.plaintiff, case.defendant) if p]
    if parties:
        doc["description"] = {"ne": " · ".join(parties)}

    # about: resolved entity @id IRIs (case-level + per-party CaseEntity rows).
    about: list[dict[str, str]] = []
    if case.nes_id:
        about.append({"@id": case.nes_id})
    for ent in _case_entity_iris(case):
        about.append({"@id": ent})
    if about:
        doc["about"] = about

    # LOCKED #1: each order in document_sources is a STANDALONE court_order
    # Material; the case record REFERENCES them via hasPart rather than embedding
    # their media. The order @ids are derivable from the (court, case_number)[, n]
    # key, so they are emitted whether or not the order Material rows have been
    # materialized yet (importer --materialize-orders creates the rows).
    order_parts = [
        {"@id": court_order_material_iri(case.court_id, case.case_number, n)}
        for _src, n in case_order_sources(case.document_sources)
    ]
    if order_parts:
        doc["hasPart"] = order_parts

    return doc


def _case_entity_iris(case: Any) -> list[str]:
    """Resolved (non-null) ``nes_id`` IRIs of the case's party rows, if loadable.

    Lazy + defensive: if the related manager isn't available (e.g. a bare/unsaved
    instance in a pure-shaping test), returns ``[]`` rather than raising.
    """
    try:
        from courts.models import CaseEntity

        rows = CaseEntity.objects.filter(
            court_id=case.court_id, case_number=case.case_number
        ).exclude(nes_id__isnull=True).exclude(nes_id="")
        return [r.nes_id for r in rows]
    except Exception:  # noqa: BLE001 — shaping must never hard-fail on DB state.
        return []


# ── R2 published index: JSON-LD node shape (PART C) ──────────────────────────
# Ported from the FastAPI ngm.index.build_index concept: the published index is
# a tree of nodes; leaf nodes carry manuscripts. Here each manuscript and each
# node becomes a schema.org CreativeWork JSON-LD object so the public crawl
# surface is linked-data. The live R2 publish stays stubbed in the lakehouse;
# THIS shaping is real + tested.

#: Index source-type hint → material_type token, so an index manuscript shapes
#: to the right schema.org @type. Mirrors ngm.index.models.SourceType.
INDEX_SOURCE_TYPE_TO_MATERIAL = {
    "COURT_ORDER": MaterialType.COURT_ORDER,
    "CIAA_PRESS_RELEASE": MaterialType.CHARGE_SHEET,
    "AG_ABHIYOG_PATRA": MaterialType.CHARGE_SHEET,
    "OAG_AUDIT_REPORT": MaterialType.OFFICIAL_REPORT,
    "LAW_OR_BILL": MaterialType.LEGAL_CORPUS,
    "COURT_FILING_OTHER": MaterialType.DOCUMENT,
    "MISC": MaterialType.DOCUMENT,
    "NEWS": MaterialType.DOCUMENT,
    "SOCIAL_MEDIA": MaterialType.DOCUMENT,
}


def _manuscript_material_iri(document_id: str) -> str | None:
    """Derive a material ``@id`` IRI from an index document_id (``ngm:<src>:<id>``).

    ``ngm:court-order:supreme:082-OA-0503`` → source ``court-order`` (slashed for
    multi-segment), ident the remaining colon-joined tail (dotted). Returns
    ``None`` when no usable id can be formed.
    """
    if not document_id:
        return None
    parts = [p for p in document_id.split(":") if p]
    # Drop a leading "ngm" namespace marker if present.
    if parts and parts[0] == "ngm":
        parts = parts[1:]
    if len(parts) < 2:
        return None
    source = parts[0].replace("-", "_")
    ident = ".".join(parts[1:]).replace("-", "_").lower()
    try:
        return build_material_iri(source, ident)
    except ValueError:
        return None


def manuscript_jsonld(manuscript: dict[str, Any]) -> dict[str, Any]:
    """Shape one index manuscript dict into a schema.org CreativeWork JSON-LD.

    Input is the index ``Manuscript.to_dict()`` shape:
    ``{url, file_name, metadata, links:[{link,role}], document_id, source_type}``.
    Output is a self-describing JSON-LD object (``@context``/``@type``/``@id`` +
    ``name``, ``url``, ``associatedMedia`` from the roled links). The roled links
    reuse the SAME MediaObject mapping as ``document_sources``.
    """
    document_id = manuscript.get("document_id") or ""
    source_type = manuscript.get("source_type") or "MISC"
    material_type = INDEX_SOURCE_TYPE_TO_MATERIAL.get(
        source_type, MaterialType.DOCUMENT
    )
    schema_type, additional_type = type_for(material_type)

    metadata = manuscript.get("metadata") or {}
    title = (
        metadata.get("title")
        or manuscript.get("file_name")
        or document_id
        or "Document"
    )

    doc: dict[str, Any] = {
        "@context": MATERIAL_CONTEXT,
        "@type": schema_type,
        "name": {"ne": str(title)},
        "inLanguage": "ne",
        "isAccessibleForFree": True,
    }
    iri = _manuscript_material_iri(document_id)
    if iri:
        doc["@id"] = iri
    if document_id:
        doc["identifier"] = document_id
    if additional_type:
        doc["additionalType"] = additional_type
    if manuscript.get("url"):
        doc["url"] = manuscript["url"]

    # Roled links → associatedMedia MediaObjects (same mapping as document_sources).
    media = media_objects_from_document_sources(
        [{"document_id": document_id, "url": manuscript.get("links") or []}]
    )
    if media:
        doc["associatedMedia"] = media

    pub = metadata.get("publication_date") or metadata.get("date")
    if pub:
        doc["datePublished"] = str(pub)
    return doc


def index_node_jsonld(node: dict[str, Any]) -> dict[str, Any]:
    """Shape one index *node* into a schema.org JSON-LD object.

    A node is ``{name, path, children?:[{name,path,$ref}], manuscripts?:[...],
    next?}``. Branch nodes become a ``CollectionPage`` (``hasPart`` → child
    refs); leaf nodes a ``Collection`` whose ``hasPart`` is each manuscript's
    CreativeWork JSON-LD. ``next`` (pagination) rides on ``jawafdehi:next``.
    This is the public-crawl linked-data surface (R2 publish itself stays
    stubbed in the lakehouse).
    """
    name = node.get("name") or ""
    path = node.get("path") or "/"
    children = node.get("children") or []
    manuscripts = node.get("manuscripts") or []
    is_leaf = bool(manuscripts) and not children

    doc: dict[str, Any] = {
        "@context": MATERIAL_CONTEXT,
        "@type": "Collection" if is_leaf else "CollectionPage",
        "@id": f"https://jawafdehi.org/index{path}" if path else None,
        "name": {"ne": str(name)},
        "jawafdehi:indexPath": path,
    }
    if doc["@id"] is None:
        del doc["@id"]

    parts: list[dict[str, Any]] = []
    for child in children:
        if not isinstance(child, dict):
            continue
        ref: dict[str, Any] = {
            "@type": "CollectionPage",
            "name": {"ne": str(child.get("name") or "")},
            "jawafdehi:indexPath": child.get("path"),
        }
        if child.get("$ref"):
            ref["url"] = child["$ref"]
        parts.append(ref)
    for ms in manuscripts:
        if isinstance(ms, dict):
            parts.append(manuscript_jsonld(ms))
    if parts:
        doc["hasPart"] = parts

    if node.get("next") is not None:
        doc["jawafdehi:next"] = node["next"]
    return doc


# ── lightweight validation ───────────────────────────────────────────────────


def validate_material_jsonld(data: dict[str, Any], *, iri: str | None = None) -> None:
    """Minimal validation of a material JSON-LD doc (per the remodel plan).

    Checks only: ``@type`` is a known schema.org/material type, ``@id`` is a
    valid material IRI (and matches ``iri`` when given), ``name`` present. The
    rest is free-form JSON-LD. Raises ``ValueError`` on the first violation.
    """
    if not isinstance(data, dict):
        raise ValueError("material JSON-LD must be a JSON object")

    types = data.get("@type")
    type_list = types if isinstance(types, list) else [types]
    for t in type_list:
        if t not in KNOWN_MATERIAL_SCHEMA_TYPES:
            raise ValueError(f"unknown material @type: {t!r}")

    doc_id = data.get("@id")
    if not doc_id or not is_valid_material_iri(doc_id):
        raise ValueError(f"@id is not a valid material IRI: {doc_id!r}")
    if iri is not None and doc_id != iri:
        raise ValueError(f"@id {doc_id!r} does not match material iri {iri!r}")

    if not data.get("name"):
        raise ValueError("material JSON-LD requires a 'name'")
