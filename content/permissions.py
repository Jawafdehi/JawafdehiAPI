"""Wagtail CMS bootstrap, run as a ``post_migrate`` handler (see
``ContentConfig.ready``):

1. create the single ``ArticleIndexPage`` (can't be done in a data migration —
   Wagtail's page save aborts the migration transaction on PostgreSQL), and
2. apply the group -> permission policy (``config.groups.sync_groups``).

Idempotent, and a no-op when the CMS tables aren't present (partial/historical
migration states in tests, e.g. rolling ``cases`` back cascade-unapplies the
dependent ``content`` migrations).
"""

from django.apps import apps as global_apps


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


def sync_cms_group_permissions(sender=None, **kwargs):
    from django.contrib.auth.management import create_permissions
    from django.db import connections

    from config.groups import sync_groups

    # No-op unless the CMS tables exist on this connection. post_migrate fires
    # for every migrate, including partial/historical states in tests, where
    # querying these tables would raise.
    connection = connections[kwargs.get("using") or "default"]
    required = {"content_articleindexpage", "wagtailcore_page", "wagtailcore_site"}
    if not required.issubset(set(connection.introspection.table_names())):
        return

    # Ensure the permissions referenced by the policy exist, regardless of
    # post_migrate signal ordering on a first-time deploy.
    for label in (
        "auth",
        "cases",
        "wagtailadmin",
        "wagtailcore",
        "wagtailimages",
        "wagtaildocs",
    ):
        try:
            create_permissions(global_apps.get_app_config(label), verbosity=0)
        except LookupError:
            pass

    sync_groups()
