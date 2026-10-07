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
)


def _set_owned_permissions(group, owned_content_type_ids, permissions):
    """Replace only the permissions this command owns, keeping the rest.

    This command owns the case-domain model permissions; the Wagtail ones on the
    same groups belong to ``content.permissions.sync_cms_group_permissions``,
    which adds them on ``post_migrate``. A blanket ``permissions.set()`` here
    does not respect that split — it drops every permission it does not itself
    list, so running this command after ``migrate`` silently revoked
    ``wagtailadmin.access_admin`` from ``Caseworker`` and locked every caseworker
    out of the Newsroom. The page and collection permissions live in their own
    tables and survived, which is why the loss was invisible until someone tried
    to open the CMS.

    Scoping the replace to the owned content types keeps this command
    authoritative over its own domain (a hand-granted case permission is still
    repaired) and inert everywhere else.
    """
    foreign = list(
        group.permissions.exclude(content_type_id__in=owned_content_type_ids)
    )
    group.permissions.set(list(permissions) + foreign)


class Command(BaseCommand):
    help = (
        "Create user groups (Caseworker, ReadOnly, JobPoller, Prerender) with "
        "appropriate permissions. v3 authz model: admin == is_superuser (no "
        "group); the single content-staff role is Caseworker (folds in the old "
        "Moderator); Prerender deliberately carries none."
    )

    def handle(self, *args, **options):
        """Create groups and assign permissions."""

        # Get content types
        case_ct = ContentType.objects.get_for_model(Case)
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

        # NOTE: DocumentSource has been removed (ADR: cases own no documents),
        # so its permissions are no longer created or assigned. Document access
        # is being rewired to Material + CaseMaterialReference; see
        # docs/jawafdehi/sources-to-materials-prod-migration.md.

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

        # Caseworker: the single content-staff role (v3). It folds in the old
        # Moderator, so it holds the FULL case + relationship perm set including
        # delete_case. Entity-registry writes are authorized in
        # entities.permissions by Group membership (Caseworker), not by these
        # model permissions. Admin == is_superuser (no group), so no Admin group
        # is created.
        caseworker_group, created = Group.objects.get_or_create(name="Caseworker")
        if created:
            self.stdout.write(self.style.SUCCESS("Created Caseworker group"))
        else:
            self.stdout.write("Caseworker group already exists")

        owned_content_type_ids = {case_ct.pk, relationship_ct.pk}

        _set_owned_permissions(
            caseworker_group,
            owned_content_type_ids,
            [
                case_permissions["view"],
                case_permissions["add"],
                case_permissions["change"],
                case_permissions["delete"],
                relationship_permissions["view"],
                relationship_permissions["add"],
                relationship_permissions["change"],
                relationship_permissions["delete"],
            ],
        )

        # JobPoller: the machine role (the review poller). Review/jobs access is
        # granted in review/permissions.py + jobs/permissions.py by group name;
        # it holds no model permissions here. (Renamed from "ReviewAssistant";
        # the migration renames the existing row so this command must run AFTER
        # migrate — a bare get_or_create here would otherwise collide with the
        # migration's rename on the UNIQUE group name.)
        job_poller_group, created = Group.objects.get_or_create(name="JobPoller")
        if created:
            self.stdout.write(self.style.SUCCESS("Created JobPoller group"))
        else:
            self.stdout.write("JobPoller group already exists")

        job_poller_group.permissions.set([])

        # ReadOnly: an org-wide read role that can be assigned to anyone. Grants
        # view_* INCLUDING casework, so the holder can GET/list all cases
        # (including non-PUBLISHED, non-CLOSED) and relationships, but holds no
        # add/change/delete permission. Casework review read access is granted by
        # group name in review/permissions.py (CanReadReview); writes there stay
        # gated by HasContributorRole (the content-role write gate), which
        # excludes ReadOnly.
        readonly_group, created = Group.objects.get_or_create(name="ReadOnly")
        if created:
            self.stdout.write(self.style.SUCCESS("Created ReadOnly group"))
        else:
            self.stdout.write("ReadOnly group already exists")

        _set_owned_permissions(
            readonly_group,
            owned_content_type_ids,
            [
                case_permissions["view"],
                relationship_permissions["view"],
            ],
        )

        # Prerender: the jawafdehi.org build's machine role. It holds NO
        # permissions and is named by NO gate, so its holder sees exactly what an
        # anonymous visitor sees. The group exists only so SyncedUserRateThrottle
        # can route the build into its own bucket (THROTTLE_RATE_PRERENDER)
        # instead of the 1000/hour anonymous one it was competing for.
        #
        # The empty permission set is the invariant, not an oversight: the build
        # writes what it fetches into static HTML that is served to everyone, so
        # granting this group anything would publish it. ``.set([])`` is
        # unconditional for the same reason JobPoller's is — it repairs a group
        # that was granted something by hand. These two machine roles are
        # deliberately NOT scoped the way Caseworker/ReadOnly are: neither is
        # ever meant to hold a CMS permission, so stripping everything is the
        # invariant rather than a bug.
        prerender_group, created = Group.objects.get_or_create(name="Prerender")
        if created:
            self.stdout.write(self.style.SUCCESS("Created Prerender group"))
        else:
            self.stdout.write("Prerender group already exists")

        prerender_group.permissions.set([])

        self.stdout.write(self.style.SUCCESS("Successfully configured all groups"))
