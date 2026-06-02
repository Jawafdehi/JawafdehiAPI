from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from nesdb.importer import import_full


class Command(BaseCommand):
    help = "Rebuild derived NES Postgres tables from all nes-db JSON files."

    def add_arguments(self, parser):
        parser.add_argument(
            "--path",
            default=None,
            help="Path to nes-db/v2. Defaults to settings.NES_DB_PATH.",
        )

    def handle(self, *args, **options):
        configured_path = options["path"] or settings.NES_DB_PATH
        if not configured_path:
            raise CommandError(
                "NES_DB_PATH is not configured. Pass --path or set NES_DB_PATH."
            )
        repo_path = Path(configured_path)
        if not repo_path.exists():
            raise CommandError(f"NES database path does not exist: {repo_path}")
        if (
            not (repo_path / "entity").exists()
            and not (repo_path / "relationship").exists()
        ):
            raise CommandError(
                f"NES database path does not look like nes-db/v2: {repo_path}"
            )

        result = import_full(repo_path)
        self.stdout.write(
            self.style.SUCCESS(
                "Reindexed NES DB: "
                f"{result.entities_upserted} entities, "
                f"{result.relationships_upserted} relationships, "
                f"commit {result.commit_hash}"
            )
        )
