"""In-process resolution seam for NGM Material display details.

NGM is the single source of truth for documents ("materials"). After the
sources→materials fold (ADR: cases own no documents), a Jawafdehi case stores
only the canonical material @id IRI (``https://jawafdehi.org/material/<source>/<ident>``)
as a join key on the ``CaseMaterialReference`` bind — it never stores document
data (title/type/links).

This module is the *seam* that turns those IRIs into display details, mirroring
``cases.services.nes_resolver.resolve_entities`` for materials. Unlike NES,
``Material`` lives in this same Django project (the ``materials`` app is an
installed app routed to the ``ngm`` DB), so resolution is a direct in-process
``iri``-lookup — no HTTP, no cross-DB FK. It stays defensive (app-not-installed /
DB-not-routed → stub records) so pure-shaping tests and degraded environments do
not crash.

The function is total: it always returns one entry per requested id, so callers
can do ``resolve_materials(iris)[material_iri]`` safely.
"""

from __future__ import annotations

import logging
from typing import Any, Optional, TypedDict

logger = logging.getLogger(__name__)


class ResolvedLink(TypedDict):
    """A single roled media link projected from a Material's associatedMedia."""

    link: str
    role: str


class ResolvedMaterial(TypedDict):
    """Display details for a single NGM material, resolved from its @id IRI."""

    material_iri: str
    display_name: Optional[str]
    material_type: Optional[str]
    urls: list[ResolvedLink]


def _stub_material(material_iri: str) -> ResolvedMaterial:
    """Minimal record used when a material is unresolvable."""
    return {
        "material_iri": material_iri,
        "display_name": None,
        "material_type": None,
        "urls": [],
    }


def _primary_name_from_document(data: dict) -> Optional[str]:
    """Human-readable name from a stored material JSON-LD doc.

    ``name`` is a plain string or a language map ``{"en": ..., "ne": ...}``.
    Prefers English, then Nepali, then any non-empty language value. (Same
    convention as the NES entity resolver.)
    """
    if not isinstance(data, dict):
        return None
    name = data.get("name")
    if isinstance(name, str):
        return name.strip() or None
    if isinstance(name, dict):
        for lang in ("en", "ne"):
            value = name.get(lang)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for value in name.values():
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _links_from_document(data: dict) -> list[ResolvedLink]:
    """Project a material's ``associatedMedia`` MediaObjects to roled links.

    Inverse of ``materials.jsonld.media_objects_from_document_sources``: each
    MediaObject carries ``contentUrl`` + ``jawafdehi:linkRole``.
    """
    if not isinstance(data, dict):
        return []
    media = data.get("associatedMedia")
    if not isinstance(media, list):
        return []
    links: list[ResolvedLink] = []
    for mo in media:
        if not isinstance(mo, dict):
            continue
        url = mo.get("contentUrl")
        if not isinstance(url, str) or not url.strip():
            continue
        role = mo.get("jawafdehi:linkRole") or mo.get("role") or "RAW"
        links.append({"link": url.strip(), "role": str(role)})
    return links


def _derive_court_case_document(material_iri: str) -> Optional[dict]:
    """Rebuild a court-case material's JSON-LD from the court tables.

    A court-case material (``/material/court/<court>.<case_number>``) by design
    almost never has a stored ``Material`` row: the relational ``courts`` tables
    are the system of record and the material is a derived projection, so it is
    rebuilt on the fly at read time rather than materialized at ingest. This
    mirrors the
    court-case fallback in ``materials.views._resolve_material`` but stays in the
    service layer (it depends on ``courts``/``materials.jsonld``, not the
    materials *views* module). Returns ``None`` for any non-court IRI or when the
    referenced court case does not exist.
    """
    try:
        from courts.models import CourtCase
        from jawafdehi_shared.entities.ids import parse_material_iri
        from materials import jsonld as materials_jsonld

        parsed = parse_material_iri(material_iri)
        if parsed.source != materials_jsonld.COURT_SOURCE or "." not in parsed.ident:
            return None
        court_identifier, _, case_number = parsed.ident.partition(".")
        # The ident is lowercased in the IRI; the stored case_number is uppercased.
        case = CourtCase.objects.filter(
            court_id=court_identifier,
            case_number__iexact=case_number,
            is_deleted=False,
        ).first()
        if case is None:
            return None
        return materials_jsonld.court_case_to_jsonld(case)
    except Exception:  # pragma: no cover  # noqa: BLE001 - defensive: court app/DB unavailable
        logger.warning(
            "court-case derive failed for material %s", material_iri, exc_info=True
        )
        return None


def resolve_materials(material_iris) -> dict[str, ResolvedMaterial]:
    """Resolve canonical material @id IRIs to display details.

    Args:
        material_iris: An iterable of canonical material @id IRI strings
            (``https://jawafdehi.org/material/<source>/<ident>``). Duplicates and
            falsy values are ignored.

    Returns:
        A dict mapping every requested (non-empty) id to a ``ResolvedMaterial``.
        Ids that cannot be resolved (soft-deleted, missing, or materials app
        unavailable) map to a stub with ``display_name``/``material_type``
        ``None`` and empty ``urls``.
    """
    ids = [iri for iri in dict.fromkeys(material_iris) if iri]
    if not ids:
        return {}

    resolved: dict[str, ResolvedMaterial] = {iri: _stub_material(iri) for iri in ids}

    try:
        from django.apps import apps as _django_apps

        if not _django_apps.is_installed("materials"):
            raise ModuleNotFoundError("materials not in INSTALLED_APPS")
        from materials.models import Material
    except (ImportError, RuntimeError):  # pragma: no cover - stub fallback path
        logger.debug(
            "Materials app not available in-process; returning stub material "
            "records for %d id(s).",
            len(ids),
        )
        return resolved

    try:
        # Material is keyed by `iri` (the canonical @id) and soft-deleted rows
        # must not resolve (a retracted document is not evidence).
        for mat in Material.objects.filter(iri__in=ids, is_deleted=False):
            data: Any = mat.data if isinstance(mat.data, dict) else {}
            resolved[mat.iri] = {
                "material_iri": mat.iri,
                "display_name": _primary_name_from_document(data),
                "material_type": mat.material_type or None,
                "urls": _links_from_document(data),
            }
    except Exception:  # pragma: no cover  # noqa: BLE001 - defensive: DB not routed/migrated
        logger.warning(
            "Failed to resolve NGM materials in-process; returning stubs.",
            exc_info=True,
        )

    # Court-case materials often have NO stored Material row — they are derived
    # on the fly from the court tables (the same fallback GET /api/materials/
    # uses via ``_resolve_material``). Without this, evidence referencing such a
    # material resolves to a stub (display_name=None) and the public case card
    # falls back to showing the raw IRI/slug (BB-20). Fill any id that is still a
    # stub from the derived court-case JSON-LD.
    unresolved = [iri for iri in ids if resolved[iri]["display_name"] is None]
    if unresolved:
        from materials.jsonld import MaterialType

        for iri in unresolved:
            data = _derive_court_case_document(iri)
            if not isinstance(data, dict):
                continue
            resolved[iri] = {
                "material_iri": iri,
                "display_name": _primary_name_from_document(data),
                # Derived docs only ever describe a court case; use the stable
                # NGM material-type token (not the schema.org @type).
                "material_type": MaterialType.COURT_CASE,
                "urls": _links_from_document(data),
            }

    return resolved
