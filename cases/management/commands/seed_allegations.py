from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Seed example cases data"

    def handle(self, *args, **options):
        raise NotImplementedError(
            "This command creates/reads DocumentSource rows, which have been "
            "removed (ADR: cases own no documents). It must be rewired to create "
            "Material + CaseMaterialReference records before use. See "
            "docs/jawafdehi/sources-to-materials-prod-migration.md."
        )
