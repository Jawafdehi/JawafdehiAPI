from django.core.management.base import BaseCommand
from cases.models import Case, CaseState
from django.db import transaction

class Command(BaseCommand):
    help = 'Backfill slugs for published or in-review cases that have null slugs'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true', help='Do not save changes')

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        cases = Case.objects.filter(state__in=[CaseState.PUBLISHED, CaseState.IN_REVIEW], slug__isnull=True)
        
        self.stdout.write(f"Found {cases.count()} cases missing slugs.")
        
        for case in cases:
            new_slug = case._generate_unique_slug()
            self.stdout.write(f"Generated slug for case {case.id} ({case.case_id}): {new_slug}")
            
            if not dry_run:
                case.slug = new_slug
                case.save(update_fields=['slug'])
                self.stdout.write(self.style.SUCCESS(f"Successfully updated case {case.id}"))
        
        if dry_run:
            self.stdout.write("Dry run complete. No changes saved.")
        else:
            self.stdout.write("Backfill complete.")
