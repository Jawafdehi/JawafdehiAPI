"""Regression tests for the Newsroom's authenticated-but-unauthorized loop.

Two independent defects conspired to lock a real editor out of the CMS for ~100
minutes on 2026-10-07 while every health check stayed green:

1. ``config.oidc_admin._apply_roles`` rewrote group membership from the role
   claim on EVERY login, so a payload that carried no role claim at all (a
   Zitadel flattening action that stopped firing — ``OIDC_RP_SCOPES`` requests
   no roles scope, so that action is the only source) silently revoked a working
   user's ``Caseworker`` group and ``is_staff``.
2. Wagtail then rendered that revocation as an endless login bounce rather than
   an error: ``require_admin_access`` sends an authenticated user lacking
   ``wagtailadmin.access_admin`` to ``reject_request`` -> ``redirect_to_login``,
   which under live SSO re-authenticates instantly and returns to the same
   forbidden page.

Together the user saw a spinning browser, and the only server-side trace was a
wall of 302s. These tests pin both halves shut.
"""

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.urls import reverse

from config.oidc_admin import _apply_roles

ROLES_CLAIM = "urn:zitadel:iam:org:project:roles"


def _caseworker_group():
    """The Caseworker group, carrying wagtailadmin.access_admin."""
    group, _ = Group.objects.get_or_create(name="Caseworker")
    perm = Permission.objects.filter(
        content_type__app_label="wagtailadmin", codename="access_admin"
    ).first()
    assert perm is not None, "wagtailadmin.access_admin missing — Wagtail not migrated?"
    group.permissions.add(perm)
    return group


def _user(email="sambhav@example.org", **kwargs):
    return get_user_model().objects.create_user(
        username=email, email=email, **kwargs
    )


# --------------------------------------------------------------------------
# 1. A missing role claim must not revoke anything.
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_absent_role_claim_preserves_existing_groups():
    """No role claim at all == unknown, NOT "this user has no roles".

    This is the regression that demoted a working editor mid-session.
    """
    group = _caseworker_group()
    user = _user(is_staff=True)
    user.groups.add(group)

    # userinfo with identity but no role information in either shape.
    _apply_roles(user, {"email": user.email, "sub": "abc123"})

    user.refresh_from_db()
    assert set(user.groups.values_list("name", flat=True)) == {"Caseworker"}
    assert user.is_staff is True


@pytest.mark.django_db
def test_empty_role_claim_does_revoke():
    """An explicitly empty claim IS a revocation and must still be honoured."""
    group = _caseworker_group()
    user = _user(is_staff=True)
    user.groups.add(group)

    _apply_roles(user, {"email": user.email, ROLES_CLAIM: {}})

    user.refresh_from_db()
    assert list(user.groups.all()) == []
    assert user.is_staff is False


@pytest.mark.django_db
def test_flattened_roles_claim_grants_caseworker():
    """The normal path: Zitadel's flattened list still maps to the group."""
    _caseworker_group()
    user = _user()

    _apply_roles(user, {"email": user.email, "roles": ["caseworker"]})

    user.refresh_from_db()
    assert set(user.groups.values_list("name", flat=True)) == {"Caseworker"}
    assert user.is_staff is True


@pytest.mark.django_db
def test_empty_flattened_roles_list_revokes():
    """``roles: []`` is present-but-empty, so it revokes rather than preserves."""
    group = _caseworker_group()
    user = _user(is_staff=True)
    user.groups.add(group)

    _apply_roles(user, {"email": user.email, "roles": []})

    user.refresh_from_db()
    assert list(user.groups.all()) == []


# --------------------------------------------------------------------------
# 2. The Newsroom must refuse, not loop.
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_signed_in_user_without_access_gets_403_not_redirect(client):
    """The loop itself: a logged-in user with no CMS role must see 403."""
    user = _user()
    client.force_login(user)

    response = client.get(reverse("wagtailadmin_home"))

    assert response.status_code == 403
    assert b"Caseworker" in response.content


@pytest.mark.django_db
def test_anonymous_user_still_redirected_to_login(client):
    """Anonymous callers are NOT the bug — they must keep getting the redirect."""
    response = client.get(reverse("wagtailadmin_home"))

    assert response.status_code == 302
    assert response.status_code != 403


@pytest.mark.django_db
def test_caseworker_reaches_the_newsroom(client):
    """The middleware must not block the people it is meant to let through."""
    user = _user(is_staff=True)
    user.groups.add(_caseworker_group())
    client.force_login(user)

    response = client.get(reverse("wagtailadmin_home"))

    assert response.status_code == 200


@pytest.mark.django_db
def test_superuser_reaches_the_newsroom(client):
    """Superusers hold no group but bypass Wagtail's checks; has_perms agrees."""
    user = _user(is_staff=True, is_superuser=True)
    client.force_login(user)

    response = client.get(reverse("wagtailadmin_home"))

    assert response.status_code == 200


@pytest.mark.django_db
def test_logout_stays_reachable_without_access(client):
    """A blocked user must still be able to sign out and switch accounts."""
    user = _user()
    client.force_login(user)

    response = client.post(reverse("wagtailadmin_logout"))

    assert response.status_code != 403
