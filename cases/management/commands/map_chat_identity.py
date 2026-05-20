"""Map an OpenWebUI user ID to a Django user via ChatUserIdentity.

Usage:
    python manage.py map_chat_identity <owui_user_id> <django_username>
    python manage.py map_chat_identity --list

Examples:
    python manage.py map_chat_identity 62c9d5f7-fd25-4f30-bb9f-fa9a48899825 ashwini

The Django user must already exist. This command updates the ChatUserIdentity
record (auto-created by first API call) to point to the Django user.
"""

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from cases.models import ChatUserIdentity

User = get_user_model()


class Command(BaseCommand):
    help = __doc__

    def add_arguments(self, parser):
        parser.add_argument("owui_user_id", nargs="?", help="OpenWebUI user ID")
        parser.add_argument(
            "django_username", nargs="?", help="Django username to map to"
        )
        parser.add_argument(
            "--list",
            action="store_true",
            help="List all ChatUserIdentity records and their mapping status",
        )

    def handle(self, *args, **options):
        if options["list"]:
            self._list_all()
            return

        owui_user_id = options["owui_user_id"]
        django_username = options["django_username"]

        if not owui_user_id or not django_username:
            raise CommandError(
                "Both owui_user_id and django_username are required (or use --list)"
            )

        try:
            user = User.objects.get(username=django_username)
        except User.DoesNotExist:
            raise CommandError(f"Django user '{django_username}' does not exist.")

        identity, created = ChatUserIdentity.objects.get_or_create(
            owui_user_id=owui_user_id,
            defaults={"user": user, "owui_user_name": ""},
        )

        if not created:
            identity.user = user
            identity.save(update_fields=["user"])

        roles = list(user.groups.values_list("name", flat=True))
        self.stdout.write(
            self.style.SUCCESS(
                f"Mapped OWUI user {owui_user_id} -> Django user '{django_username}' "
                f"(roles: {roles or 'none'})"
            )
        )

    def _list_all(self):
        """List all ChatUserIdentity records."""
        identities = list(
            ChatUserIdentity.objects.select_related("user")
            .prefetch_related("user__groups")
            .all()
            .order_by("owui_user_name")
        )

        self.stdout.write(
            f"\n{'OWUI User ID':<40} {'OWUI Name':<25} {'Mapped To':<25} {'Roles'}"
        )
        self.stdout.write("-" * 110)

        for ident in identities:
            if ident.user:
                roles = [g.name for g in ident.user.groups.all()]
                mapped_to = ident.user.get_username()
                roles_str = ", ".join(roles) if roles else "(none)"
            else:
                mapped_to = "(unmapped)"
                roles_str = "—"

            self.stdout.write(
                f"{ident.owui_user_id:<40} {ident.owui_user_name:<25} {mapped_to:<25} {roles_str}"
            )

        self.stdout.write(f"\nTotal: {len(identities)} identity records")
