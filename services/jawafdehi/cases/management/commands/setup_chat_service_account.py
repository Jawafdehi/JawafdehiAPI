from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand

User = get_user_model()


class Command(BaseCommand):
    help = (
        "Create or update the chat-jawafdehi-org service account user. "
        "NOTE (phase5 / OIDC-only migration): this command no longer mints a "
        "DRF auth token — the rest_framework.authtoken app has been removed and "
        "the API is OIDC-only. The chat service account is now a Zitadel "
        "principal; provision it in Zitadel and add its OIDC `sub` to "
        "OIDC_SERVICE_ACCOUNT_SUBJECTS. This command only provisions the local "
        "Django user/group (the OIDC `sub`-keyed user is auto-created on first "
        "authenticated request anyway)."
    )

    def handle(self, *args, **options):
        user, created = User.objects.get_or_create(
            username="chat-jawafdehi-org",
            defaults={
                "email": "chat-jawafdehi-org@jawafdehi.org",
                "is_active": True,
            },
        )

        if created:
            self.stdout.write(
                self.style.SUCCESS(f"Created service account user: {user.username}")
            )
        else:
            self.stdout.write(f"Service account user already exists: {user.username}")

        user.set_unusable_password()
        user.save(update_fields=["password"])

        caseworker_group, _ = Group.objects.get_or_create(name="Caseworker")
        user.groups.add(caseworker_group)
        self.stdout.write("Ensured user is in Caseworker group")

        self.stdout.write(
            self.style.WARNING(
                "No DRF token is created. Provision this account in Zitadel and "
                "set OIDC_SERVICE_ACCOUNT_SUBJECTS to its OIDC `sub`. The MCP "
                "server must send a Zitadel access token as Authorization: "
                "Bearer <access>."
            )
        )
