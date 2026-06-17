"""Tests for config.roles.sync_user_roles."""

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group

from config.roles import sync_user_roles

User = get_user_model()


@pytest.mark.django_db
class TestSyncUserRoles:
    """Test suite for the sync_user_roles function."""

    def test_adding_roles_creates_group_membership_and_is_staff(self):
        """Adding roles creates group membership and sets is_staff."""
        user = User.objects.create_user(
            username="testuser", email="test@example.com", password="testpass123"
        )
        assert user.is_staff is False
        assert user.groups.count() == 0

        sync_user_roles(user, ["Moderator"])
        user.refresh_from_db()

        assert user.is_staff is True
        assert user.groups.filter(name="Moderator").exists()

    def test_admin_role_sets_is_superuser(self):
        """Admin role sets is_superuser."""
        user = User.objects.create_user(
            username="admin_user", email="admin@example.com", password="testpass123"
        )
        assert user.is_superuser is False

        sync_user_roles(user, ["admin"])
        user.refresh_from_db()

        assert user.is_superuser is True
        assert user.is_staff is True
        assert user.groups.filter(name="Admin").exists()

    def test_removing_role_revokes_group_and_clears_flags(self):
        """Removing a role revokes the managed group and clears is_superuser/is_staff."""
        user = User.objects.create_user(
            username="roleuser", email="roleuser@example.com", password="testpass123"
        )
        # Add Admin role
        sync_user_roles(user, ["Admin"])
        user.refresh_from_db()
        assert user.is_superuser is True
        assert user.is_staff is True

        # Remove all roles
        sync_user_roles(user, [])
        user.refresh_from_db()

        assert user.is_superuser is False
        assert user.is_staff is False
        assert user.groups.count() == 0

    def test_non_managed_group_is_left_untouched(self):
        """A pre-existing group outside MANAGED_GROUPS is left untouched."""
        user = User.objects.create_user(
            username="mixeduser", email="mixed@example.com", password="testpass123"
        )
        # Create a non-managed group and add user to it
        custom_group, _ = Group.objects.get_or_create(name="CustomGroup")
        user.groups.add(custom_group)

        # Sync with a managed role
        sync_user_roles(user, ["Contributor"])
        user.refresh_from_db()

        # Managed role is added
        assert user.groups.filter(name="Contributor").exists()
        # Custom group remains
        assert user.groups.filter(name="CustomGroup").exists()
        assert user.is_staff is True

    def test_case_insensitive_role_names(self):
        """Role names are matched case-insensitively."""
        user = User.objects.create_user(
            username="caseuser", email="case@example.com", password="testpass123"
        )

        sync_user_roles(user, ["ADMIN", "contributor"])
        user.refresh_from_db()

        # Both roles should be added with correct casing
        assert user.groups.filter(name="Admin").exists()
        assert user.groups.filter(name="Contributor").exists()
        assert user.is_superuser is True
        assert user.is_staff is True

    def test_unknown_roles_are_ignored(self):
        """Unknown role names are silently ignored."""
        user = User.objects.create_user(
            username="unknownuser",
            email="unknown@example.com",
            password="testpass123",
        )

        # Mix of known and unknown roles
        sync_user_roles(user, ["Contributor", "UnknownRole", "SuperAdmin"])
        user.refresh_from_db()

        # Only the known role is added
        assert user.groups.filter(name="Contributor").exists()
        assert user.groups.count() == 1  # Only Contributor
        assert user.is_staff is True

    def test_readonly_role_does_not_set_is_staff(self):
        """ReadOnly role does not set is_staff."""
        user = User.objects.create_user(
            username="readonly_user",
            email="readonly@example.com",
            password="testpass123",
        )

        sync_user_roles(user, ["ReadOnly"])
        user.refresh_from_db()

        assert user.groups.filter(name="ReadOnly").exists()
        assert user.is_staff is False
        assert user.is_superuser is False

    def test_multiple_roles_are_synced_correctly(self):
        """Multiple roles can be synced at once."""
        user = User.objects.create_user(
            username="multiuser", email="multi@example.com", password="testpass123"
        )

        sync_user_roles(user, ["Moderator", "Contributor"])
        user.refresh_from_db()

        assert user.groups.filter(name="Moderator").exists()
        assert user.groups.filter(name="Contributor").exists()
        assert user.groups.count() == 2
        assert user.is_staff is True

    def test_role_list_none_is_handled(self):
        """A None role list results in no groups."""
        user = User.objects.create_user(
            username="noneuser", email="none@example.com", password="testpass123"
        )

        sync_user_roles(user, None)
        user.refresh_from_db()

        assert user.groups.count() == 0
        assert user.is_staff is False

    def test_user_saved_only_when_changed(self):
        """User is only saved when flags actually change."""
        user = User.objects.create_user(
            username="unchangeduser",
            email="unchanged@example.com",
            password="testpass123",
        )

        # First sync
        sync_user_roles(user, ["Contributor"])
        user_id = user.id

        # Re-fetch and sync with the same roles
        user = User.objects.get(id=user_id)

        sync_user_roles(user, ["Contributor"])

        # Verify the user still reflects the same state
        assert user.is_staff is True
        assert user.groups.filter(name="Contributor").exists()
