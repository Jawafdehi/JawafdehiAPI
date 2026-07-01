"""Seed a minimal local-dev dataset + role users for the admin panel.

DEV ONLY. Creates the standard groups, three login users (admin / moderator /
caseworker, password == username), and a handful of cases (one per state), a
court, a court case, and a blocklisted firm — enough to exercise every admin
screen. Idempotent: safe to re-run.

Usage (with DEV_AUTH env, DEBUG=True):
    uv run python manage.py seed_dev
"""

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management import call_command
from django.core.management.base import BaseCommand

from cases.models import (
    Case,
    CaseEntityRelationship,
    CaseState,
    CaseType,
    RelationshipType,
)
from courts.models import BlacklistedFirm, Court, CourtCase

User = get_user_model()

USERS = [
    ("admin", [], True),
    ("moderator", ["Moderator"], False),
    ("caseworker", ["Caseworker"], False),
]

CASES = [
    ("seed-draft", "Draft: alleged kickbacks at Dept A", CaseState.DRAFT),
    ("seed-in-review", "Review: procurement fraud Ministry X", CaseState.IN_REVIEW),
    ("seed-published", "Published: embezzlement at Board Y", CaseState.PUBLISHED),
    ("seed-closed", "Closed: dismissed complaint Z", CaseState.CLOSED),
]


class Command(BaseCommand):
    help = "Seed minimal local-dev data + role users (DEV ONLY, idempotent)."

    def handle(self, *args, **options):
        call_command("create_groups")

        for name, groups, su in USERS:
            u, _ = User.objects.get_or_create(
                username=name, defaults={"email": f"{name}@local.test"}
            )
            u.set_password(name)
            u.is_staff = True
            u.is_superuser = su
            u.save()
            u.groups.clear()
            for g in groups:
                grp = Group.objects.filter(name=g).first()
                if grp:
                    u.groups.add(grp)
            self.stdout.write(f"user {name}: pw={name} superuser={su} groups={groups}")

        court, _ = Court.objects.get_or_create(
            identifier="supreme-court",
            defaults=dict(
                court_type="SUPREME",
                full_name_nepali="सर्वोच्च अदालत",
                full_name_english="Supreme Court",
            ),
        )
        CourtCase.objects.get_or_create(
            court=court,
            case_number="075-CR-0123",
            defaults=dict(
                case_type="Corruption",
                case_status="Pending",
                plaintiff="State",
                defendant="Ram Bahadur",
            ),
        )
        BlacklistedFirm.objects.get_or_create(
            firm_name="Acme Builders Pvt Ltd",
            defaults=dict(
                proprietor_name="J. Doe",
                reason="Contract fraud",
                recommending_office="CIAA",
            ),
        )

        for slug, title, state in CASES:
            strict = state in (CaseState.IN_REVIEW, CaseState.PUBLISHED)
            c, created = Case.objects.get_or_create(
                slug=slug,
                defaults=dict(
                    title=title,
                    case_type=CaseType.CORRUPTION,
                    state=state,
                    short_description="Seed case for local testing.",
                    description="## Summary\n\nSeed **markdown** body.",
                    key_allegations=["Misappropriation", "Bid rigging"]
                    if strict
                    else [],
                ),
            )
            if created and strict:
                CaseEntityRelationship.objects.get_or_create(
                    case=c,
                    nes_id="https://jawafdehi.org/entity/person/ram-bahadur",
                    relationship_type=RelationshipType.ACCUSED,
                )
            self.stdout.write(f"case {slug}: state={c.state} created={created}")

        self.stdout.write(self.style.SUCCESS("seed_dev complete."))
