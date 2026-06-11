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
        # has_role covers Admin / Moderator / Contributor. ReviewAssistant is a
        # review-system role (manages reviews + document sources) that also gets
        # in here without being a general content contributor.
        if user.groups.filter(name="ReviewAssistant").exists():
            return True
        # has_role is a django-rules predicate; call it directly.
        return bool(has_role(user))


class CanReadReview(permissions.BasePermission):
    """Allow read access to the Casework Review System.

    Admits superuser, Admin, Moderator, Contributor, ReviewAssistant, and the
    org-wide ReadOnly role. Used only on GET endpoints (review list/detail,
    rules, config read, ``me``); mutation endpoints keep HasContributorRole,
    which deliberately excludes ReadOnly. This is what lets a read-only role
    observe the queue without being able to claim jobs, submit results, or
    re-queue reviews.
    """

    message = "Reading the Casework Review System requires a role with read access."

    def has_permission(self, request, view):
        user = request.user
        if not (user and user.is_authenticated):
            return False
        if user.is_superuser:
            return True
        # ReviewAssistant + ReadOnly get read access by group name; the rest
        # (Admin / Moderator / Contributor) come through has_role.
        if user.groups.filter(name__in=["ReviewAssistant", "ReadOnly"]).exists():
            return True
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


class CanManageDocumentSources(permissions.BasePermission):
    """Allow users who may update document sources.

    Used by the poller's source-markdown maintenance endpoint. Satisfied by a
    superuser or anyone holding the ``cases.change_documentsource`` permission
    (Admin / Moderator / Contributor / ReviewAssistant via group perms).
    """

    message = "Updating document sources requires the change_documentsource permission."

    def has_permission(self, request, view):
        user = request.user
        if not (user and user.is_authenticated):
            return False
        if user.is_superuser:
            return True
        return user.has_perm("cases.change_documentsource")
