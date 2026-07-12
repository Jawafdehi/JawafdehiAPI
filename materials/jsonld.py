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

from jawafdehi_shared.entities.ids import (
    build_courtcase_iri,
    build_material_iri,
    build_source_material_iri,
    is_valid_material_iri,
)

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
    PRECEDENT = "precedent"            # published law-journal precedent (नजिर) — NKP
    MANUSCRIPT = "manuscript"          # a scanned manuscript document
    CHARGE_SHEET = "charge_sheet"      # CIAA/AG अभियोगपत्र
    LEGAL_CORPUS = "legal_corpus"      # acts / laws / ordinances / constitution
    OFFICIAL_REPORT = "official_report"  # OAG / annual reports
    NEWS = "news"                      # news / media article (was Jawafdehi SourceType.NEWS)
    SOCIAL_MEDIA = "social_media"      # social-media post (was SourceType.SOCIAL_MEDIA)
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
    # Published law-journal precedent (नेपाल कानून पत्रिका नजिर): the citable,
    # edited ruling that establishes binding precedent — neither a raw docket
    # record (court_case) nor enacted legislation (legal_corpus). schema.org has
    # no term, so CreativeWork + jawafdehi:Precedent.
    MaterialType.PRECEDENT: ("CreativeWork", "jawafdehi:Precedent"),
    MaterialType.MANUSCRIPT: (["Manuscript", "DigitalDocument"], None),
    # Charge sheet → DigitalDocument + jawafdehi:ChargeSheet.
    MaterialType.CHARGE_SHEET: ("DigitalDocument", "jawafdehi:ChargeSheet"),
    # Legal corpus (acts/laws/ordinances/constitution) → Legislation.
    MaterialType.LEGAL_CORPUS: ("Legislation", None),
    # Official report (OAG audit, annual reports) → Report.
    MaterialType.OFFICIAL_REPORT: ("Report", None),
    # News/media article → schema.org NewsArticle (a CreativeWork subtype).
    MaterialType.NEWS: ("NewsArticle", None),
    # Social-media post → SocialMediaPosting.
    MaterialType.SOCIAL_MEDIA: ("SocialMediaPosting", None),
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
        "NewsArticle",
        "SocialMediaPosting",
        "MediaObject",
    }
)


def type_for(material_type: str) -> tuple[Any, str | None]:
    """(@type, additionalType) for a material_type token. Defaults to DigitalDocument."""
    return MATERIAL_TYPES.get(material_type, ("DigitalDocument", None))


#: schema.org @type -> material_type token, for deriving the promoted column when
#: a doc doesn't state material_type explicitly. Falls back to DOCUMENT.
_TYPE_BY_SCHEMA: dict[str, str] = {
    "Legislation": MaterialType.LEGAL_CORPUS,
    "LegislationObject": MaterialType.LEGAL_CORPUS,
    "Report": MaterialType.OFFICIAL_REPORT,
    "Manuscript": MaterialType.MANUSCRIPT,
    "DigitalDocument": MaterialType.DOCUMENT,
    "CreativeWork": MaterialType.DOCUMENT,
    "NewsArticle": MaterialType.NEWS,
    "SocialMediaPosting": MaterialType.SOCIAL_MEDIA,
}

#: jawafdehi ``additionalType`` -> material_type token. Several material types
#: share one schema.org ``@type`` (court_case, precedent and generic docs are all
#: ``CreativeWork``); their discriminator is the ``additionalType``. Derived from
#: MATERIAL_TYPES so it can't drift from the shaping table. Consulted BEFORE the
#: bare-@type fallback, so a bare precedent/court-case doc isn't flattened to
#: ``document``.
_TYPE_BY_ADDITIONAL: dict[str, str] = {
    additional: token
    for token, (_schema, additional) in MATERIAL_TYPES.items()
    if additional
}


def infer_material_type(doc: dict[str, Any]) -> str:
    """Derive a material_type token from a doc that omits an explicit one.

    Prefers the jawafdehi ``additionalType`` discriminator (which distinguishes
    the several material types sharing one schema.org ``@type`` — e.g. court_case
    vs precedent vs generic CreativeWork), then falls back to the bare ``@type``.
    Defaults to DOCUMENT.
    """
    additional = doc.get("additionalType")
    for a in additional if isinstance(additional, list) else [additional]:
        if isinstance(a, str) and a in _TYPE_BY_ADDITIONAL:
            return _TYPE_BY_ADDITIONAL[a]

    atype = doc.get("@type")
    for t in atype if isinstance(atype, list) else [atype]:
        if isinstance(t, str) and t in _TYPE_BY_SCHEMA:
            return _TYPE_BY_SCHEMA[t]
    return MaterialType.DOCUMENT


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

    Each order doc is a STANDALONE Material (``court_order``,
    ``@type [Manuscript, DigitalDocument]``) carrying the order's roled file links
    as ``associatedMedia`` — the ONE court material per document-bearing case (the
    materials layer owns documents; case identity + metadata live in the courtcase
    layer). It ``isPartOf`` the case's canonical ``/courtcase/<court>/<num>`` IRI,
    NOT a ``court_case`` Material (those were retired as redundant shadows of the
    courtcase read plane). ``n`` disambiguates multiple orders on one case
    (``None`` → the sole order). ``name`` is the human case-order title (the raw
    ``document_id`` rides on ``identifier`` only, never as the display name).
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
        "name": {"ne": f"{case_number} आदेश"},
        "inLanguage": "ne",
        "isPartOf": {"@id": build_courtcase_iri(court_identifier, case_number)},
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

    NOTE: ``court_case`` Materials are NO LONGER persisted (the materials layer
    owns documents only; case identity lives in the courtcase read plane). This
    projection now backs solely the on-the-fly read-plane derivation for a
    ``/material/court/<court>.<num>`` IRI with no stored row (``materials.views``
    + ``cases.services.material_resolver``) — a graceful linked-data fallback.

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


# ── Jawafdehi case source → Material JSON-LD ─────────────────────────────────
# A Jawafdehi case "document source" is the same modality as an NGM material:
# a titled document with roled file links + related entities. When sources fold
# into materials (ADR: cases-own-no-documents), each source becomes a first-class
# Material at /material/jawafdehi/<ident>. This shaper is the projection; it is a
# PURE function (no DB) so it unit-tests like court_case_to_jsonld.

#: Jawafdehi ``SourceType`` value → NGM ``MaterialType`` token. Governance docs
#: map to their issuer-faithful material types; news/social get first-class
#: material types (added for this fold) so downstream tiering/badges keep signal;
#: everything else is a generic ``document``. Keyed by the stable SourceType
#: *value* strings (``cases.models.SourceType``) to avoid importing the enum here
#: (keeps this module Django-model-free).
JAWAF_SOURCE_TYPE_TO_MATERIAL: dict[str, str] = {
    "CIAA_PRESS_RELEASE": MaterialType.CHARGE_SHEET,
    "AG_ABHIYOG_PATRA": MaterialType.CHARGE_SHEET,
    "OAG_AUDIT_REPORT": MaterialType.OFFICIAL_REPORT,
    "COURT_ORDER": MaterialType.COURT_ORDER,
    "COURT_FILING_OTHER": MaterialType.DOCUMENT,
    "LAW_OR_BILL": MaterialType.LEGAL_CORPUS,
    "NEWS": MaterialType.NEWS,
    "SOCIAL_MEDIA": MaterialType.SOCIAL_MEDIA,
    "MISC": MaterialType.DOCUMENT,
}


def material_type_for_source_type(source_type: str | None) -> str:
    """Map a Jawafdehi ``SourceType`` value to a ``MaterialType`` (default DOCUMENT)."""
    return JAWAF_SOURCE_TYPE_TO_MATERIAL.get(source_type or "", MaterialType.DOCUMENT)


def documentsource_to_jsonld(
    *,
    source_id: str,
    title: str,
    source_type: str | None,
    url: list[dict[str, Any]] | None,
    description: str = "",
    related_entities: list[str] | None = None,
    publication_date: Any = None,
) -> tuple[dict[str, Any], str]:
    """Shape a Jawafdehi case source into ``(jsonld_doc, material_type)``.

    Returns the material_type alongside the doc because ``Material`` stores it as
    a promoted column and ``from_jsonld`` requires it explicitly. The ``@id`` is
    ``/material/jawafdehi/<normalized source_id>``; roled links become
    ``associatedMedia`` (reusing ``media_objects_from_document_sources``);
    ``related_entities`` NES IRIs ride as ``about``; ``publication_date`` →
    ``datePublished``. Accepts primitive fields (not the ORM object) so it stays
    a pure, DB-free projection.
    """
    material_type = material_type_for_source_type(source_type)
    schema_type, additional_type = type_for(material_type)
    iri = build_source_material_iri(source_id)

    doc: dict[str, Any] = {
        "@context": MATERIAL_CONTEXT,
        "@type": schema_type,
        "@id": iri,
        # Source titles are stored as plain (often Devanagari) strings; tag as
        # Nepali so the bilingual name container stays consistent with materials.
        "name": {"ne": (title or "").strip() or source_id},
        "jawafdehi:sourceType": source_type or "MISC",
    }
    if additional_type:
        doc["additionalType"] = additional_type
    if description and description.strip():
        doc["description"] = {"ne": description.strip()}
    media = media_objects_from_document_sources([{"url": url}])
    if media:
        doc["associatedMedia"] = media
    about = [{"@id": iri_} for iri_ in (related_entities or []) if iri_]
    if about:
        doc["about"] = about
    if publication_date is not None:
        # Accept a date/datetime (isoformat) or an already-ISO string.
        iso = getattr(publication_date, "isoformat", lambda: str(publication_date))()
        doc["datePublished"] = iso
    return doc, material_type


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
