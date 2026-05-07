from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils.text import slugify

from caseworker.models import PublicChatConfig, RAGSkillProfile, Skill
from knowledge.models import KnowledgeCollection


class Command(BaseCommand):
    help = "Sync a file-based public RAG skill pack into the database."

    def add_arguments(self, parser):
        parser.add_argument(
            "skill_dir", help="Directory containing SKILL.md and skill.json"
        )
        parser.add_argument(
            "--attach-active-public-chat",
            action="store_true",
            help="Attach the RAG skill and collections to the active public chat config.",
        )
        parser.add_argument(
            "--public-chat-config",
            help="PublicChatConfig.name to attach. Overrides --attach-active-public-chat.",
        )
        parser.add_argument(
            "--skip-knowledge-import",
            action="store_true",
            help="Only sync Skill/RAGSkillProfile metadata; do not import manifests.",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        skill_dir = Path(options["skill_dir"]).resolve()
        if not skill_dir.is_dir():
            raise CommandError(f"RAG skill directory not found: {skill_dir}")

        skill_md_path = skill_dir / "SKILL.md"
        metadata_path = skill_dir / "skill.json"
        if not skill_md_path.is_file():
            raise CommandError(f"Missing SKILL.md in {skill_dir}")
        if not metadata_path.is_file():
            raise CommandError(f"Missing skill.json in {skill_dir}")

        metadata = _load_json(metadata_path)
        name = _required_slug(metadata, "name")
        display_name = (
            _optional_string(metadata, "display_name") or name.replace("-", " ").title()
        )
        description = _optional_string(metadata, "description") or ""
        trigger_keywords = _string_list(
            metadata.get("trigger_keywords", []), "trigger_keywords"
        )
        if not trigger_keywords:
            raise CommandError("RAG skill must declare at least one trigger keyword.")
        skill_content = skill_md_path.read_text(encoding="utf-8").strip()
        if not skill_content:
            raise CommandError("SKILL.md cannot be empty.")

        imported_collection_names = []
        if not options["skip_knowledge_import"]:
            imported_collection_names = self._import_manifests(skill_dir, metadata)

        configured_collection_names = _string_list(
            metadata.get("collections", []), "collections"
        )
        collection_names = sorted(
            set(configured_collection_names + imported_collection_names)
        )
        if not collection_names:
            raise CommandError(
                "RAG skill must declare collections or import at least one manifest."
            )

        collections = list(
            KnowledgeCollection.objects.filter(name__in=collection_names)
        )
        found_names = {collection.name for collection in collections}
        missing_names = sorted(set(collection_names) - found_names)
        if missing_names:
            raise CommandError(
                "RAG skill references missing knowledge collections: "
                + ", ".join(missing_names)
            )

        skill, _ = Skill.objects.update_or_create(
            name=name,
            defaults={
                "display_name": display_name,
                "description": description,
                "content": skill_content,
                "is_active": _optional_bool(metadata, "is_active", True),
            },
        )

        profile, _ = RAGSkillProfile.objects.update_or_create(
            name=name,
            defaults={
                "display_name": display_name,
                "description": description,
                "skill": skill,
                "trigger_keywords": trigger_keywords,
                "priority": _nonnegative_int(metadata, "priority", 100),
                "max_results": _positive_int(metadata, "max_results", 5),
                "min_keyword_matches": _positive_int(
                    metadata, "min_keyword_matches", 1
                ),
                "requires_citations": _optional_bool(
                    metadata, "requires_citations", True
                ),
                "is_active": _optional_bool(metadata, "is_active", True),
                "source_path": str(skill_dir),
                "metadata": _optional_object(metadata, "metadata"),
            },
        )
        profile.collections.set(collections)

        chat_config = self._target_public_chat_config(options, metadata)
        if chat_config:
            chat_config.knowledge_rag_enabled = True
            chat_config.save()
            chat_config.rag_skill_profiles.add(profile)
            chat_config.knowledge_collections.add(*collections)

        self.stdout.write(
            self.style.SUCCESS(
                f"Synced RAG skill {name} with {len(collections)} collection(s)."
            )
        )
        if chat_config:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Attached to public chat config {chat_config.name}."
                )
            )

    def _import_manifests(self, skill_dir: Path, metadata: dict[str, Any]) -> list[str]:
        manifest_paths = _manifest_paths(skill_dir, metadata)
        collection_names = []
        for manifest_path in manifest_paths:
            manifest = _load_json(manifest_path)
            collection_names.append(_manifest_collection_name(manifest, manifest_path))
            call_command("import_knowledge_artifacts", str(manifest_path))
        return collection_names

    def _target_public_chat_config(
        self, options: dict[str, Any], metadata: dict[str, Any]
    ) -> PublicChatConfig | None:
        config_name = options.get("public_chat_config") or _optional_string(
            metadata, "public_chat_config"
        )
        if config_name:
            try:
                return PublicChatConfig.objects.get(name=config_name)
            except PublicChatConfig.DoesNotExist as exc:
                raise CommandError(
                    f"PublicChatConfig not found: {config_name}"
                ) from exc

        attach_active = _optional_bool(metadata, "attach_active_public_chat", False)
        if options["attach_active_public_chat"] or attach_active:
            config = PublicChatConfig.objects.filter(is_active=True).first()
            if config is None:
                raise CommandError("No active PublicChatConfig exists.")
            return config

        return None


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CommandError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise CommandError(f"{path} must contain a JSON object.")
    return payload


def _required_slug(metadata: dict[str, Any], field: str) -> str:
    value = metadata.get(field)
    if not isinstance(value, str) or not value.strip():
        raise CommandError(f"skill.json requires {field}.")
    slug = slugify(value.strip())
    if not slug:
        raise CommandError(f"skill.json {field} must produce a valid slug.")
    return slug


def _optional_string(metadata: dict[str, Any], field: str) -> str | None:
    value = metadata.get(field)
    if value is None:
        return None
    if not isinstance(value, str):
        raise CommandError(f"skill.json {field} must be a string.")
    return value.strip()


def _optional_bool(metadata: dict[str, Any], field: str, default: bool) -> bool:
    value = metadata.get(field, default)
    if not isinstance(value, bool):
        raise CommandError(f"skill.json {field} must be a boolean.")
    return value


def _nonnegative_int(metadata: dict[str, Any], field: str, default: int) -> int:
    value = metadata.get(field, default)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise CommandError(f"skill.json {field} must be a non-negative integer.")
    return value


def _positive_int(metadata: dict[str, Any], field: str, default: int) -> int:
    value = metadata.get(field, default)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise CommandError(f"skill.json {field} must be a positive integer.")
    return value


def _optional_object(metadata: dict[str, Any], field: str) -> dict[str, Any]:
    value = metadata.get(field, {})
    if not isinstance(value, dict):
        raise CommandError(f"skill.json {field} must be a JSON object.")
    return value


def _string_list(value: Any, field: str = "value") -> list[str]:
    if not isinstance(value, list):
        raise CommandError(f"skill.json {field} must be a JSON list of strings.")
    result = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise CommandError(
                f"skill.json {field} must contain non-empty strings only."
            )
        result.append(item.strip())
    return result


def _manifest_paths(skill_dir: Path, metadata: dict[str, Any]) -> list[Path]:
    raw_paths = metadata.get("manifests")
    if raw_paths is None:
        default_manifest = skill_dir / "manifest.json"
        if default_manifest.is_file():
            return [default_manifest]
        return []
    paths = _string_list(raw_paths, "manifests")
    resolved = [(skill_dir / path).resolve() for path in paths]
    escaped = [str(path) for path in resolved if not path.is_relative_to(skill_dir)]
    if escaped:
        raise CommandError(
            "Manifest path(s) must stay inside the RAG skill directory: "
            + ", ".join(escaped)
        )
    missing = [str(path) for path in resolved if not path.is_file()]
    if missing:
        raise CommandError("Missing manifest file(s): " + ", ".join(missing))
    return resolved


def _manifest_collection_name(manifest: dict[str, Any], path: Path) -> str:
    collection = manifest.get("collection")
    if isinstance(collection, str) and collection.strip():
        return collection.strip()
    if isinstance(collection, dict):
        name = collection.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    raise CommandError(f"Manifest {path} must declare collection.name.")
