"""Unified-search indexer for NGM court cases (``ngm-courtcases`` index).

A ``CourtCase`` has NO schema.org ``name`` language map — its natural key is the
composite ``(case_number, court)`` and its synthesized ``@id`` is
``https://jawafdehi.org/courtcase/<court>/<case_number>`` (``CourtCase.iri``).

CONTRACT GAP / how the bilingual title is built (no language-map name exists):
* ``title_ne`` ← the ``case_number`` (e.g. ``081-CR-0081``) — the human handle a
  court reader recognises; it's script-neutral so it's the safe primary title.
* ``title_en`` ← the court's English full name + case_number when the ``Court``
  row resolves an English name; else ``None``.
* party names (``plaintiff``/``defendant`` + each ``CaseEntity.name``, mostly
  Devanagari) go into ``body`` and ``keywords`` so a party-name query matches.
* ``identifiers`` ← the IRI, ``case_number``, ``court``, and any resolved party
  ``nes_id`` IRIs.
* Bikram Sambat registration date is carried verbatim into ``date_bs``.

Best-effort: an OpenSearch error is logged and swallowed.
"""

from __future__ import annotations

from typing import Any

from jawafdehi_shared.search.indexing import (
    best_effort,
    delete_doc,
    title_translit,
    upsert_doc,
)
from jawafdehi_shared.search.opensearch import COURTCASE_INDEX, make_client
from courts.normalize import is_verdict_sentinel

SOURCE_APP = "ngm"
TYPE_TOKEN = "jawafdehi:CourtCase"


def _party_names(case: Any) -> list[str]:
    """Collect party display names: the case ``plaintiff``/``defendant`` strings
    plus every related ``CaseEntity.name`` (defensive — works on a bare instance
    in a pure-shaping test, returning just the case-level parties)."""
    names: list[str] = []
    for value in (getattr(case, "plaintiff", None), getattr(case, "defendant", None)):
        if isinstance(value, str) and value.strip():
            names.append(value.strip())
    try:
        from courts.models import CaseEntity

        rows = CaseEntity.objects.filter(
            court_id=case.court_id, case_number=case.case_number
        ).values_list("name", flat=True)
        for name in rows:
            if isinstance(name, str) and name.strip():
                names.append(name.strip())
    except Exception:  # noqa: BLE001 — shaping must not hard-fail on DB state.
        pass
    # Deduplicate, preserve order.
    seen: set[str] = set()
    out: list[str] = []
    for n in names:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _party_iris(case: Any) -> list[str]:
    """Resolved (non-null) ``nes_id`` IRIs: the case-level one + party rows."""
    iris: list[str] = []
    case_nes = getattr(case, "nes_id", None)
    if case_nes:
        iris.append(case_nes)
    try:
        from courts.models import CaseEntity

        rows = (
            CaseEntity.objects.filter(
                court_id=case.court_id, case_number=case.case_number
            )
            .exclude(nes_id__isnull=True)
            .exclude(nes_id="")
            .values_list("nes_id", flat=True)
        )
        iris.extend(rows)
    except Exception:  # noqa: BLE001
        pass
    seen: set[str] = set()
    out: list[str] = []
    for i in iris:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


def _court_english_name(case: Any) -> str | None:
    """Resolve the court's English full name, if the ``Court`` row has one."""
    court = getattr(case, "court", None)
    name = getattr(court, "full_name_english", None)
    if isinstance(name, str) and name.strip():
        return name.strip()
    return None


def build_doc(obj: Any) -> dict[str, Any]:
    """Map a ``CourtCase`` to the common index doc. Pure: no OpenSearch calls.

    (``_party_names``/``_party_iris`` lazily query ``CaseEntity`` but degrade to
    the case-level parties when no DB is available.)"""
    iri = obj.iri  # synthesized courtcase IRI (property)
    case_number = getattr(obj, "case_number", "") or ""
    court_id = getattr(obj, "court_id", "") or ""

    # title_ne: the case_number is the recognised handle (script-neutral).
    title_ne = case_number or None
    # title_en: court English name + case_number, when resolvable.
    court_en = _court_english_name(obj)
    title_en = f"{court_en} {case_number}".strip() if court_en else None

    parties = _party_names(obj)
    body = " · ".join(parties) or None

    keywords: list[str] = list(parties)
    case_type = getattr(obj, "case_type", None)
    if case_type:
        keywords.append(case_type)

    identifiers: list[str] = [iri, case_number, court_id]
    identifiers = [i for i in identifiers if i]
    for nes_iri in _party_iris(obj):
        if nes_iri not in identifiers:
            identifiers.append(nes_iri)

    doc: dict[str, Any] = {
        "iri": iri,
        "type": TYPE_TOKEN,
        "source_app": SOURCE_APP,
        "title_ne": title_ne,
        "title_en": title_en,
        "title_translit": title_translit(title_ne, title_en),
        "body": body,
        "keywords": keywords,
        "identifiers": identifiers,
        "raw": {
            "@id": iri,
            "case_number": case_number,
            "court": court_id,
            "case_type": case_type,
            "case_status": getattr(obj, "case_status", None),
            "plaintiff": getattr(obj, "plaintiff", None),
            "defendant": getattr(obj, "defendant", None),
        },
    }
    # Promote case_type to a top-level keyword so the unified search can filter and
    # facet on it (it also stays in ``keywords`` and ``raw`` for text recall).
    # NORMALIZE the facet token to upper-case: court-case types are free text from
    # scrapers with inconsistent casing ("CORRUPTION" vs "Corruption"), which would
    # otherwise split one concept into duplicate facet buckets. The verbatim value
    # is preserved in ``raw.case_type`` for display; the label layer upper-cases too.
    if case_type:
        doc["case_type"] = case_type.upper()

    reg_ad = getattr(obj, "registration_date_ad", None)
    if reg_ad is not None:
        doc["date"] = reg_ad.isoformat() if hasattr(reg_ad, "isoformat") else str(reg_ad)
    reg_bs = getattr(obj, "registration_date_bs", None)
    if reg_bs:
        doc["date_bs"] = str(reg_bs)

    # Re-added legacy fields (spec 01 §5a): verdict / subject / status become
    # queryable facets + search body. The verdict_date_bs sentinel (`**** ** **`)
    # is NEVER surfaced as data (§6.1): dropped here; the physical column is left
    # as the scraper wrote it.
    case_subject = getattr(obj, "case_subject", None)
    if case_subject:
        keywords.append(case_subject)  # same list object as doc["keywords"]
        doc["body"] = f"{doc['body']} · {case_subject}" if doc.get("body") else case_subject
    status = getattr(obj, "status", None)
    if status:
        doc["status"] = status
    verdict_type = getattr(obj, "verdict_type", None)
    if verdict_type:
        doc["verdict_type"] = verdict_type
    verdict_ad = getattr(obj, "verdict_date_ad", None)
    if verdict_ad is not None:
        doc["verdict_date"] = (
            verdict_ad.isoformat() if hasattr(verdict_ad, "isoformat") else str(verdict_ad)
        )
    verdict_bs = getattr(obj, "verdict_date_bs", None)
    verdict_bs = None if is_verdict_sentinel(verdict_bs) else str(verdict_bs)
    if verdict_bs:
        doc["verdict_date_bs"] = verdict_bs
    doc["raw"].update(
        {
            "status": status,
            "verdict_type": verdict_type,
            "verdict_judge": getattr(obj, "verdict_judge", None),
            "case_subject": case_subject,
            "verdict_date_ad": doc.get("verdict_date"),
            "verdict_date_bs": verdict_bs,
        }
    )

    created = getattr(obj, "created_at", None)
    updated = getattr(obj, "updated_at", None)
    if created is not None:
        doc["created_at"] = created.isoformat() if hasattr(created, "isoformat") else created
    if updated is not None:
        doc["updated_at"] = updated.isoformat() if hasattr(updated, "isoformat") else updated
    return doc


@best_effort("index courtcase")
def index(obj: Any, *, client=None) -> None:
    """Upsert the court case's doc into ``ngm-courtcases`` (best-effort)."""
    upsert_doc(client or make_client(), COURTCASE_INDEX, build_doc(obj))


@best_effort("delete courtcase")
def delete(obj: Any, *, client=None) -> None:
    """Delete the court case's doc from ``ngm-courtcases`` (best-effort)."""
    iri = obj.iri
    if iri:
        delete_doc(client or make_client(), COURTCASE_INDEX, iri)
