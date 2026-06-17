from django.contrib.auth.models import Group

# Zitadel project-role name -> Django Group name
ROLE_TO_GROUP = {
    "admin": "Admin",
    "moderator": "Moderator",
    "contributor": "Contributor",
    "readonly": "ReadOnly",
    "review_assistant": "ReviewAssistant",
}
MANAGED_GROUPS = set(ROLE_TO_GROUP.values())
SUPERUSER_GROUPS = {"Admin"}
STAFF_GROUPS = {"Admin", "Moderator", "Contributor"}


def sync_user_roles(user, role_names):
    """Zitadel-authoritative sync: make the user's membership within MANAGED_GROUPS
    exactly mirror the given Zitadel role names. Groups outside MANAGED_GROUPS are
    never touched. Updates is_superuser/is_staff to match. Saves only when changed."""
    desired = {
        ROLE_TO_GROUP[r.lower()]
        for r in (role_names or [])
        if r.lower() in ROLE_TO_GROUP
    }

    current_managed = set(
        user.groups.filter(name__in=MANAGED_GROUPS).values_list("name", flat=True)
    )
    to_add = desired - current_managed
    to_remove = current_managed - desired
    if to_add:
        for g in to_add:
            grp, _ = Group.objects.get_or_create(name=g)
            user.groups.add(grp)
    if to_remove:
        for grp in Group.objects.filter(name__in=to_remove):
            user.groups.remove(grp)

    want_super = bool(desired & SUPERUSER_GROUPS)
    want_staff = bool(desired & STAFF_GROUPS)
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
