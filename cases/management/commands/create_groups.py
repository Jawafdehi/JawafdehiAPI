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
    DocumentSourceUpload,
    JawafEntity,
)


class Command(BaseCommand):
    help = "Create user groups (Admin, Moderator, Contributor) with appropriate permissions"

    def handle(self, *args, **options):
        """Create groups and assign permissions."""

        # Get content types
        case_ct = ContentType.objects.get_for_model(Case)
        source_ct = ContentType.objects.get_for_model(DocumentSource)
        upload_ct = ContentType.objects.get_for_model(DocumentSourceUpload)
        entity_ct = ContentType.objects.get_for_model(JawafEntity)
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

        # Get or create permissions for DocumentSourceUpload
        upload_permissions = {
            "view": Permission.objects.get_or_create(
                codename="view_documentsourceupload",
                content_type=upload_ct,
                defaults={"name": "Can view document source upload"},
            )[0],
            "add": Permission.objects.get_or_create(
                codename="add_documentsourceupload",
                content_type=upload_ct,
                defaults={"name": "Can add document source upload"},
            )[0],
            "change": Permission.objects.get_or_create(
                codename="change_documentsourceupload",
                content_type=upload_ct,
                defaults={"name": "Can change document source upload"},
            )[0],
            "delete": Permission.objects.get_or_create(
                codename="delete_documentsourceupload",
                content_type=upload_ct,
                defaults={"name": "Can delete document source upload"},
            )[0],
        }

        # Get or create permissions for JawafEntity
        entity_permissions = {
            "view": Permission.objects.get_or_create(
                codename="view_jawafentity",
                content_type=entity_ct,
                defaults={"name": "Can view jawaf entity"},
            )[0],
            "add": Permission.objects.get_or_create(
                codename="add_jawafentity",
                content_type=entity_ct,
                defaults={"name": "Can add jawaf entity"},
            )[0],
            "change": Permission.objects.get_or_create(
                codename="change_jawafentity",
                content_type=entity_ct,
                defaults={"name": "Can change jawaf entity"},
            )[0],
            "delete": Permission.objects.get_or_create(
                codename="delete_jawafentity",
                content_type=entity_ct,
                defaults={"name": "Can delete jawaf entity"},
            )[0],
        }

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
                upload_permissions["view"],
                upload_permissions["add"],
                upload_permissions["change"],
                upload_permissions["delete"],
                entity_permissions["view"],
                entity_permissions["add"],
                entity_permissions["change"],
                entity_permissions["delete"],
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
                upload_permissions["view"],
                upload_permissions["add"],
                upload_permissions["change"],
                upload_permissions["delete"],
                entity_permissions["view"],
                entity_permissions["add"],
                entity_permissions["change"],
                entity_permissions["delete"],
                relationship_permissions["view"],
                relationship_permissions["add"],
                relationship_permissions["change"],
                relationship_permissions["delete"],
            ]
        )

        # Create Contributor group
        contributor_group, created = Group.objects.get_or_create(name="Contributor")
        if created:
            self.stdout.write(self.style.SUCCESS("Created Contributor group"))
        else:
            self.stdout.write("Contributor group already exists")

        # Contributors get view, add, and change permissions (limited by assignment for cases/sources)
        # Entities: contributors can view and add, but cannot change or delete
        contributor_group.permissions.set(
            [
                case_permissions["view"],
                case_permissions["add"],
                case_permissions["change"],
                source_permissions["view"],
                source_permissions["add"],
                source_permissions["change"],
                upload_permissions["view"],
                upload_permissions["add"],
                upload_permissions["change"],
                upload_permissions["delete"],
                entity_permissions["view"],
                entity_permissions["add"],
                relationship_permissions["view"],
                relationship_permissions["add"],
                relationship_permissions["change"],
                relationship_permissions["delete"],
            ]
        )

        self.stdout.write(self.style.SUCCESS("Successfully configured all groups"))
