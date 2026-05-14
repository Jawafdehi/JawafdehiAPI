from django.core.management.base import BaseCommand
from django.contrib.auth.models import Group
from django.contrib.auth import get_user_model
from rest_framework.authtoken.models import Token

User = get_user_model()


class Command(BaseCommand):
    help = "Create or update the chat-jawafdehi-org service account"

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

        contributor_group, _ = Group.objects.get_or_create(name="Contributor")
        user.groups.add(contributor_group)
        self.stdout.write("Ensured user is in Contributor group")

        Token.objects.filter(user=user).delete()
        token = Token.objects.create(user=user)

        self.stdout.write(self.style.SUCCESS(f"Service account token: {token.key}"))
        self.stdout.write(
            self.style.WARNING(
                "Store this token securely in jawafdehi-mcp's .env file as JAWAFDEHI_API_TOKEN"
            )
        )
