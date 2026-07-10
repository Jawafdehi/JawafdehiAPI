"""Regression tests for ``content.permissions.sync_cms_group_permissions``.

This ``post_migrate`` handler is the who-can-publish-articles boundary: it
layers the Wagtail-specific page/collection permissions onto the managed
editorial Django groups (Admin / Moderator / Caseworker), leaving all other
model permissions to ``cases create_groups``.

Contract characterized here (Wagtail 7.4, ``GroupPagePermission`` /
``GroupCollectionPermission`` carry a ``permission`` FK):

* Admin + Moderator get the *full* page-permission set on the Articles index,
  **including ``publish_page``**, plus full (add/change/delete/choose) image &
  document collection perms on the root collection.
* Caseworker (v2's rename of main's "Contributor" editor tier) gets *editor*
  perms only — ``add_page`` + ``change_page`` on the index and add/change/choose
  (no delete) on the collection — and, notably, **no ``publish_page``**.
* All three managed groups get ``wagtailadmin.access_admin``.
* A non-CMS group (e.g. ReadOnly) gets NOTHING — no page/collection rows and no
  access_admin — so it cannot edit or publish articles.
* Re-running is idempotent (no duplicate rows) and authoritative (a stray grant
  is revoked on the next run).

The content app + Wagtail tables live on the ``default`` DB, so these are
``@pytest.mark.django_db`` tests. The ArticleIndexPage and root collection are
created by migrations / the handler itself.
"""

import pytest

from content.permissions import (
    GROUP_COLLECTION_PERMS,
    GROUP_PAGE_PERMS,
    ensure_article_index,
    sync_cms_group_permissions,
)


def _page_codenames(group):
    from wagtail.models import GroupPagePermission

    return set(
        GroupPagePermission.objects.filter(group=group).values_list(
            "permission__codename", flat=True
        )
    )


def _collection_codenames(group):
    from wagtail.models import GroupCollectionPermission

    return set(
        GroupCollectionPermission.objects.filter(group=group).values_list(
            "permission__codename", flat=True
        )
    )


def _has_access_admin(group):
    return group.permissions.filter(
        content_type__app_label="wagtailadmin", codename="access_admin"
    ).exists()


@pytest.mark.django_db
def test_admin_and_moderator_can_publish():
    """Admin + Moderator get the full page perms incl. publish_page."""
    from django.contrib.auth.models import Group

    sync_cms_group_permissions(using="default")

    for name in ("Admin", "Moderator"):
        group = Group.objects.get(name=name)
        page_perms = _page_codenames(group)
        assert "publish_page" in page_perms
        assert page_perms == {
            "add_page",
            "change_page",
            "publish_page",
            "bulk_delete_page",
            "lock_page",
            "unlock_page",
        }
        # Full collection perms include delete_*.
        assert "delete_image" in _collection_codenames(group)
        assert "delete_document" in _collection_codenames(group)
        assert _has_access_admin(group)


@pytest.mark.django_db
def test_caseworker_is_editor_only_cannot_publish():
    """Caseworker gets edit perms but NOT publish_page or delete perms.

    This is the load-bearing boundary: the contributor tier may draft/edit
    articles but must not be able to publish them or delete media.
    """
    from django.contrib.auth.models import Group

    sync_cms_group_permissions(using="default")

    group = Group.objects.get(name="Caseworker")
    page_perms = _page_codenames(group)
    assert page_perms == {"add_page", "change_page"}
    assert "publish_page" not in page_perms
    assert "bulk_delete_page" not in page_perms

    coll_perms = _collection_codenames(group)
    assert coll_perms == {
        "add_image",
        "change_image",
        "choose_image",
        "add_document",
        "change_document",
        "choose_document",
    }
    # Editor tier must not be able to delete media.
    assert "delete_image" not in coll_perms
    assert "delete_document" not in coll_perms
    # But it does still get admin access to reach the editor.
    assert _has_access_admin(group)


@pytest.mark.django_db
def test_non_cms_group_gets_no_cms_power():
    """A group outside the managed CMS map (ReadOnly) gets no CMS perms.

    ReadOnly exists as an org-wide read role; it must not be granted Wagtail
    admin access nor any page/collection permission, so it cannot edit or
    publish articles or touch media.
    """
    from django.contrib.auth.models import Group

    # Pre-create the group so it exists before the sync runs.
    readonly, _ = Group.objects.get_or_create(name="ReadOnly")

    sync_cms_group_permissions(using="default")

    readonly.refresh_from_db()
    assert _page_codenames(readonly) == set()
    assert _collection_codenames(readonly) == set()
    assert not _has_access_admin(readonly)


@pytest.mark.django_db
def test_managed_maps_agree_with_granted_rows():
    """The rows granted match the module's declared GROUP_* maps exactly."""
    from django.contrib.auth.models import Group

    sync_cms_group_permissions(using="default")

    for name, expected in GROUP_PAGE_PERMS.items():
        group = Group.objects.get(name=name)
        assert _page_codenames(group) == set(expected)

    for name, expected in GROUP_COLLECTION_PERMS.items():
        group = Group.objects.get(name=name)
        assert _collection_codenames(group) == set(expected)


@pytest.mark.django_db
def test_creates_managed_groups_if_absent():
    """The handler creates the managed group rows when they don't exist yet."""
    from django.contrib.auth.models import Group

    Group.objects.filter(name__in=("Admin", "Moderator", "Caseworker")).delete()

    sync_cms_group_permissions(using="default")

    for name in ("Admin", "Moderator", "Caseworker"):
        assert Group.objects.filter(name=name).exists()


@pytest.mark.django_db
def test_idempotent_no_duplicate_rows():
    """Running the sync repeatedly does not accumulate or oscillate grants."""
    from django.contrib.auth.models import Group
    from wagtail.models import GroupCollectionPermission, GroupPagePermission

    sync_cms_group_permissions(using="default")

    admin = Group.objects.get(name="Admin")
    pages_after_first = GroupPagePermission.objects.filter(group=admin).count()
    colls_after_first = GroupCollectionPermission.objects.filter(group=admin).count()

    # Two more runs must not change the counts.
    sync_cms_group_permissions(using="default")
    sync_cms_group_permissions(using="default")

    assert (
        GroupPagePermission.objects.filter(group=admin).count() == pages_after_first
    )
    assert (
        GroupCollectionPermission.objects.filter(group=admin).count()
        == colls_after_first
    )
    # And the perm set is unchanged.
    assert "publish_page" in _page_codenames(admin)


@pytest.mark.django_db
def test_authoritative_revokes_stray_page_grant():
    """A stray page permission is reconciled away on the next sync.

    If a Caseworker were somehow granted publish_page on the Articles index,
    the authoritative reconcile must strip it back to the editor set.
    """
    from django.contrib.auth.models import Group, Permission
    from wagtail.models import GroupPagePermission

    sync_cms_group_permissions(using="default")

    caseworker = Group.objects.get(name="Caseworker")
    index = ensure_article_index(using="default")
    publish = Permission.objects.get(
        content_type__app_label="wagtailcore", codename="publish_page"
    )
    GroupPagePermission.objects.create(
        group=caseworker, page=index, permission=publish
    )
    assert "publish_page" in _page_codenames(caseworker)

    # Re-sync must revoke the stray grant.
    sync_cms_group_permissions(using="default")
    assert "publish_page" not in _page_codenames(caseworker)
    assert _page_codenames(caseworker) == {"add_page", "change_page"}


@pytest.mark.django_db
def test_noop_when_cms_tables_absent(monkeypatch):
    """Handler is a no-op when the required Wagtail tables are missing.

    It guards on introspection.table_names(); if the CMS tables aren't present
    (partial/historical migration states) it returns without touching groups.
    """
    from django.contrib.auth.models import Group
    from django.db import connections

    # Pretend no tables exist by patching table_names() directly on the real
    # introspection object — simpler than a delegating stand-in and safe since the
    # handler only consults table_names() here.
    monkeypatch.setattr(
        connections["default"].introspection,
        "table_names",
        lambda *args, **kwargs: [],
    )

    Group.objects.filter(name="Admin").delete()
    # Should return early without creating the Admin group.
    sync_cms_group_permissions(using="default")
    assert not Group.objects.filter(name="Admin").exists()
