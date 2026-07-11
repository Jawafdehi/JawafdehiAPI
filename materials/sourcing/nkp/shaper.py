"""Shape a scraped Nepal Law Journal (नेपाल कानून पत्रिका / NKP) decision into
Material JSON-LD — a published law-journal precedent (नजिर).

Pure + DB-free: takes the scraper's flat ``NkpDecisionItem`` dict, returns
``(jsonld_doc, material_type)`` ready to POST to ``/api/materials/`` (the crawler
in :mod:`materials.sourcing.nkp.crawl` is the API client). Lives beside its
crawler under ``materials/sourcing/nkp/`` — the home for external-source shapers
(see ``materials/sourcing/README.md``); the generic Material JSON-LD contract
(``MATERIAL_CONTEXT``, ``type_for``, ``MaterialType``) stays in ``materials.jsonld``.
"""

from __future__ import annotations

import hashlib
from typing import Any

from jawafdehi_shared.dates import bs_to_ad_iso
from jawafdehi_shared.entities.ids import build_material_iri

from materials.jsonld import (
    MATERIAL_CONTEXT,
    MaterialType,
    media_objects_from_document_sources,
    type_for,
)

#: IRI ``source`` segment for an NKP precedent material (``/material/nkp/<ident>``).
NKP_SOURCE = "nkp"


def nkp_precedent_ident(decision: dict[str, Any]) -> str:
    """Stable, unique ident for an NKP precedent's ``@id``.

    Keyed on the site's own ``detail_id`` (the ``/full_detail/{id}`` row id) — the
    only per-decision stable primary key on nkp.gov.np. The citable decision
    number (निर्णय नं.) is NOT unique: ~57 numbers are reused across the corpus
    (a fresh sequence per volume/era), so keying the ``@id`` on it would upsert
    genuinely-distinct precedents onto one row and silently lose them.

    Fallback: if a record has no ``detail_id`` (none do in the current corpus —
    purely defensive), mint an artificial ``jawa-<hash>`` ident deterministically
    from the decision's identifying fields, so a re-scrape reproduces the same
    ``@id`` (idempotent upsert) rather than duplicating the row.
    """
    detail_id = str(decision.get("detail_id") or "").strip().lower()
    if detail_id:
        return detail_id
    basis = "|".join(
        str(decision.get(k) or "")
        for k in ("decision_no", "year_bs", "month", "case_name", "source_url")
    )
    digest = hashlib.sha1(basis.encode("utf-8")).hexdigest()[:12]
    return f"jawa-{digest}"


def nkp_precedent_material_iri(ident: str) -> str:
    """Canonical material ``@id`` IRI for an NKP precedent from its stable ident.

    ``ident`` is :func:`nkp_precedent_ident` (the site ``detail_id`` or a
    ``jawa-<hash>`` fallback) — e.g. ``8880`` → ``https://<base>/material/nkp/8880``.
    """
    return build_material_iri(NKP_SOURCE, str(ident).strip().lower())


def nkp_decision_to_jsonld(decision: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Shape one scraped NKP decision → ``(jsonld_doc, material_type)``.

    The published precedent maps to ``CreativeWork`` + ``jawafdehi:Precedent``.
    The decision's own ``source_url`` (the nkp.gov.np page) is the ``url``; a
    ``fallback_pdf_url`` (when the HTML body was an upload-error note) rides as an
    ALTERNATE ``associatedMedia`` link. The full Unicode judgment text lands in
    the language-tagged ``text`` field so it is search-indexable without OCR.

    Pure function (no DB): takes the scraper's flat dict, returns the JSON-LD doc
    plus its ``material_type`` (the crawler is the API client).
    """
    material_type = MaterialType.PRECEDENT
    # @id keys on the site's stable detail_id (see nkp_precedent_ident); the human
    # decision number (निर्णय नं.) is carried as ``identifier`` but is NOT unique.
    decision_no = decision.get("decision_no") or decision.get("detail_id")
    iri = nkp_precedent_material_iri(nkp_precedent_ident(decision))
    schema_type, additional_type = type_for(material_type)

    name = decision.get("title") or f"निर्णय नं. {decision_no}"
    doc: dict[str, Any] = {
        "@context": MATERIAL_CONTEXT,
        "@type": schema_type,
        "@id": iri,
        "name": {"ne": name},
        "additionalType": additional_type,
        "inLanguage": "ne",
        "isAccessibleForFree": True,
        "identifier": str(decision_no),
        "url": decision.get("source_url"),
        # Publisher: the Supreme Court of Nepal publishes the journal.
        "publisher": {"@type": "GovernmentOrganization", "name": {"ne": "सर्वोच्च अदालत"}},
        "isPartOf": {
            "@type": "Periodical",
            "name": {"ne": "नेपाल कानून पत्रिका"},
        },
    }

    # Journal coordinates + case identity as jawafdehi: extension properties.
    for field_name, key in (
        ("case_number", "jawafdehi:caseNumber"),
        ("case_name", "jawafdehi:caseSubject"),
        ("court", "jawafdehi:court"),
        ("bench", "jawafdehi:bench"),
        ("volume", "jawafdehi:journalVolume"),
        ("year_bs", "jawafdehi:journalYearBS"),
        ("month", "jawafdehi:journalMonth"),
        ("issue", "jawafdehi:journalIssue"),
    ):
        val = decision.get(field_name)
        if val:
            doc[key] = val

    date_bs = decision.get("decision_date_bs")
    if date_bs:
        # The decision date drives search date sort/filter. The unified-search
        # indexer reads ``jawafdehi:publicationDateBS`` for its BS ``date_bs`` and
        # ``datePublished`` for the Gregorian ``date``; emit BOTH (plus the
        # descriptive ``decisionDateBS``) so precedents are date-orderable — not
        # only ``jawafdehi:decisionDateBS``, which the indexer does not read.
        doc["jawafdehi:decisionDateBS"] = date_bs
        doc["jawafdehi:publicationDateBS"] = date_bs
        ad = bs_to_ad_iso(date_bs)
        if ad:
            doc["datePublished"] = ad
    if decision.get("judges"):
        doc["jawafdehi:judges"] = decision["judges"]
    if decision.get("referenced_laws"):
        doc["jawafdehi:referencedLaws"] = decision["referenced_laws"]

    # Headnotes (सूत्र) as description; full judgment text as searchable `text`.
    headnotes = decision.get("headnotes") or []
    if headnotes:
        doc["description"] = {
            "ne": " ".join(h.get("text", "") for h in headnotes if h.get("text"))[:2000]
        }
    if decision.get("full_text"):
        doc["text"] = {"ne": decision["full_text"]}

    # When the HTML body was an upload-error note, the scanned issue PDF on
    # supremecourt.gov.np is the recoverable source — carry it as an ALTERNATE
    # associatedMedia so the link survives on the material doc itself (the write
    # goes through the single-material API, which stores the doc verbatim).
    fallback_pdf = decision.get("fallback_pdf_url")
    if fallback_pdf:
        doc["associatedMedia"] = media_objects_from_document_sources(
            [{"url": [{"link": fallback_pdf, "role": "ALTERNATE"}]}]
        )

    return doc, material_type
