"""Regression tests for ``content.permissions.sync_cms_group_permissions``.

This ``post_migrate`` handler is the who-can-publish-articles boundary: it
layers the Wagtail-specific page/collection permissions onto the managed
editorial Django group, leaving all other model permissions to
``cases create_groups``.

v3 authz model: the managed CMS groups are exactly ``{Caseworker}``. Caseworker
is now a full editor+publisher (it folds in the old Moderator tier); admins are
``is_superuser`` and carry no group (superusers bypass Wagtail perm checks
entirely). There are no Admin/Moderator/Public groups anymore.

Contract characterized here (Wagtail 7.4, ``GroupPagePermission`` /
``GroupCollectionPermission`` carry a ``permission`` FK):

* Caseworker gets the *full* page-permission set on the Articles index,
  **including ``publish_page``**, plus full (add/change/delete/choose) image &
  document collection perms on the root collection.
* Caseworker gets ``wagtailadmin.access_admin``.
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
def test_caseworker_can_publish():
    """Caseworker gets the full page perms incl. publish_page.

    v3 folds the old Admin/Moderator publisher tier into Caseworker, so the
    single managed content group is now a full editor+publisher. Admins are
    ``is_superuser`` and bypass Wagtail perm checks, so they need no group.
    """
    from django.contrib.auth.models import Group

    sync_cms_group_permissions(using="default")

    group = Group.objects.get(name="Caseworker")
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
def test_caseworker_gets_full_editor_publisher_perms():
    """Caseworker is a full editor+publisher with full media perms.

    v3 inverts the old editor-only boundary: the single content-staff role may
    now publish articles AND delete media. This is the load-bearing assertion
    that Caseworker holds the complete page + collection permission set.
    """
    from django.contrib.auth.models import Group

    sync_cms_group_permissions(using="default")

    group = Group.objects.get(name="Caseworker")
    page_perms = _page_codenames(group)
    assert page_perms == {
        "add_page",
        "change_page",
        "publish_page",
        "bulk_delete_page",
        "lock_page",
        "unlock_page",
    }
    assert "publish_page" in page_perms
    assert "bulk_delete_page" in page_perms

    coll_perms = _collection_codenames(group)
    assert coll_perms == {
        "add_image",
        "change_image",
        "delete_image",
        "choose_image",
        "add_document",
        "change_document",
        "delete_document",
        "choose_document",
    }
    # Full editor now CAN delete media.
    assert "delete_image" in coll_perms
    assert "delete_document" in coll_perms
    # And it gets admin access to reach the editor.
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

    Group.objects.filter(name__in=("Caseworker",)).delete()

    sync_cms_group_permissions(using="default")

    for name in ("Caseworker",):
        assert Group.objects.filter(name=name).exists()


@pytest.mark.django_db
def test_idempotent_no_duplicate_rows():
    """Running the sync repeatedly does not accumulate or oscillate grants."""
    from django.contrib.auth.models import Group
    from wagtail.models import GroupCollectionPermission, GroupPagePermission

    sync_cms_group_permissions(using="default")

    caseworker = Group.objects.get(name="Caseworker")
    pages_after_first = GroupPagePermission.objects.filter(group=caseworker).count()
    colls_after_first = GroupCollectionPermission.objects.filter(
        group=caseworker
    ).count()

    # Two more runs must not change the counts.
    sync_cms_group_permissions(using="default")
    sync_cms_group_permissions(using="default")

    assert (
        GroupPagePermission.objects.filter(group=caseworker).count()
        == pages_after_first
    )
    assert (
        GroupCollectionPermission.objects.filter(group=caseworker).count()
        == colls_after_first
    )
    # And the perm set is unchanged.
    assert "publish_page" in _page_codenames(caseworker)


@pytest.mark.django_db
def test_authoritative_revokes_stray_page_grant():
    """A stray page permission is reconciled away on the next sync.

    If Caseworker were somehow granted a page permission outside the managed
    set (here ``delete_page``, which is not part of GROUP_PAGE_PERMS), the
    authoritative reconcile must strip it back to the managed full-editor set.
    """
    from django.contrib.auth.models import Group, Permission
    from wagtail.models import GroupPagePermission

    sync_cms_group_permissions(using="default")

    managed = {
        "add_page",
        "change_page",
        "publish_page",
        "bulk_delete_page",
        "lock_page",
        "unlock_page",
    }
    caseworker = Group.objects.get(name="Caseworker")
    index = ensure_article_index(using="default")
    # delete_page is a Django default page permission but is intentionally NOT
    # in the managed CMS set, so it stands in for an unmanaged/stray grant.
    stray = Permission.objects.get(
        content_type__app_label="wagtailcore", codename="delete_page"
    )
    GroupPagePermission.objects.create(group=caseworker, page=index, permission=stray)
    assert "delete_page" in _page_codenames(caseworker)

    # Re-sync must revoke the stray grant.
    sync_cms_group_permissions(using="default")
    assert "delete_page" not in _page_codenames(caseworker)
    assert _page_codenames(caseworker) == managed


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
