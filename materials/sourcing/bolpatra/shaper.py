"""Shape a parsed bolpatra e-GP tender into Material JSON-LD — a public
procurement notice (PPMO/bolpatra सूचना).

Pure + DB-free: takes a :class:`~materials.sourcing.bolpatra.parse.ParsedTender`
and returns ``(jsonld_doc, material_type)`` ready to POST to ``/api/materials/``
(the crawler in :mod:`materials.sourcing.bolpatra.crawl` is the API client). Lives
beside its crawler under ``materials/sourcing/bolpatra/`` — the home for
external-source shapers; the generic Material JSON-LD contract
(``MATERIAL_CONTEXT``, ``type_for``, ``MaterialType``) stays in ``materials.jsonld``.

Mirrors the NKP precedent shaper's shape/idioms.
"""

from __future__ import annotations

from typing import Any

from materials.jsonld import MATERIAL_CONTEXT, MaterialType, type_for
from materials.sourcing.bolpatra.parse import ParsedTender

#: IRI ``source`` segment for a bolpatra procurement notice
#: (``/material/bolpatra/<tenderId>``).
BOLPATRA_SOURCE = "bolpatra"

#: Provenance authority (host of the e-GP portal) — the publisher key.
BOLPATRA_AUTHORITY = "bolpatra.gov.np"


def _ad_iso(value: str | None) -> str | None:
    """Best-effort ``DD-MM-YYYY[ HH:MM]`` (e-GP's format) → ISO ``YYYY-MM-DD``.

    e-GP prints Gregorian dates as ``30-07-2026 16:00``. Returns ``None`` on any
    unparseable input (a date is sort/filter metadata, never a hard precondition).
    """
    if not value:
        return None
    head = str(value).strip().split()[0]  # drop the time component
    parts = head.split("-")
    if len(parts) != 3:
        return None
    day, month, year = parts
    if len(year) != 4 or not (day.isdigit() and month.isdigit() and year.isdigit()):
        return None
    return f"{year}-{int(month):02d}-{int(day):02d}"


def tender_to_jsonld(tender: ParsedTender) -> tuple[dict[str, Any], str]:
    """Shape one parsed tender → ``(jsonld_doc, material_type)``.

    The procurement notice maps to ``CreativeWork`` + ``jawafdehi:
    ProcurementNotice`` (see ``materials.jsonld.MATERIAL_TYPES``). The e-GP detail
    page is the ``url``; the notice-number/entity/category/method and the bid
    schedule ride as ``jawafdehi:`` extension properties. ``datePublished`` is the
    Gregorian notice date so notices are date-orderable in unified search.

    Pure function (no DB): the crawler is the API client. The ``@id`` is minted
    lazily by the crawler via ``build_material_iri(BOLPATRA_SOURCE, tender_id)`` so
    this stays import-light and unit-testable without the ids module; callers that
    want the doc pre-keyed pass through the crawler.
    """
    material_type = MaterialType.PROCUREMENT_NOTICE
    schema_type, additional_type = type_for(material_type)

    source_url = (
        f"https://bolpatra.gov.np/egp/getTenderDetails?tenderId={tender.tender_id}"
    )
    name = tender.project_name or tender.notice_number or f"Tender {tender.tender_id}"

    doc: dict[str, Any] = {
        "@context": MATERIAL_CONTEXT,
        "@type": schema_type,
        "additionalType": additional_type,
        "name": {"en": name},
        "identifier": str(tender.notice_number or tender.tender_id),
        "url": source_url,
        "isAccessibleForFree": True,
        # The procuring entity publishes the notice through PPMO's e-GP.
        "publisher": {
            "@type": "GovernmentOrganization",
            "name": {"en": "Public Procurement Monitoring Office"},
        },
        "sources": [{"url": source_url, "authority": BOLPATRA_AUTHORITY}],
    }

    # Procurement identity + classification as jawafdehi: extension properties.
    for field_value, key in (
        (tender.notice_number, "jawafdehi:noticeNumber"),
        (tender.public_entity, "jawafdehi:procuringEntity"),
        (tender.procurement_category, "jawafdehi:procurementCategory"),
        (tender.procurement_method, "jawafdehi:procurementMethod"),
        (tender.current_status, "jawafdehi:currentStatus"),
        (tender.source_of_funds, "jawafdehi:sourceOfFunds"),
    ):
        if field_value:
            doc[key] = field_value

    if tender.description:
        doc["description"] = {"en": tender.description}

    # Notice publication date drives search date sort/filter.
    published_ad = _ad_iso(tender.publication_date)
    if published_ad:
        doc["datePublished"] = published_ad
    if tender.publication_date:
        doc["jawafdehi:noticePublicationDate"] = tender.publication_date

    # Bid schedule — carried verbatim (e-GP's DD-MM-YYYY HH:MM) as jawafdehi: props.
    for field_value, key in (
        (tender.submission_deadline, "jawafdehi:bidSubmissionDeadline"),
        (tender.opening_date, "jawafdehi:bidOpeningDate"),
        (tender.prebid_meeting_date, "jawafdehi:preBidMeetingDate"),
    ):
        if field_value:
            doc[key] = field_value

    return doc, material_type
