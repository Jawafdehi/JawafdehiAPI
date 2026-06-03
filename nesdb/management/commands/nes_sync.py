import subprocess
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from nesdb.importer import import_incremental


def _is_inside_git_worktree(path: Path) -> bool:
    try:
        subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--is-inside-work-tree"],
            check=True,
            capture_output=True,
            timeout=10,
        )
        return True
    except (
        subprocess.CalledProcessError,
        FileNotFoundError,
        subprocess.TimeoutExpired,
    ):
        return False


class Command(BaseCommand):
    help = "Incrementally sync derived NES Postgres tables from the nes-db git diff."

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
        if not _is_inside_git_worktree(repo_path):
            raise CommandError(
                f"NES database path is not inside a git repository: {repo_path}"
            )

        result = import_incremental(repo_path)
        self.stdout.write(
            self.style.SUCCESS(
                "Synced NES DB: "
                f"{result.entities_upserted} entities upserted, "
                f"{result.entities_deleted} entities deleted, "
                f"{result.relationships_upserted} relationships upserted, "
                f"{result.relationships_deleted} relationships deleted, "
                f"commit {result.commit_hash}"
            )
        )
