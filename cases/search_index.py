"""Unified-search indexer for Jawafdehi cases (``jawafdehi-cases`` index).

Projects a ``Case`` into the common index doc. CASE-ONLY-PUBLISHED RULE (plan
§5, decision #6): only ``state == PUBLISHED`` cases enter the index — the search
index is all-public, so there is NO visibility/ACL field. ``index(case)`` upserts
when published and DELETES from the index when the case is not published (so a
case that leaves PUBLISHED is removed). ``should_index(case)`` exposes the rule.

Field mapping:
* ``iri``            ← ``Case.public_iri`` (``https://jawafdehi.org/case/<slug>``,
  minted at publish — the document ``_id``),
* ``type``           ← ``"Case"`` (+ the platform ``case_type`` in keywords),
* ``title_ne/en``    ← ``Case.title`` (script-bucketed; cases store one title),
* ``title_translit`` ← shared transliteration of the title,
* ``body``           ← ``description`` + ``key_allegations`` (joined),
* ``keywords``       ← ``tags`` (+ ``case_type``),
* ``identifiers``    ← the IRI, the slug, and the ``court_cases`` references,
* ``date``           ← ``case_start_date`` (else created date),
* ``raw``            ← a light serialized record (return-only).

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
from jawafdehi_shared.search.opensearch import CASE_INDEX, make_client

SOURCE_APP = "jawafdehi"
TYPE_TOKEN = "Case"


def should_index(case: Any) -> bool:
    """True iff the case belongs in the all-public index (state == PUBLISHED)."""
    # Compare against the value so we don't import CaseState at module load
    # (keeps this importable in pure-shaping contexts); CaseState.PUBLISHED == "PUBLISHED".
    return str(getattr(case, "state", "")) == "PUBLISHED"


def _case_iri(case: Any) -> str | None:
    """The case's public @id IRI (only set when PUBLISHED)."""
    return getattr(case, "public_iri", None)


def build_doc(case: Any) -> dict[str, Any]:
    """Map a published ``Case`` to the common index doc. Pure: no OpenSearch.

    The caller is responsible for the published gate; this shapes whatever it is
    given (used directly by tests for the doc shape)."""
    iri = _case_iri(case)
    title = getattr(case, "title", "") or ""
    title_ne, title_en = name_to_titles(title)

    body_parts: list[str] = []
    description = getattr(case, "description", None)
    if description and description.strip():
        body_parts.append(description.strip())
    for allegation in getattr(case, "key_allegations", None) or []:
        if isinstance(allegation, str) and allegation.strip():
            body_parts.append(allegation.strip())
    short = getattr(case, "short_description", None)
    if short and short.strip():
        body_parts.append(short.strip())
    body = "\n".join(body_parts) or None

    tags = [t for t in (getattr(case, "tags", None) or []) if isinstance(t, str)]
    keywords = list(tags)
    case_type = getattr(case, "case_type", None)
    if case_type:
        keywords.append(case_type)

    slug = getattr(case, "slug", None)
    identifiers: list[str] = [i for i in (iri, slug) if i]
    for ref in getattr(case, "court_cases", None) or []:
        if isinstance(ref, str) and ref and ref not in identifiers:
            identifiers.append(ref)

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
            "slug": slug,
            "case_type": case_type,
            "title": title,
            "tags": tags,
        },
    }
    # Promote case_type to a top-level keyword so the unified search can filter and
    # facet on it (it also stays in ``keywords`` and ``raw`` for text recall).
    if case_type:
        doc["case_type"] = case_type

    start = getattr(case, "case_start_date", None)
    created = getattr(case, "created_at", None)
    if start is not None:
        doc["date"] = start.isoformat() if hasattr(start, "isoformat") else str(start)
    elif created is not None and hasattr(created, "date"):
        doc["date"] = created.date().isoformat()
    if created is not None:
        doc["created_at"] = created.isoformat() if hasattr(created, "isoformat") else created
    updated = getattr(case, "updated_at", None)
    if updated is not None:
        doc["updated_at"] = updated.isoformat() if hasattr(updated, "isoformat") else updated
    return doc


@best_effort("index case")
def index(case: Any, *, client=None) -> None:
    """Upsert a PUBLISHED case; otherwise remove it from the index (best-effort).

    The case-only-published rule: publishing indexes; any non-PUBLISHED state
    (draft/in-review/closed) deletes the doc so it never appears in search."""
    cl = client or make_client()
    if should_index(case):
        upsert_doc(cl, CASE_INDEX, build_doc(case))
    else:
        delete(case, client=cl)


@best_effort("delete case")
def delete(case: Any, *, client=None) -> None:
    """Delete the case's doc from ``jawafdehi-cases`` (best-effort).

    Uses the public IRI when the case is (or was) published; falls back to
    building one from the slug so a case that has just LEFT published state can
    still be evicted even though ``public_iri`` now returns ``None``."""
    iri = _case_iri(case)
    if not iri:
        slug = getattr(case, "slug", None)
        if slug:
            from jawafdehi_shared.entities.ids import build_case_iri

            try:
                iri = build_case_iri(slug)
            except ValueError:
                iri = None
    if iri:
        delete_doc(client or make_client(), CASE_INDEX, iri)
