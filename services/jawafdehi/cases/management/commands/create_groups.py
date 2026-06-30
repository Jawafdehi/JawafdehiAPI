"""
Management command to create user groups for role-based permissions.

Usage: python manage.py create_groups
"""

from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType
from django.core.management.base import BaseCommand

from cases.models import (
    Case,
    CaseEntityRelationship,
    DocumentSource,
)


class Command(BaseCommand):
    help = (
        "Create user groups (Admin, Moderator, Caseworker, ReadOnly, Public, "
        "ReviewAssistant) with appropriate permissions"
    )

    def handle(self, *args, **options):
        """Create groups and assign permissions."""

        # Get content types
        case_ct = ContentType.objects.get_for_model(Case)
        source_ct = ContentType.objects.get_for_model(DocumentSource)
        relationship_ct = ContentType.objects.get_for_model(CaseEntityRelationship)

        # Get or create permissions for Case
        case_permissions = {
            "view": Permission.objects.get_or_create(
                codename="view_case",
                content_type=case_ct,
                defaults={"name": "Can view case"},
            )[0],
            "add": Permission.objects.get_or_create(
                codename="add_case",
                content_type=case_ct,
                defaults={"name": "Can add case"},
            )[0],
            "change": Permission.objects.get_or_create(
                codename="change_case",
                content_type=case_ct,
                defaults={"name": "Can change case"},
            )[0],
            "delete": Permission.objects.get_or_create(
                codename="delete_case",
                content_type=case_ct,
                defaults={"name": "Can delete case"},
            )[0],
        }

        # Get or create permissions for DocumentSource
        source_permissions = {
            "view": Permission.objects.get_or_create(
                codename="view_documentsource",
                content_type=source_ct,
                defaults={"name": "Can view document source"},
            )[0],
            "add": Permission.objects.get_or_create(
                codename="add_documentsource",
                content_type=source_ct,
                defaults={"name": "Can add document source"},
            )[0],
            "change": Permission.objects.get_or_create(
                codename="change_documentsource",
                content_type=source_ct,
                defaults={"name": "Can change document source"},
            )[0],
            "delete": Permission.objects.get_or_create(
                codename="delete_documentsource",
                content_type=source_ct,
                defaults={"name": "Can delete document source"},
            )[0],
        }

        # NOTE: there are no JawafEntity permissions anymore — the model was
        # removed because NES owns entities. Case<->entity binds are managed
        # through CaseEntityRelationship (permissions below).

        # Get or create permissions for CaseEntityRelationship
        relationship_permissions = {
            "view": Permission.objects.get_or_create(
                codename="view_caseentityrelationship",
                content_type=relationship_ct,
                defaults={"name": "Can view case entity relationship"},
            )[0],
            "add": Permission.objects.get_or_create(
                codename="add_caseentityrelationship",
                content_type=relationship_ct,
                defaults={"name": "Can add case entity relationship"},
            )[0],
            "change": Permission.objects.get_or_create(
                codename="change_caseentityrelationship",
                content_type=relationship_ct,
                defaults={"name": "Can change case entity relationship"},
            )[0],
            "delete": Permission.objects.get_or_create(
                codename="delete_caseentityrelationship",
                content_type=relationship_ct,
                defaults={"name": "Can delete case entity relationship"},
            )[0],
        }

        # Create Admin group
        admin_group, created = Group.objects.get_or_create(name="Admin")
        if created:
            self.stdout.write(self.style.SUCCESS("Created Admin group"))
        else:
            self.stdout.write("Admin group already exists")

        # Admins get all permissions
        admin_group.permissions.set(
            [
                case_permissions["view"],
                case_permissions["add"],
                case_permissions["change"],
                case_permissions["delete"],
                source_permissions["view"],
                source_permissions["add"],
                source_permissions["change"],
                source_permissions["delete"],
                relationship_permissions["view"],
                relationship_permissions["add"],
                relationship_permissions["change"],
                relationship_permissions["delete"],
            ]
        )

        # Create Moderator group
        moderator_group, created = Group.objects.get_or_create(name="Moderator")
        if created:
            self.stdout.write(self.style.SUCCESS("Created Moderator group"))
        else:
            self.stdout.write("Moderator group already exists")

        # Moderators get all permissions for cases, sources, and entities
        moderator_group.permissions.set(
            [
                case_permissions["view"],
                case_permissions["add"],
                case_permissions["change"],
                case_permissions["delete"],
                source_permissions["view"],
                source_permissions["add"],
                source_permissions["change"],
                source_permissions["delete"],
                relationship_permissions["view"],
                relationship_permissions["add"],
                relationship_permissions["change"],
                relationship_permissions["delete"],
            ]
        )

        # Create Caseworker group (formerly "Contributor")
        caseworker_group, created = Group.objects.get_or_create(name="Caseworker")
        if created:
            self.stdout.write(self.style.SUCCESS("Created Caseworker group"))
        else:
            self.stdout.write("Caseworker group already exists")

        # Caseworkers get view, add, and change permissions (limited by assignment for cases/sources)
        # Entities: caseworkers can view and add, but cannot change or delete
        caseworker_group.permissions.set(
            [
                case_permissions["view"],
                case_permissions["add"],
                case_permissions["change"],
                source_permissions["view"],
                source_permissions["add"],
                source_permissions["change"],
                relationship_permissions["view"],
                relationship_permissions["add"],
                relationship_permissions["change"],
                relationship_permissions["delete"],
            ]
        )

        # ReviewAssistant: a review-system role that can manage document sources
        # (e.g. populate the MARKDOWN url during a review) and access reviews.
        # Review access itself is granted in review/permissions.py by group name;
        # here we grant the document-source permissions it needs.
        review_assistant_group, created = Group.objects.get_or_create(
            name="ReviewAssistant"
        )
        if created:
            self.stdout.write(self.style.SUCCESS("Created ReviewAssistant group"))
        else:
            self.stdout.write("ReviewAssistant group already exists")

        review_assistant_group.permissions.set(
            [
                source_permissions["view"],
                source_permissions["change"],
            ]
        )

        # ReadOnly: an org-wide read role that can be assigned to anyone. Grants
        # view_* on every content model INCLUDING casework, so the holder can
        # GET/list all cases (including non-PUBLISHED, non-CLOSED), sources,
        # uploads, entities, and relationships, but holds no add/change/delete
        # permission. Casework review read access is granted by group name in
        # review/permissions.py (CanReadReview); writes there stay gated by
        # HasContributorRole (the caseworker-role write gate), which excludes
        # ReadOnly.
        readonly_group, created = Group.objects.get_or_create(name="ReadOnly")
        if created:
            self.stdout.write(self.style.SUCCESS("Created ReadOnly group"))
        else:
            self.stdout.write("ReadOnly group already exists")

        readonly_group.permissions.set(
            [
                case_permissions["view"],
                source_permissions["view"],
                relationship_permissions["view"],
            ]
        )

        # Public: a public-surface read role. Like ReadOnly it can be assigned to
        # anyone and holds no write permission, but it has NO casework access:
        # it is granted NO view_* model permissions (view_case /
        # view_documentsource / view_caseentityrelationship all expose casework
        # such as draft/in-review material). A Public user therefore sees only
        # the unauthenticated public surface (PUBLISHED cases via the public API,
        # which requires no model perm) and is excluded from the casework view
        # predicates (can_view_case / can_view_source) and CanReadReview. The
        # group still exists so the role is a first-class, assignable principal
        # that the OIDC role->group sync can attach.
        public_group, created = Group.objects.get_or_create(name="Public")
        if created:
            self.stdout.write(self.style.SUCCESS("Created Public group"))
        else:
            self.stdout.write("Public group already exists")

        # Public = ReadOnly MINUS casework: no model view perms at all.
        public_group.permissions.set([])

        # NGM rate-limit tier groups. These hold no model permissions; they
        # exist so the OIDC role->group sync can attach them (the authenticator
        # only attaches EXISTING groups) and the in-process NGM plane can gate on
        # them (ngm_service.courts.permissions.NGM_ROLE_GROUPS). Also seeded by
        # the cases migration 0039_ngm_rate_tier_groups so a fresh DB has them
        # without running this command; created here too for completeness.
        for tier_name in ("NGM_SilverTier", "NGM_GoldTier", "NGM_PlatinumTier"):
            _, created = Group.objects.get_or_create(name=tier_name)
            if created:
                self.stdout.write(self.style.SUCCESS(f"Created {tier_name} group"))
            else:
                self.stdout.write(f"{tier_name} group already exists")

        self.stdout.write(self.style.SUCCESS("Successfully configured all groups"))
