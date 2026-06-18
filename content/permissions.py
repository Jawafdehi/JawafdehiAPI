"""Map existing Jawafdehi role groups onto Wagtail CMS permissions.

Runs as a ``post_migrate`` handler (see ``ContentConfig.ready``) rather than a
data migration: Django creates a model's default permissions in its own
``post_migrate`` signal, so on a fresh deploy those rows don't exist yet while
data migrations run. The handler is idempotent — safe to run on every migrate.

Role mapping (groups defined in cases/rules/predicates.py):
  - Contributor -> Wagtail "Editor": draft/edit articles, manage media, no publish.
  - Moderator / Admin -> Wagtail "Moderator": full publish + delete rights.
  - ReadOnly -> no CMS access.
"""

from django.apps import apps as global_apps

# Page permission codenames on the default Page content type.
_EDITOR_PAGE_PERMS = ["add_page", "change_page"]
_MODERATOR_PAGE_PERMS = _EDITOR_PAGE_PERMS + [
    "publish_page",
    "bulk_delete_page",
    "lock_page",
    "unlock_page",
]

# (app_label, model, [codenames]) collection-scoped media permissions.
_EDITOR_MEDIA = [
    ("wagtailimages", "image", ["add_image", "change_image", "choose_image"]),
    ("wagtaildocs", "document", ["add_document", "change_document", "choose_document"]),
]
_MODERATOR_MEDIA = [
    (
        "wagtailimages",
        "image",
        ["add_image", "change_image", "delete_image", "choose_image"],
    ),
    (
        "wagtaildocs",
        "document",
        ["add_document", "change_document", "delete_document", "choose_document"],
    ),
]

ROLE_PERMISSIONS = {
    "Contributor": {"page": _EDITOR_PAGE_PERMS, "media": _EDITOR_MEDIA},
    "Moderator": {"page": _MODERATOR_PAGE_PERMS, "media": _MODERATOR_MEDIA},
    "Admin": {"page": _MODERATOR_PAGE_PERMS, "media": _MODERATOR_MEDIA},
}


def ensure_article_index():
    """Create the single ArticleIndexPage under the default site root if absent.

    Done in post_migrate (real models, clean transaction) rather than a data
    migration — Wagtail's page save/treebeard logic aborts the migration
    transaction on PostgreSQL.
    """
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

    index = ArticleIndexPage(title="Articles", slug="articles", live=True)
    root.add_child(instance=index)
    return index


def sync_cms_group_permissions(sender=None, **kwargs):
    from django.contrib.auth.management import create_permissions
    from django.contrib.auth.models import Group, Permission
    from django.contrib.contenttypes.models import ContentType
    from wagtail.models import (
        Collection,
        GroupCollectionPermission,
        GroupPagePermission,
    )

    ensure_article_index()

    # Defensive: ensure the permissions we reference have been materialised,
    # regardless of post_migrate signal ordering on a first-time deploy.
    for label in ("wagtailadmin", "wagtailcore", "wagtailimages", "wagtaildocs"):
        try:
            create_permissions(global_apps.get_app_config(label), verbosity=0)
        except LookupError:
            pass

    groups = {g.name: g for g in Group.objects.filter(name__in=ROLE_PERMISSIONS.keys())}
    if not groups:
        return

    access_admin = Permission.objects.filter(
        content_type__app_label="wagtailadmin", codename="access_admin"
    ).first()
    page_ct = ContentType.objects.filter(app_label="wagtailcore", model="page").first()
    index = ensure_article_index()
    root_collection = Collection.objects.filter(depth=1).order_by("path").first()

    for name, group in groups.items():
        config = ROLE_PERMISSIONS[name]

        if access_admin:
            group.permissions.add(access_admin)

        if index and page_ct:
            for codename in config["page"]:
                perm = Permission.objects.filter(
                    content_type=page_ct, codename=codename
                ).first()
                if perm:
                    GroupPagePermission.objects.get_or_create(
                        group=group, page=index, permission=perm
                    )

        if root_collection:
            for app_label, model, codenames in config["media"]:
                ct = ContentType.objects.filter(
                    app_label=app_label, model=model
                ).first()
                if not ct:
                    continue
                for codename in codenames:
                    perm = Permission.objects.filter(
                        content_type=ct, codename=codename
                    ).first()
                    if perm:
                        GroupCollectionPermission.objects.get_or_create(
                            group=group, collection=root_collection, permission=perm
                        )
