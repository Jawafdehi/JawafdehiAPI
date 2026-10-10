"""In-process resolution seam for NES entity display details.

NES (Nepal Entity Service) is the single source of truth for entities.
Jawafdehi stores only the canonical entity @id IRI
(``https://jawafdehi.org/entity/<prefix>/<slug>``) as a join key on the
``CaseEntityRelationship`` bind and in ``DocumentSource.related_entities``; it
never stores entity data (names/type).

This module is the *seam* that turns those IRIs into display details. NES keys
its ``StoredEntity`` rows by the same @id IRI (the ``iri`` PK), so resolution is
a direct ``iri``-lookup against the NES table. The service consolidation that would
let Jawafdehi share the NES database in-process is not done yet, so resolution is
best-effort:

* If the NES app models are importable in this process
  (``entities.models.StoredEntity`` — same physical ``entities``
  table NES owns), we read name/type directly from the stored schema.org
  JSON-LD document. No cross-DB foreign key is involved; this is an
  IRI -> document lookup (``StoredEntity.iri`` is the PK) against whatever DB
  the StoredEntity model is routed to.
* Otherwise we fall back to a documented stub that returns a minimal record
  (``{"nes_id": ...}`` with the other fields ``None``) for every requested id,
  so callers can render the id without crashing. Wiring this seam to the NES
  HTTP API (``settings.NES_API_URL``) is the planned follow-up.

The function is intentionally typed and total: it always returns one entry per
requested id, so callers can do ``resolve_entities(ids)[nes_id]`` safely.
"""

from __future__ import annotations

import logging
from typing import Optional, TypedDict

logger = logging.getLogger(__name__)


class ResolvedEntity(TypedDict):
    """Display details for a single NES entity, resolved from its id.

    ``display_name`` is the single best name in ANY language (see
    :func:`_primary_name_from_document`); ``name_en``/``name_ne`` are the two
    scripts kept apart, which is what a bilingual surface needs — it renders the
    name in the reader's language with the other script beneath it, and
    ``display_name`` alone cannot say which language it ended up being.

    ``is_live`` mirrors ``StoredEntity.is_deleted`` inverted. It exists because
    this resolver does NOT filter soft-deleted rows while the HTTP read plane
    (``EntityRepository._live``) does — see the note in :func:`resolve_entities`.
    """

    nes_id: str
    display_name: Optional[str]
    entity_type: Optional[str]
    name_en: Optional[str]
    name_ne: Optional[str]
    image: Optional[str]
    is_live: bool


def _stub_entity(nes_id: str) -> ResolvedEntity:
    """Minimal record used when NES is not resolvable in-process."""
    return {
        "nes_id": nes_id,
        "display_name": None,
        "entity_type": None,
        "name_en": None,
        "name_ne": None,
        "image": None,
        # An unresolved id is not a RETIRED id. Defaulting to True keeps
        # "unresolved" and "soft-deleted" distinguishable downstream; callers key
        # the identity fields off `name_*`/`image` being None either way.
        "is_live": True,
    }


def _primary_name_from_document(data: dict) -> Optional[str]:
    """Extract a human-readable name from a stored NES schema.org JSON-LD document.

    The stored doc is schema.org JSON-LD: ``name`` is either a plain string or a
    language map ``{"en": "...", "ne": "..."}`` (the bilingual representation).
    Prefers English, then Nepali, then any non-empty language value.
    """
    if not isinstance(data, dict):
        return None
    name = data.get("name")
    if isinstance(name, str):
        return name.strip() or None
    if isinstance(name, dict):
        # Language map: prefer en, then ne, then any non-empty string value.
        for lang in ("en", "ne"):
            value = name.get(lang)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for value in name.values():
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _scripts_from_document(data: dict) -> tuple[Optional[str], Optional[str]]:
    """Split a stored document's ``name`` into its (English, Nepali) spellings.

    Unlike :func:`_primary_name_from_document` this does NOT fall back between
    languages — a surface that shows "name, and its other-script spelling" has to
    know which it has, and a fallback would print the same string twice.

    A bare-string ``name`` counts as English with no Nepali, matching the SPA's
    ``jsonLdToEntity`` so the two sides of this contract agree; the pair is
    ``(None, None)`` for anything else.
    """
    if not isinstance(data, dict):
        return None, None
    name = data.get("name")
    if isinstance(name, str):
        return (name.strip() or None), None
    if isinstance(name, dict):

        def _one(lang: str) -> Optional[str]:
            value = name.get(lang)
            return value.strip() or None if isinstance(value, str) else None

        return _one("en"), _one("ne")
    return None, None


def _image_from_document(data: dict) -> Optional[str]:
    """The entity's picture URL, or None.

    schema.org ``image`` is a URL string, an ImageObject (``url``/``contentUrl``),
    or an array of either. The SPA's ``toPictures`` maps all of them to one
    ``pictures[]`` of kind ``full`` and its consumers then read the first, so
    returning the first usable URL here is the same choice made one layer earlier.
    """
    if not isinstance(data, dict):
        return None

    def _url_of(value) -> Optional[str]:
        if isinstance(value, str):
            return value.strip() or None
        if isinstance(value, dict):
            for key in ("url", "contentUrl"):
                candidate = value.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    return candidate.strip()
        return None

    image = data.get("image")
    for item in image if isinstance(image, list) else [image]:
        url = _url_of(item)
        if url:
            return url
    return None


def resolve_entities(nes_ids) -> dict[str, ResolvedEntity]:
    """Resolve canonical NES entity @id IRIs to display details.

    Args:
        nes_ids: An iterable of canonical NES entity @id IRI strings
            (``https://jawafdehi.org/entity/<prefix>/<slug>``). Duplicates and
            falsy values are ignored. NES keys its rows by the same IRI, so this
            is a direct ``StoredEntity.id``-IRI lookup.

    Returns:
        A dict mapping every requested (non-empty) id to a ``ResolvedEntity``.
        Ids that NES cannot resolve (or when NES is unavailable in-process) map
        to a stub record with ``display_name``/``entity_type`` set to ``None``.
    """
    ids = [nid for nid in dict.fromkeys(nes_ids) if nid]
    if not ids:
        return {}

    resolved: dict[str, ResolvedEntity] = {nid: _stub_entity(nid) for nid in ids}

    try:
        # NES app models live in the standalone NES service. The package may be
        # IMPORTABLE in this process (monorepo) yet the model is not usable until
        # the service consolidation adds `entities` to INSTALLED_APPS and
        # routes its DB — touching the class before then raises RuntimeError
        # ("doesn't declare an explicit app_label / isn't in INSTALLED_APPS").
        # Treat both "not importable" and "not registered" as "NES unavailable
        # in-process" → stubs. (Narrower than a blanket except: a genuine query
        # error below still surfaces via its own handler.)
        from django.apps import apps as _django_apps

        if not _django_apps.is_installed("entities"):
            raise ModuleNotFoundError("entities not in INSTALLED_APPS")
        from entities.models import StoredEntity
    except (ImportError, RuntimeError):  # pragma: no cover - stub fallback path
        logger.debug(
            "NES models not available in-process (not importable or not an "
            "installed app); returning stub entity records for %d id(s).",
            len(ids),
        )
        return resolved

    try:
        # StoredEntity is keyed by `iri` (the canonical @id), not `id`.
        #
        # NOTE: this deliberately does NOT filter `is_deleted`, which is a
        # divergence from the HTTP read plane (`EntityRepository._live()` does).
        # Narrowing it here would change `display_name` on the case LIST payload
        # and in the search-index card for every case bound to a retired entity —
        # a silent, reindex-requiring change. The divergence is surfaced as
        # `is_live` instead, so a caller that needs to match the HTTP plane's
        # behaviour can, without moving this query's goalposts.
        for stored in StoredEntity.objects.filter(iri__in=ids):
            data = stored.data if isinstance(stored.data, dict) else {}
            name_en, name_ne = _scripts_from_document(data)
            resolved[stored.iri] = {
                "nes_id": stored.iri,
                "display_name": _primary_name_from_document(data),
                "entity_type": stored.entity_type or None,
                "name_en": name_en,
                "name_ne": name_ne,
                "image": _image_from_document(data),
                "is_live": not stored.is_deleted,
            }
    except Exception:  # pragma: no cover  # noqa: BLE001 - defensive: DB not routed/migrated
        logger.warning(
            "Failed to resolve NES entities in-process; returning stubs.",
            exc_info=True,
        )

    return resolved


def build_entity_binds(relationships, resolved, *, include_identity: bool = False) -> list[dict]:
    """Shape ``CaseEntityRelationship`` rows + resolved NES details into the entity
    binds used by BOTH the API (``CaseSerializer.get_entities``) and the search
    index card. One definition so the two consumers can't drift.

    ``resolved`` is a :func:`resolve_entities` result (``nes_id -> ResolvedEntity``);
    a missing/unresolved id yields ``None`` name/type rather than raising, so this
    is safe on the best-effort indexing path as well as the API path.

    ``include_identity`` adds ``name`` (``{"en", "ne"}``) and ``image``, which is
    what lets a case DETAIL page render its parties without fetching
    ``/api/entities/<iri>`` once per party — the fan-out that exhausted the
    database's connection ceiling (see the 2026-09-29 COE). It defaults to OFF,
    and that default is load-bearing for two separate consumers:

    * the case LIST payload is already 782 KiB–2.9 MB per page, and this would add
      ~120 bytes per bind across ~3,100 of them;
    * ``cases.search_index`` builds the indexed card from this same function, so
      flipping the default would change every stored document and silently require
      a reindex.

    Identity is emitted only for LIVE entities. A soft-deleted (merged-away) row
    still resolves here — ``resolve_entities`` does not filter it — but
    ``GET /api/entities/<iri>`` 404s on it, so the page shows the fallback name and
    a generic glyph today. Gating on ``is_live`` reproduces exactly that, rather
    than quietly promoting a retired entity's full bilingual name and photo onto
    the page. Following the ``merged_into`` pointer to the survivor would be a
    product change, and belongs in its own.

    Per-entity ``notes`` are PUBLIC. They are the defendant's role line — "तत्कालीन
    प्रधानाध्यापक — मुख्य प्रतिवादी" — written to be read next to the name on the
    public case page, and the extractor that fills them (``accused_verdicts``, added
    in #474) caps the role at 90 chars for exactly that purpose. They used to be
    withheld from anonymous callers as internal casework content under BB-04, which
    is a rule about the case-level ``Case.notes`` field; the per-bind column shares
    only its name. That gate is gone: the field renders unconditionally in the public
    entity chips, so gating it here made a public page's content depend on whether
    the viewer's OIDC token happened to be attached.

    ``Case.notes`` — the case-level internal field — is a DIFFERENT column and stays
    casework-gated in ``CaseSerializer.get_notes``. Do not conflate the two."""

    def _bind(rel) -> dict:
        record = resolved.get(rel.nes_id) or {}
        bind = {
            "nes_id": rel.nes_id,
            "display_name": record.get("display_name"),
            "entity_type": record.get("entity_type"),
            "type": rel.relationship_type,
            "outcome": rel.outcome,
            "notes": rel.notes or "",
        }
        if include_identity:
            live = record.get("is_live", True)
            bind["name"] = {
                "en": record.get("name_en") if live else None,
                "ne": record.get("name_ne") if live else None,
            }
            bind["image"] = record.get("image") if live else None
        return bind

    return [_bind(rel) for rel in relationships]
