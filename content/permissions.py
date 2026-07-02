"""Wagtail CMS bootstrap, run as a ``post_migrate`` handler (see
``ContentConfig.ready``):

1. create the single ``ArticleIndexPage`` (can't be done in a data migration —
   Wagtail's page save aborts the migration transaction on PostgreSQL), and
2. grant the managed editorial groups their Wagtail page/collection permissions
   on the Articles index and the root collection.

Idempotent, and a no-op when the CMS tables aren't present (partial/historical
migration states in tests, e.g. rolling ``cases`` back cascade-unapplies the
dependent ``content`` migrations).

v2 divergence: on ``origin/main`` this delegated to ``config.groups.sync_groups``
(a single source of truth that also reconciled the Django model permissions for
the case domain). v2 has no ``config.groups`` — its group *membership* and
case-domain model permissions are owned by ``cases.management.commands.
create_groups`` (Django Groups: Admin / Moderator / Caseworker / ReadOnly /
Public / NGM_* tiers). This module therefore only layers the *Wagtail-specific*
page/collection permissions on top of those groups (creating the group rows if
they don't exist yet), so the CMS port stays self-contained and does not depend
on a module that no longer exists.
"""

from django.apps import apps as global_apps

# Wagtail page-permission codenames granted on the Articles index page, keyed by
# managed Django group name. Groups absent from this map get no page permission.
_ALL_PAGE_PERMS = [
    "add_page",
    "change_page",
    "publish_page",
    "bulk_delete_page",
    "lock_page",
    "unlock_page",
]
_EDITOR_PAGE_PERMS = ["add_page", "change_page"]

GROUP_PAGE_PERMS = {
    "Admin": _ALL_PAGE_PERMS,
    "Moderator": _ALL_PAGE_PERMS,
    # "Caseworker" is v2's rename of main's "Contributor" editor tier.
    "Caseworker": _EDITOR_PAGE_PERMS,
}


def _collection(ops):
    return [f"{op}_{model}" for model in ("image", "document") for op in ops]


_FULL_COLLECTION = _collection(("add", "change", "delete", "choose"))
_EDITOR_COLLECTION = _collection(("add", "change", "choose"))

# Wagtail collection-permission codenames granted on the root collection.
GROUP_COLLECTION_PERMS = {
    "Admin": _FULL_COLLECTION,
    "Moderator": _FULL_COLLECTION,
    "Caseworker": _EDITOR_COLLECTION,
}

# Groups that need access to the Wagtail admin at all (wagtailadmin.access_admin).
_ACCESS_ADMIN_GROUPS = ("Admin", "Moderator", "Caseworker")


def ensure_article_index():
    """Create the single ArticleIndexPage under the default site root if absent."""
    from wagtail.models import Page, Site

    from content.models import ArticleIndexPage

    existing = ArticleIndexPage.objects.first()
    if existing is not None:
        return existing

    site = Site.objects.filter(is_default_site=True).first()
    root = site.root_page if site else Page.objects.filter(depth=2).first()
    if root is None:
        root = Page.objects.filter(depth=1).first()
    if root is None:
        return None

    # A page row with this slug can linger as an orphan after migration
    # rollbacks in tests (the ArticleIndexPage child row is dropped while the
    # base wagtailcore_page row survives); don't try to recreate it.
    if root.get_children().filter(slug="articles").exists():
        return None

    index = ArticleIndexPage(title="Articles", slug="articles", live=True)
    root.add_child(instance=index)
    return index


def _perm(dotted):
    from django.contrib.auth.models import Permission

    app_label, codename = dotted.split(".", 1)
    return Permission.objects.filter(
        content_type__app_label=app_label, codename=codename
    ).first()


def _collection_perm(codename):
    from django.contrib.auth.models import Permission

    is_image = codename.endswith("_image")
    return Permission.objects.filter(
        content_type__app_label="wagtailimages" if is_image else "wagtaildocs",
        content_type__model="image" if is_image else "document",
        codename=codename,
    ).first()


def _reconcile_collection_scoped(model, group, scope_kwargs, want_codenames, resolve):
    """Reconcile a (group, scope) set of permission rows to exactly want."""
    existing = {
        row.permission.codename: row
        for row in model.objects.filter(group=group, **scope_kwargs).select_related(
            "permission"
        )
    }
    for codename in want_codenames:
        if codename not in existing:
            perm = resolve(codename)
            if perm:
                model.objects.create(group=group, permission=perm, **scope_kwargs)
    for codename, row in existing.items():
        if codename not in want_codenames:
            row.delete()


def sync_cms_group_permissions(sender=None, **kwargs):
    """Grant the managed editorial groups their Wagtail page/collection perms.

    Authoritative + idempotent for the CMS-specific permissions only (Django
    model permissions for the case domain remain owned by
    ``cases create_groups``). Runs on every ``migrate`` via ``post_migrate``;
    a no-op unless the Wagtail/content tables exist on this connection
    (post_migrate fires for partial/historical states in tests too).
    """
    from django.contrib.auth.management import create_permissions
    from django.contrib.auth.models import Group
    from django.db import connections
    from wagtail.models import (
        Collection,
        GroupCollectionPermission,
        GroupPagePermission,
    )

    # No-op unless the CMS tables exist on this connection. post_migrate fires
    # for every migrate, including partial/historical states in tests, where
    # querying these tables would raise.
    connection = connections[kwargs.get("using") or "default"]
    required = {"content_articleindexpage", "wagtailcore_page", "wagtailcore_site"}
    if not required.issubset(set(connection.introspection.table_names())):
        return

    # Ensure the permissions referenced below exist, regardless of post_migrate
    # signal ordering on a first-time deploy.
    for label in (
        "auth",
        "wagtailadmin",
        "wagtailcore",
        "wagtailimages",
        "wagtaildocs",
    ):
        try:
            create_permissions(global_apps.get_app_config(label), verbosity=0)
        except LookupError:
            pass

    index = ensure_article_index()
    root_collection = Collection.objects.filter(depth=1).order_by("path").first()
    access_admin = _perm("wagtailadmin.access_admin")

    managed = set(GROUP_PAGE_PERMS) | set(GROUP_COLLECTION_PERMS) | set(
        _ACCESS_ADMIN_GROUPS
    )
    for name in managed:
        group, _ = Group.objects.get_or_create(name=name)

        # Wagtail admin access (additive: we only add it, never strip other
        # model perms owned by cases.create_groups).
        if name in _ACCESS_ADMIN_GROUPS and access_admin is not None:
            group.permissions.add(access_admin)

        # Wagtail page permissions on the Articles index.
        if index is not None:
            _reconcile_collection_scoped(
                GroupPagePermission,
                group,
                {"page": index},
                set(GROUP_PAGE_PERMS.get(name, [])),
                lambda c: _perm(f"wagtailcore.{c}"),
            )

        # Wagtail collection permissions on the root collection.
        if root_collection is not None:
            _reconcile_collection_scoped(
                GroupCollectionPermission,
                group,
                {"collection": root_collection},
                set(GROUP_COLLECTION_PERMS.get(name, [])),
                _collection_perm,
            )
