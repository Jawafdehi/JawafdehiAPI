"""Permissions for the Casework Review System.

Per the VOL-3 directive: "update the system so that a minimum of contributor
role is needed to access the review system. We'll use JWTs."

The jawafdehi-api already models roles as Django auth Groups (Admin, Moderator,
Contributor) with predicates in ``cases.rules.predicates``. We reuse the
``has_role`` predicate (true for any of Admin / Moderator / Contributor, or a
superuser) rather than inventing a parallel role system.

Authentication itself is handled by DRF's configured classes; the review URLs
pin JWT first (see settings.SIMPLE_JWT + the project's
DEFAULT_AUTHENTICATION_CLASSES, which lead with JWTAuthentication).
"""

from rest_framework import permissions

from cases.rules.predicates import has_role, is_admin_or_moderator


class HasContributorRole(permissions.BasePermission):
    """Allow only authenticated users with at least the Contributor role.

    "At least Contributor" means the user is in the Contributor, Moderator, or
    Admin group (or is a superuser). Anonymous users and authenticated users
    with no role are denied.
    """

    message = (
        "Access to the Casework Review System requires at least the "
        "Contributor role."
    )

    def has_permission(self, request, view):
        user = request.user
        if not (user and user.is_authenticated):
            return False
        if user.is_superuser:
            return True
        # has_role is a django-rules predicate; call it directly.
        return bool(has_role(user))


class IsAdminOrModerator(permissions.BasePermission):
    """Allow only Admin / Moderator (or superuser).

    Used to gate review-wide settings (e.g. the global scoring thresholds) that
    a plain Contributor should be able to read but not change.
    """

    message = "This action requires the Admin or Moderator role."

    def has_permission(self, request, view):
        user = request.user
        if not (user and user.is_authenticated):
            return False
        if user.is_superuser:
            return True
        return bool(is_admin_or_moderator(user))
