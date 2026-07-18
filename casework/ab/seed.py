"""Load a read-only prod snapshot into the LOCAL sqlite databases."""
import json
from pathlib import Path


def material_iris_from_case(case):
    return [
        e["material_iri"]
        for e in (case.get("evidence") or [])
        if isinstance(e, dict) and e.get("material_iri")
    ]


def evidence_entries_from_case(case):
    """The raw evidence dicts on a snapshot case payload (order preserved)."""
    return [e for e in (case.get("evidence") or []) if isinstance(e, dict)]


def material_doc_from_entry(entry):
    """Build ``(iri, material_type, jsonld_doc)`` from one snapshot evidence
    entry's resolved ``material`` dict, or ``None`` when the entry carries no
    ``material_iri`` or no resolvable ``material`` payload to seed from.

    The snapshot's ``material`` dict is exactly the shape the local detail
    endpoint's resolver (``cases.services.material_resolver``) expects a stored
    ``Material.data`` to project back into: ``display_name`` -> ``name``,
    ``material_type`` -> the promoted column + schema.org ``@type``, and each
    ``urls[]`` ``{link, role}`` pair -> one ``associatedMedia`` MediaObject via
    ``materials.jsonld.media_objects_from_document_sources`` (the SAME shaper
    prod uses), so roles — especially ``MARKDOWN`` — round-trip verbatim: no
    role is invented and none is dropped.
    """
    from materials.jsonld import (
        MATERIAL_CONTEXT,
        MaterialType,
        media_objects_from_document_sources,
        type_for,
    )

    iri = entry.get("material_iri")
    material = entry.get("material")
    if not iri or not isinstance(material, dict):
        return None

    material_type = material.get("material_type") or MaterialType.DOCUMENT
    schema_type, additional_type = type_for(material_type)
    display_name = (material.get("display_name") or "").strip()

    doc = {
        "@context": MATERIAL_CONTEXT,
        "@type": schema_type,
        "@id": iri,
        "name": display_name or iri,
    }
    if additional_type:
        doc["additionalType"] = additional_type

    urls = material.get("urls") or []
    media = media_objects_from_document_sources([{"url": urls}])
    if media:
        doc["associatedMedia"] = media

    return iri, material_type, doc


def seed_materials_from_snapshot(cases):
    """Create/update NGM ``Material`` rows from every case's evidence entries.

    Writes land in the ``ngm`` database via ``config.db_router`` (the
    ``materials`` app is pinned there) — no cross-database FK is created;
    ``CaseMaterialReference`` keeps storing only the ``material_iri`` string.
    Keyed by ``iri`` (the Material PK) via ``update_or_create``, so re-running
    the seed against the same snapshot is idempotent: it neither duplicates a
    Material row nor accumulates duplicate roles on ``associatedMedia`` — a
    rerun instead REPLACES the prior doc with the freshly-derived one.
    """
    from jawafdehi_shared.entities.ids import parse_material_iri
    from materials.models import Material

    seen_iris = set()
    created = 0
    for payload in cases:
        for entry in evidence_entries_from_case(payload):
            built = material_doc_from_entry(entry)
            if built is None:
                continue
            iri, material_type, doc = built
            if iri in seen_iris:
                # Same material cited by more than one case in this snapshot;
                # the first occurrence already seeded it this run.
                continue
            seen_iris.add(iri)
            parsed = parse_material_iri(iri)
            Material.objects.update_or_create(
                iri=iri,
                defaults={
                    "material_type": material_type,
                    "source": parsed.source,
                    "ident": parsed.ident,
                    "data": doc,
                },
            )
            created += 1
    return created


def snapshot_is_usable(cases):
    if not cases:
        raise ValueError("snapshot contains no cases — refusing to seed")
    return True


def load_snapshot(snapshot_dir):
    files = sorted(Path(snapshot_dir, "cases").glob("*.json"))
    cases = [json.loads(f.read_text(encoding="utf-8")) for f in files]
    snapshot_is_usable(cases)
    return cases


def seed_from_snapshot(snapshot_dir):
    """Create local Case/CaseMaterialReference rows AND their NGM Material rows.

    Local writes only, split across two databases per ``config.db_router``:
    ``Case``/``CaseMaterialReference`` -> ``default``, ``Material`` -> ``ngm``.
    Without the ``Material`` rows, the case detail endpoint's material resolver
    (``cases.services.material_resolver.resolve_materials``) has nothing to
    resolve and every evidence entry's ``material`` comes back null — so this
    is not optional bookkeeping, it is what makes the seeded evidence actually
    resolve to a document (see ``casework/ab/README.md`` / task 4 report).
    """
    from cases.models import Case, CaseMaterialReference

    cases = load_snapshot(snapshot_dir)
    created = 0
    for payload in cases:
        case, _ = Case.objects.update_or_create(
            slug=payload["slug"],
            defaults={
                "title": payload.get("title") or payload["slug"],
                "case_type": payload.get("case_type") or "CORRUPTION",
                "state": payload.get("state") or "DRAFT",
                "short_description": payload.get("short_description") or "",
                "description": payload.get("description") or "",
                "bigo": payload.get("bigo"),
                "tags": payload.get("tags") or [],
                "timeline": payload.get("timeline") or [],
                "key_allegations": payload.get("key_allegations") or [],
                "court_cases": payload.get("court_cases") or [],
            },
        )
        case.material_references.all().delete()
        for ordinal, iri in enumerate(material_iris_from_case(payload)):
            CaseMaterialReference.objects.create(
                case=case, material_iri=iri, ordinal=ordinal, additional_details=""
            )
        created += 1

    materials_seeded = seed_materials_from_snapshot(cases)
    return {"seeded": created, "materials_seeded": materials_seeded}
