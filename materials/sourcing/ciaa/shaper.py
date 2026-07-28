"""Shape a CIAA press release (प्रेस विज्ञप्ति) into Material JSON-LD.

A press release is an inherently SINGLE-SOURCE public document — one authority
(the Commission for the Investigation of Abuse of Authority) publishes it — so it
maps to ``MaterialType.PRESS_RELEASE`` (schema.org ``CreativeWork`` +
``jawafdehi:PressRelease``). It is keyed under the ``ciaa_press_release`` IRI
source segment with the press id as the ident, which REPRODUCES the legacy index
``@id`` (the index document_id ``ngm:ciaa-press-release:<id>`` was slugged to
``/material/ciaa_press_release/<id>`` by ``jsonld._manuscript_material_iri``) — so
this go-forward writer and the materials already synced from the frozen ``ngm_v1``
index reconcile to ONE row, never a hyphen/underscore IRI fork.

Pure + DB-free: takes a ``ParsedPressRelease`` (not ORM objects), returns
``(jsonld_doc, material_type)`` — the same shape as the AG / NKP shapers.
Persisted through the material API plane (see ``materials/sourcing/README.md``):
``PUT`` this doc, then ``POST`` each attachment to ``…/file`` (RAW for the PDF,
ALTERNATE for the rest), which appends those MediaObjects to this doc. The CIAA
landing page rides here as a ``SOURCE_PAGE`` reference (a link, not a rehost).
"""

from __future__ import annotations

from typing import Any

from jawafdehi_shared.dates import bs_to_ad_iso
from jawafdehi_shared.entities.ids import build_material_iri

from materials.jsonld import (
    MATERIAL_CONTEXT,
    MaterialType,
    media_objects_from_document_sources,
    type_for,
)
from materials.sourcing.ciaa.parse import ParsedPressRelease

#: IRI source segment: ``/material/ciaa_press_release/<press_id>``. MUST equal the
#: underscored form of the legacy index document_id ``ngm:ciaa-press-release:<id>``
#: so re-ingest is idempotent with the synced rows (the idempotency anchor).
CIAA_PRESS_SOURCE = "ciaa_press_release"

#: Legacy Jawafdehi ``SourceType`` token — kept on the doc for provenance and as
#: the ``INDEX_SOURCE_TYPE_TO_MATERIAL`` / ``JAWAF_SOURCE_TYPE_TO_MATERIAL`` key.
SOURCE_TYPE = "CIAA_PRESS_RELEASE"

#: The publishing authority (CIAA), as a schema.org publisher Organization node.
_CIAA_NAME_NE = "अख्तियार दुरुपयोग अनुसन्धान आयोग"


def press_release_iri(press_id: object) -> str:
    """The canonical Material ``@id`` for a press release — the idempotency anchor."""
    return build_material_iri(CIAA_PRESS_SOURCE, str(press_id))


def press_release_to_jsonld(record: ParsedPressRelease) -> tuple[dict[str, Any], str]:
    """Shape one press release → ``(jsonld_doc, material_type)``.

    The doc carries the title (``name.ne``), body (``text.ne`` — the unified-search
    feed, so search never re-OCRs), the Bikram Sambat publication date
    (``jawafdehi:datePublishedBS``, the authoritative Nepali date) plus its AD
    conversion (``datePublished``, what search sorts on), the CIAA landing page as
    a ``SOURCE_PAGE`` MediaObject, and the legacy index id as ``identifier`` for
    cross-system trace. Binary attachments are NOT embedded here — the command
    uploads them via ``…/file``.
    """
    material_type = MaterialType.PRESS_RELEASE
    schema_type, additional_type = type_for(material_type)
    iri = press_release_iri(record.press_id)

    title = (record.title or "").strip() or f"CIAA press release {record.press_id}"
    doc: dict[str, Any] = {
        "@context": MATERIAL_CONTEXT,
        "@type": schema_type,
        "@id": iri,
        "name": {"ne": title},
        "jawafdehi:sourceType": SOURCE_TYPE,
        "identifier": f"ngm:ciaa-press-release:{record.press_id}",
        "publisher": {"@type": "GovernmentOrganization", "name": {"ne": _CIAA_NAME_NE}},
    }
    if additional_type:
        doc["additionalType"] = additional_type

    # BS is authoritative; datePublished (schema.org, AD) is what search sorts on —
    # prefer the converted AD date, else fall back to the BS string so the date is
    # never dropped when conversion is unavailable.
    date_bs = (record.publication_date_bs or "").strip()
    date_ad = bs_to_ad_iso(date_bs)
    if date_bs:
        doc["jawafdehi:datePublishedBS"] = date_bs
    if date_ad:
        doc["datePublished"] = date_ad
    elif date_bs:
        doc["datePublished"] = date_bs

    # CIAA landing page as a SOURCE_PAGE reference link (reuse the shared shaper).
    if record.source_url:
        media = media_objects_from_document_sources(
            [{"url": [{"link": record.source_url, "role": "SOURCE_PAGE"}]}]
        )
        if media:
            doc["associatedMedia"] = media

    body = (record.full_text or "").strip()
    if body:
        doc["text"] = {"ne": body}

    return doc, material_type
