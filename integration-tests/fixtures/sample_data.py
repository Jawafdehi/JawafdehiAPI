"""Canonical sample records shared across services.

Clean-slate IRI contract (2026-06): the platform join key is a schema.org
``@id`` **IRI**, NOT the old ``entity:<prefix>/<slug>`` string. See
``shared/jawafdehi_shared/entities/ids.py``:

  * entity    IRI: ``https://jawafdehi.org/entity/<prefix>/<slug>``   (NES authority)
  * material  IRI: ``https://jawafdehi.org/material/<source>/<ident>``(NGM docs)
  * case      IRI: ``https://jawafdehi.org/case/<slug>``             (Jawafdehi, minted at publish)
  * courtcase IRI: ``https://jawafdehi.org/courtcase/<court>/<case_number>`` (NGM)

These are intentionally storage-agnostic plain dicts usable to seed a stack or
to assert shape on live data with the same constants.
"""

from __future__ import annotations

# The canonical IRI authority+scheme for the platform (configurable per
# deployment via JAWAFDEHI_IRI_BASE, but constant within a platform).
IRI_BASE = "https://jawafdehi.org"

# The entity join key that threads through all three services, as a canonical
# @id IRI: https://jawafdehi.org/entity/<prefix>/<slug>.
SAMPLE_ENTITY_PREFIX = "person"
SAMPLE_ENTITY_SLUG = "ram-chandra-poudel"
SAMPLE_ENTITY_IRI = f"{IRI_BASE}/entity/{SAMPLE_ENTITY_PREFIX}/{SAMPLE_ENTITY_SLUG}"
# Back-compat alias (the field is now an IRI, not an ``entity:`` string).
SAMPLE_ENTITY_ID = SAMPLE_ENTITY_IRI


# --- NES: a person entity (schema.org JSON-LD doc, keyed by its @id IRI) -------
# NES stores entities as raw schema.org JSON-LD; the id field is ``@id`` (the
# canonical IRI), not ``id``. List shape stays ``{entities, total, limit, offset}``.
SAMPLE_NES_ENTITY: dict = {
    "@id": SAMPLE_ENTITY_IRI,
    "@type": "Person",
    "entity_prefix": SAMPLE_ENTITY_PREFIX,
    "slug": SAMPLE_ENTITY_SLUG,
    "name": {"en": "Ram Chandra Poudel", "ne": "रामचन्द्र पौडेल"},
    "tags": ["politician"],
}


# --- NGM: a court case (shape per GET /api/ngm/cases/ results[*]) -------------
# Path params for the sub-resource routes are (court, case_number). The
# synthesized court-case @id IRI is .../courtcase/<court>/<case_number>.
SAMPLE_NGM_COURT_IDENTIFIER = "supreme"
SAMPLE_NGM_CASE_NUMBER = "077-wo-0123"
SAMPLE_NGM_COURTCASE_IRI = (
    f"{IRI_BASE}/courtcase/{SAMPLE_NGM_COURT_IDENTIFIER}/{SAMPLE_NGM_CASE_NUMBER}"
)

SAMPLE_NGM_CASE: dict = {
    "court_identifier": SAMPLE_NGM_COURT_IDENTIFIER,
    "case_number": SAMPLE_NGM_CASE_NUMBER,
    "case_type": "writ",
    "status": "decided",
    "registration_date": "2021-03-15",  # AD ISO date per the read-plane contract
}


# --- NGM: a case party (shape per court_case_entities / .../entities/) --------
# ``nes_id`` is the resolution write-back; it MUST be a canonical entity @id IRI.
# It is null until the shared resolution service populates it.
SAMPLE_NGM_PARTY: dict = {
    "name": "Ram Chandra Poudel",
    "role": "petitioner",
    "nes_id": SAMPLE_ENTITY_IRI,
}

# Same party before resolution lands — the realistic empty-corpus state.
SAMPLE_NGM_PARTY_UNRESOLVED: dict = {
    "name": "Ram Chandra Poudel",
    "role": "petitioner",
    "nes_id": None,
}


# --- Jawafdehi: a published case @id IRI --------------------------------------
# Minted at PUBLISH from the case slug: https://jawafdehi.org/case/<slug>.
SAMPLE_CASE_SLUG = "case-077-WO-0123-ram-chandra-poudel"
SAMPLE_CASE_IRI = f"{IRI_BASE}/case/{SAMPLE_CASE_SLUG}"


# --- Document source ({link, role}) -------------------------------------------
# DocumentSource.url is a JSON list of {link, role} dicts; ``role`` is a
# SourceLinkRole (RAW, MARKDOWN, PERMALINK, SOURCE_PAGE, ALTERNATE). A missing
# role defaults to RAW (normalize_url_list). NGM's doc modality maps 1:1.
SOURCE_LINK_ROLES = {"RAW", "MARKDOWN", "PERMALINK", "SOURCE_PAGE", "ALTERNATE"}

SAMPLE_DOCUMENT_SOURCE: dict = {
    "source_type": "COURT_ORDER",
    "publication_date": "2022-06-10",
    "url": [
        {"link": "https://supremecourt.gov.np/cases/077-WO-0123/order.pdf", "role": "RAW"},
        {"link": "https://supremecourt.gov.np/cases/077-WO-0123", "role": "PERMALINK"},
    ],
    "related_entities": [SAMPLE_ENTITY_IRI],
}


__all__ = [
    "IRI_BASE",
    "SAMPLE_ENTITY_PREFIX",
    "SAMPLE_ENTITY_SLUG",
    "SAMPLE_ENTITY_IRI",
    "SAMPLE_ENTITY_ID",
    "SAMPLE_NES_ENTITY",
    "SAMPLE_NGM_COURT_IDENTIFIER",
    "SAMPLE_NGM_CASE_NUMBER",
    "SAMPLE_NGM_COURTCASE_IRI",
    "SAMPLE_NGM_CASE",
    "SAMPLE_NGM_PARTY",
    "SAMPLE_NGM_PARTY_UNRESOLVED",
    "SAMPLE_CASE_SLUG",
    "SAMPLE_CASE_IRI",
    "SOURCE_LINK_ROLES",
    "SAMPLE_DOCUMENT_SOURCE",
]
