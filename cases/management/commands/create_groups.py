"""Create/reconcile the role groups and their permissions.

Thin wrapper over ``config.groups.sync_groups`` — the single source of truth
for group -> permission policy, which also runs automatically on every migrate
(see ``content`` post_migrate). Kept as a manual entry point / backfill.

Usage: python manage.py create_groups
"""

from django.core.management.base import BaseCommand

from config.groups import MANAGED_GROUPS, sync_groups


class Command(BaseCommand):
    help = "Create/reconcile role groups (Admin/Moderator/Contributor/ReadOnly/ReviewAssistant) and their permissions"

    def handle(self, *args, **options):
        count = sync_groups()
        self.stdout.write(
            self.style.SUCCESS(f"Synced {count} groups: {', '.join(MANAGED_GROUPS)}")
        )
