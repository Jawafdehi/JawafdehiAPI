"""Load a read-only prod snapshot into the LOCAL sqlite databases."""
import json
from pathlib import Path


def material_iris_from_case(case):
    return [
        e["material_iri"]
        for e in (case.get("evidence") or [])
        if isinstance(e, dict) and e.get("material_iri")
    ]


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
    """Create local Case/CaseMaterialReference rows. Local writes only."""
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
    return {"seeded": created}
