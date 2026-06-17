from django.contrib.auth.models import Group

# OIDC role key -> Django Group name.
# `admin` and `staff` are handled separately below: they drive the is_superuser
# and is_staff flags. `staff` intentionally maps to NO group (admin-site access
# only, no content permissions).
ROLE_TO_GROUP = {
    "admin": "Admin",
    "moderator": "Moderator",
    "contributor": "Contributor",
    "readonly": "ReadOnly",
    "review_assistant": "ReviewAssistant",
}
MANAGED_GROUPS = set(ROLE_TO_GROUP.values())
SUPERUSER_ROLE = "admin"
STAFF_ROLE = "staff"


def sync_user_roles(user, role_names):
    """IdP-authoritative sync: overwrite the user's membership within
    MANAGED_GROUPS to exactly mirror the OIDC role keys. Groups outside
    MANAGED_GROUPS are never touched.

    Flags are driven by explicit roles, not derived from content groups:
    `admin` -> is_superuser, `staff` -> is_staff. Saves only when something changed.
    """
    # A bare string would otherwise iterate character-by-character; treat it as
    # a single role name.
    if isinstance(role_names, str):
        role_names = [role_names]
    role_set = {r.lower() for r in (role_names or []) if isinstance(r, str)}
    desired = {ROLE_TO_GROUP[r] for r in role_set if r in ROLE_TO_GROUP}

    current_managed = set(
        user.groups.filter(name__in=MANAGED_GROUPS).values_list("name", flat=True)
    )
    to_add = desired - current_managed
    to_remove = current_managed - desired
    if to_add:
        groups = [Group.objects.get_or_create(name=name)[0] for name in to_add]
        user.groups.add(*groups)
    if to_remove:
        user.groups.remove(*Group.objects.filter(name__in=to_remove))

    want_super = SUPERUSER_ROLE in role_set
    want_staff = STAFF_ROLE in role_set
    changed = []
    if user.is_superuser != want_super:
        user.is_superuser = want_super
        changed.append("is_superuser")
    if user.is_staff != want_staff:
        user.is_staff = want_staff
        changed.append("is_staff")
    if changed:
        user.save(update_fields=changed)
    return user
