"""Permissions for the Casework Review System.

Per the VOL-3 directive: "update the system so that a minimum of caseworker
role is needed to access the review system. We'll use JWTs."

The jawafdehi-api already models roles as Django auth Groups (Admin, Moderator,
Caseworker) with predicates in ``cases.rules.predicates``. We reuse the
``has_role`` predicate (true for any of Admin / Moderator / Caseworker, or a
superuser) rather than inventing a parallel role system.

Authentication itself is handled by DRF's configured classes. As of the
phase5 OIDC-only migration the sole authenticator is
jawafdehi_shared.auth.oidc.OIDCAuthentication (the global DEFAULT_AUTHENTICATION_CLASSES);
clients send a Zitadel access token as ``Authorization: Bearer <access>``. The
old SimpleJWT path has been removed.
"""

from rest_framework import permissions

from cases.rules.predicates import has_role, is_admin_or_moderator


class HasContributorRole(permissions.BasePermission):
    """Allow only authenticated users with at least the Caseworker role.

    "At least Caseworker" means the user is in the Caseworker, Moderator, or
    Admin group (or is a superuser). Anonymous users and authenticated users
    with no role (incl. the read-only ReadOnly / Public roles) are denied.

    NOTE: the class name is retained for now to avoid churn at every call site;
    it gates the *write* surface of the review system on the caseworker role.
    """

    message = (
        "Access to the Casework Review System requires at least the "
        "Caseworker role."
    )

    def has_permission(self, request, view):
        user = request.user
        if not (user and user.is_authenticated):
            return False
        if user.is_superuser:
            return True
        # has_role covers Admin / Moderator / Caseworker. ReviewAssistant is a
        # review-system role (manages reviews + document sources) that also gets
        # in here without being a general content caseworker.
        if user.groups.filter(name="ReviewAssistant").exists():
            return True
        # has_role is a django-rules predicate; call it directly.
        return bool(has_role(user))


class CanReadReview(permissions.BasePermission):
    """Allow read access to the Casework Review System.

    Admits superuser, Admin, Moderator, Caseworker, ReviewAssistant, and the
    org-wide ReadOnly role (ReadOnly includes casework view). The Public role is
    deliberately EXCLUDED — it has no casework access. Used only on GET
    endpoints (review list/detail, rules, config read, ``me``); mutation
    endpoints keep HasContributorRole, which deliberately excludes ReadOnly and
    Public. This is what lets a read-only role observe the queue without being
    able to claim jobs, submit results, or re-queue reviews.
    """

    message = "Reading the Casework Review System requires a role with read access."

    def has_permission(self, request, view):
        user = request.user
        if not (user and user.is_authenticated):
            return False
        if user.is_superuser:
            return True
        # ReviewAssistant + ReadOnly get read access by group name; the rest
        # (Admin / Moderator / Caseworker) come through has_role. Public is
        # intentionally NOT listed here — it has no casework read access.
        if user.groups.filter(name__in=["ReviewAssistant", "ReadOnly"]).exists():
            return True
        return bool(has_role(user))


class IsAdminOrModerator(permissions.BasePermission):
    """Allow only Admin / Moderator (or superuser).

    Used to gate review-wide settings (e.g. the global scoring thresholds) that
    a plain Caseworker should be able to read but not change.
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
    (Admin / Moderator / Caseworker / ReviewAssistant via group perms).
    """

    message = "Updating document sources requires the change_documentsource permission."

    def has_permission(self, request, view):
        user = request.user
        if not (user and user.is_authenticated):
            return False
        if user.is_superuser:
            return True
        return user.has_perm("cases.change_documentsource")
