"""Authz v3: collapse to Caseworker / ReadOnly / JobPoller (+ is_superuser).

This migration does two things:

1. Schema: drop the ``Case.contributors`` M2M (object-level assignment gating is
   retired — the single content-staff role can edit any case).

2. Data (auth Groups): reconcile the group catalogue to the v3 model:
   - ensure the ``Caseworker`` group exists and grant it the FULL case +
     relationship perm set (incl. ``delete_case``) — it folds in the old
     Moderator;
   - move any ``Moderator`` members into ``Caseworker``, THEN delete the
     ``Moderator`` row (order matters — deleting first would cascade the
     memberships away before we can copy them);
   - rename ``ReviewAssistant`` -> ``JobPoller`` (row UPDATE, preserving the PK
     and its user memberships);
   - delete the retired ``Admin``, ``Public`` and ``NGM_{Silver,Gold,Platinum}``
     group rows.

Why a data migration (not just ``create_groups``): the OIDC authenticator only
ATTACHES existing groups during per-request role sync — it never creates them.
And session/admin-site and ``seed_dev`` users are NOT re-synced per request, so
a ``Moderator``-only user who never re-authenticates would lose all access
unless we move them here. The move is therefore MANDATORY, not optional.

Deploy ordering: this migration renames ``ReviewAssistant`` -> ``JobPoller``.
``create_groups`` / ``seed_dev`` do a bare ``get_or_create(name="JobPoller")``,
which would collide with this rename on the UNIQUE ``auth_group.name`` if run
first — so ``migrate`` MUST run before ``create_groups``. Ship the v3
``create_groups.py`` and ``content/permissions.py`` in the SAME release: the
``content`` ``post_migrate`` hook runs the deployed code at the end of every
``migrate`` and would otherwise re-create ``Admin``/``Moderator`` group rows.

Reverse: the group reconciliation is LOSSY and IRREVERSIBLE (we cannot tell an
original Caseworker from an ex-Moderator, and deleted memberships are gone), so
its reverse is a no-op. The ``RemoveField`` reverse re-adds an EMPTY M2M
(assignments are permanently lost).
"""

from django.conf import settings
from django.db import migrations

# Retired group rows to delete outright (inline — 0039's helper cannot be
# imported: its module name starts with a digit, and 0039 is immutable).
_DELETE_GROUPS = (
    "Admin",
    "Public",
    "NGM_SilverTier",
    "NGM_GoldTier",
    "NGM_PlatinumTier",
)

# Case + relationship perm codenames the surviving Caseworker group must hold
# (the full former-Moderator set, incl. delete_case).
_CASEWORKER_PERM_CODENAMES = (
    "view_case",
    "add_case",
    "change_case",
    "delete_case",
    "view_caseentityrelationship",
    "add_caseentityrelationship",
    "change_caseentityrelationship",
    "delete_caseentityrelationship",
)


def apply_v3_roles(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")

    # 1. Ensure the surviving content-staff group exists and holds full perms.
    caseworker, _ = Group.objects.get_or_create(name="Caseworker")
    perms = list(Permission.objects.filter(codename__in=_CASEWORKER_PERM_CODENAMES))
    if perms:
        # Add (don't replace) so any pre-existing grants are preserved.
        caseworker.permissions.add(*perms)

    # 2. Move Moderator members into Caseworker, THEN delete the Moderator row.
    moderator = Group.objects.filter(name="Moderator").first()
    if moderator is not None:
        for user in moderator.user_set.all():
            user.groups.add(caseworker)
        moderator.delete()

    # 3. Rename ReviewAssistant -> JobPoller (preserves PK + memberships). If a
    #    JobPoller row somehow already exists, fold the assistant's members in
    #    and drop the old row rather than hit the UNIQUE-name constraint.
    review_assistant = Group.objects.filter(name="ReviewAssistant").first()
    if review_assistant is not None:
        existing_poller = Group.objects.filter(name="JobPoller").first()
        if existing_poller is None:
            review_assistant.name = "JobPoller"
            review_assistant.save(update_fields=["name"])
        else:
            for user in review_assistant.user_set.all():
                user.groups.add(existing_poller)
            review_assistant.delete()
    else:
        Group.objects.get_or_create(name="JobPoller")

    # 4. Delete the retired group rows.
    Group.objects.filter(name__in=_DELETE_GROUPS).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("cases", "0049_casestatechange"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("auth", "0012_alter_user_first_name_max_length"),
    ]

    operations = [
        migrations.RunPython(
            apply_v3_roles,
            reverse_code=migrations.RunPython.noop,
        ),
        migrations.RemoveField(
            model_name="case",
            name="contributors",
        ),
    ]
