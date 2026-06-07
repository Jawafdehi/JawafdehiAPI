import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from django.db import transaction
from nes.core.identifiers import break_entity_id
from nes.core.models.relationship import Relationship
from nes.core.utils.entity_utils import entity_from_dict

from .models import NesEntity, NesEntityName, NesRelationship, NesSyncState


@dataclass
class ImportResult:
    entities_upserted: int = 0
    entities_deleted: int = 0
    relationships_upserted: int = 0
    relationships_deleted: int = 0
    commit_hash: str = ""


def current_commit(repo_path: Path) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repo_path), "rev-parse", "HEAD"],
        text=True,
    ).strip()


def changed_json_paths(repo_path: Path, from_commit: str, to_commit: str = "HEAD"):
    output = subprocess.check_output(
        [
            "git",
            "-C",
            str(repo_path),
            "diff",
            "--name-status",
            "--no-renames",
            from_commit,
            to_commit,
            "--",
            "entity/**/*.json",
            "relationship/**/*.json",
        ],
        text=True,
    )
    changes = []
    for line in output.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        status = parts[0]
        path = parts[-1]
        changes.append((status, path))
    return changes


def all_json_paths(repo_path: Path):
    for root in (repo_path / "entity", repo_path / "relationship"):
        if not root.exists():
            continue
        for path in root.rglob("*.json"):
            yield path.relative_to(repo_path).as_posix()


def import_full(repo_path: Path) -> ImportResult:
    commit_hash = current_commit(repo_path)
    with transaction.atomic():
        NesRelationship.objects.all().delete()
        NesEntity.objects.all().delete()
        result = ImportResult(commit_hash=commit_hash)
        for relative_path in all_json_paths(repo_path):
            counts = upsert_path(repo_path, relative_path)
            result.entities_upserted += counts.entities_upserted
            result.relationships_upserted += counts.relationships_upserted
        update_watermark(result)
        return result


def import_incremental(repo_path: Path) -> ImportResult:
    commit_hash = current_commit(repo_path)
    state = NesSyncState.objects.order_by("pk").first()
    if state is None:
        return import_full(repo_path)

    changes = changed_json_paths(repo_path, state.last_commit_hash, commit_hash)
    with transaction.atomic():
        result = ImportResult(commit_hash=commit_hash)
        for status, relative_path in changes:
            if status.startswith("D"):
                counts = delete_path(repo_path, state.last_commit_hash, relative_path)
            else:
                counts = upsert_path(repo_path, relative_path)
            result.entities_upserted += counts.entities_upserted
            result.entities_deleted += counts.entities_deleted
            result.relationships_upserted += counts.relationships_upserted
            result.relationships_deleted += counts.relationships_deleted
        update_watermark(result)
        return result


def update_watermark(result: ImportResult):
    state, _ = NesSyncState.objects.select_for_update().get_or_create(
        pk=1,
        defaults={"last_commit_hash": result.commit_hash},
    )
    state.last_commit_hash = result.commit_hash
    state.entities_upserted = result.entities_upserted
    state.entities_deleted = result.entities_deleted
    state.relationships_upserted = result.relationships_upserted
    state.relationships_deleted = result.relationships_deleted
    state.error_message = ""
    state.save()


def upsert_path(repo_path: Path, relative_path: str) -> ImportResult:
    path = repo_path / relative_path
    data = json.loads(path.read_text(encoding="utf-8"))
    if relative_path.startswith("entity/"):
        upsert_entity(data)
        return ImportResult(entities_upserted=1)
    if relative_path.startswith("relationship/"):
        upsert_relationship(data)
        return ImportResult(relationships_upserted=1)
    return ImportResult()


def delete_path(repo_path: Path, from_commit: str, relative_path: str) -> ImportResult:
    if relative_path.startswith("entity/"):
        entity_id = entity_id_from_path(relative_path)
        deleted, _ = NesEntity.objects.filter(entity_id=entity_id).delete()
        return ImportResult(entities_deleted=1 if deleted else 0)
    if relative_path.startswith("relationship/"):
        old_data = json.loads(
            subprocess.check_output(
                ["git", "-C", str(repo_path), "show", f"{from_commit}:{relative_path}"],
                text=True,
            )
        )
        relationship = Relationship.model_validate(old_data)
        deleted, _ = NesRelationship.objects.filter(
            relationship_id=relationship.id
        ).delete()
        return ImportResult(relationships_deleted=1 if deleted else 0)
    return ImportResult()


def upsert_entity(data: dict) -> NesEntity:
    entity = entity_from_dict(data)
    payload = entity.model_dump(mode="json")
    entity_id = payload.pop("id")
    components = break_entity_id(entity_id)
    entity_prefix = payload.get("entity_prefix") or components.prefix

    nes_entity, _ = NesEntity.objects.update_or_create(
        entity_id=entity_id,
        defaults={
            "slug": payload["slug"],
            "entity_prefix": entity_prefix,
            "tags": payload.get("tags"),
            "version_summary": payload["version_summary"],
            "created_at": payload["created_at"],
            "raw_payload": payload,
        },
    )
    NesEntityName.objects.filter(entity=nes_entity).delete()
    for name in payload.get("names") or []:
        create_name(nes_entity, name)
    for name in payload.get("misspelled_names") or []:
        create_name(nes_entity, name)
    return nes_entity


def concat_name(name_parts: dict | None) -> str | None:
    if not name_parts:
        return None
    if name_parts.get("full"):
        return name_parts["full"]
    bits = []
    for key in ("prefix", "given", "middle", "family", "suffix"):
        v = name_parts.get(key)
        if v:
            bits.append(v)
    return " ".join(bits) or None


def create_name(entity: NesEntity, name: dict) -> NesEntityName:
    return NesEntityName.objects.create(
        entity=entity,
        kind=name["kind"],
        name_en=concat_name(name.get("en")),
        name_ne=concat_name(name.get("ne")),
    )


def upsert_relationship(data: dict) -> NesRelationship:
    relationship = Relationship.model_validate(data)
    payload = relationship.model_dump(mode="json")
    relationship_id = payload.pop("id")
    return NesRelationship.objects.update_or_create(
        relationship_id=relationship_id,
        defaults={
            "source_entity_id": payload["source_entity_id"],
            "target_entity_id": payload["target_entity_id"],
            "type": payload["type"],
            "raw_payload": payload,
        },
    )[0]


def entity_id_from_path(relative_path: str) -> str:
    return f"entity:{Path(relative_path).with_suffix('').as_posix()[len('entity/'):]}"


def entities_with_prefix(prefix: str):
    return NesEntity.objects.filter(entity_prefix=prefix) | NesEntity.objects.filter(
        entity_prefix__startswith=f"{prefix}/"
    )
