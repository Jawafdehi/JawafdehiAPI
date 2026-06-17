"""Tests for config.roles.sync_user_roles.

Role semantics (IdP-authoritative, full overwrite of the managed groups):
  - `admin`            -> Admin group + is_superuser
  - `staff`            -> is_staff only (no group, no perms)
  - moderator/contributor/readonly/review_assistant -> matching content group
  - is_staff/is_superuser come from the explicit staff/admin roles, NOT derived
    from content-group membership.
"""

from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group

from config.roles import sync_user_roles

User = get_user_model()


@pytest.mark.django_db
class TestSyncUserRoles:
    def test_staff_role_sets_is_staff_only(self):
        """`staff` sets is_staff and creates no group / no superuser."""
        user = User.objects.create_user(
            username="staffuser", email="staff@example.com", password="x"
        )
        sync_user_roles(user, ["staff"])
        user.refresh_from_db()

        assert user.is_staff is True
        assert user.is_superuser is False
        assert user.groups.count() == 0

    def test_admin_role_sets_superuser_and_group(self):
        """`admin` sets is_superuser and the Admin group (is_staff stays off without `staff`)."""
        user = User.objects.create_user(
            username="adminuser", email="admin@example.com", password="x"
        )
        sync_user_roles(user, ["admin"])
        user.refresh_from_db()

        assert user.is_superuser is True
        assert user.groups.filter(name="Admin").exists()
        assert user.is_staff is False

    def test_content_role_creates_group_without_flags(self):
        """A content role maps to its group and sets no is_staff/is_superuser."""
        user = User.objects.create_user(
            username="contrib", email="contrib@example.com", password="x"
        )
        sync_user_roles(user, ["contributor"])
        user.refresh_from_db()

        assert user.groups.filter(name="Contributor").exists()
        assert user.is_staff is False
        assert user.is_superuser is False

    def test_readonly_sets_no_flags(self):
        user = User.objects.create_user(
            username="ro", email="ro@example.com", password="x"
        )
        sync_user_roles(user, ["readonly"])
        user.refresh_from_db()

        assert user.groups.filter(name="ReadOnly").exists()
        assert user.is_staff is False
        assert user.is_superuser is False

    def test_admin_and_staff_together(self):
        user = User.objects.create_user(
            username="both", email="both@example.com", password="x"
        )
        sync_user_roles(user, ["admin", "staff"])
        user.refresh_from_db()

        assert user.is_superuser is True
        assert user.is_staff is True
        assert user.groups.filter(name="Admin").exists()
        # staff contributes no group, so Admin is the only managed group.
        assert set(user.groups.values_list("name", flat=True)) == {"Admin"}

    def test_full_overwrite_removes_absent_roles(self):
        """Roles no longer present in the claim are revoked (groups + flags)."""
        user = User.objects.create_user(
            username="overwrite", email="ow@example.com", password="x"
        )
        sync_user_roles(user, ["admin", "staff", "contributor"])
        user.refresh_from_db()
        assert user.is_superuser is True
        assert user.is_staff is True

        sync_user_roles(user, ["contributor"])
        user.refresh_from_db()

        assert user.is_superuser is False
        assert user.is_staff is False
        assert set(user.groups.values_list("name", flat=True)) == {"Contributor"}

    def test_non_managed_group_is_left_untouched(self):
        user = User.objects.create_user(
            username="mixed", email="mixed@example.com", password="x"
        )
        custom, _ = Group.objects.get_or_create(name="CustomGroup")
        user.groups.add(custom)

        sync_user_roles(user, ["moderator"])
        user.refresh_from_db()

        assert user.groups.filter(name="Moderator").exists()
        assert user.groups.filter(name="CustomGroup").exists()

    def test_case_insensitive_role_keys(self):
        user = User.objects.create_user(
            username="case", email="case@example.com", password="x"
        )
        sync_user_roles(user, ["ADMIN", "Contributor", "STAFF"])
        user.refresh_from_db()

        assert user.groups.filter(name="Admin").exists()
        assert user.groups.filter(name="Contributor").exists()
        assert user.is_superuser is True
        assert user.is_staff is True

    def test_unknown_roles_are_ignored(self):
        user = User.objects.create_user(
            username="unknown", email="unknown@example.com", password="x"
        )
        sync_user_roles(user, ["contributor", "bogus", "superadmin"])
        user.refresh_from_db()

        assert set(user.groups.values_list("name", flat=True)) == {"Contributor"}

    def test_non_string_claim_items_are_ignored(self):
        """A malformed claim (non-string items) must not raise."""
        user = User.objects.create_user(
            username="malformed", email="malformed@example.com", password="x"
        )
        sync_user_roles(user, ["contributor", 123, None, {"role": "admin"}])
        user.refresh_from_db()

        assert set(user.groups.values_list("name", flat=True)) == {"Contributor"}
        assert user.is_superuser is False

    def test_none_list_is_handled(self):
        user = User.objects.create_user(
            username="none", email="none@example.com", password="x"
        )
        sync_user_roles(user, None)
        user.refresh_from_db()

        assert user.groups.count() == 0
        assert user.is_staff is False
        assert user.is_superuser is False

    def test_user_not_saved_when_flags_unchanged(self):
        """sync_user_roles must not call user.save() when nothing changed."""
        user = User.objects.create_user(
            username="unchanged", email="unchanged@example.com", password="x"
        )
        sync_user_roles(user, ["staff"])  # flips is_staff -> one save
        user = User.objects.get(id=user.id)
        assert user.is_staff is True

        with patch.object(user, "save", wraps=user.save) as mock_save:
            sync_user_roles(user, ["staff"])
            mock_save.assert_not_called()

        assert user.is_staff is True
