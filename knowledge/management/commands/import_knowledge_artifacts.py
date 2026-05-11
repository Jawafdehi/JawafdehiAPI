import json
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError

from knowledge.importer import KnowledgeImportError, import_knowledge_manifest


class Command(BaseCommand):
    help = "Import generic knowledge artifacts from a JSON manifest."

    def add_arguments(self, parser):
        parser.add_argument("manifest", help="Path to a knowledge artifact manifest")
        parser.add_argument(
            "--embed",
            action="store_true",
            help=(
                "Generate embeddings during import using the configured "
                "KNOWLEDGE_RAG_EMBEDDING_MODEL."
            ),
        )

    def handle(self, *args, **options):
        manifest_path = Path(options["manifest"]).resolve()
        if not manifest_path.is_file():
            raise CommandError(f"Manifest not found: {manifest_path}")

        manifest = _load_json(manifest_path)
        if options["embed"]:
            embedding_payload = manifest.get("embedding") or {}
            if isinstance(embedding_payload, dict):
                manifest["embedding"] = embedding_payload | {"auto": True}
            else:
                manifest["embedding"] = {"auto": True}
        base_dir = manifest_path.parent

        try:
            result = import_knowledge_manifest(manifest, base_dir=base_dir)
        except KnowledgeImportError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(
            self.style.SUCCESS(
                f"Imported {result.chunks_imported} chunks into "
                f"{result.collection.name}/{result.source.id}"
                f" and {result.embeddings_imported} embeddings"
            )
        )


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CommandError(f"Invalid JSON in {path}: {exc}") from exc
