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
        "Access to the Casework Review System requires at least the " "Caseworker role."
    )

    def has_permission(self, request, view):
        user = request.user
        if not (user and user.is_authenticated):
            return False
        if user.is_superuser:
            return True
        # has_role covers the Caseworker content-staff role. JobPoller is the
        # machine role (the review poller) that also drives the review system
        # without being a content caseworker.
        if user.groups.filter(name="JobPoller").exists():
            return True
        # has_role is a django-rules predicate; call it directly.
        return bool(has_role(user))


class CanReadReview(permissions.BasePermission):
    """Allow read access to the Casework Review System.

    Admits superuser, the Caseworker content-staff role, the JobPoller machine
    role, and the org-wide ReadOnly role (ReadOnly includes casework view).
    Unauthenticated/no-role callers are excluded. Used only on GET endpoints
    (review list/detail, rules, config read, ``me``); mutation endpoints keep
    HasContributorRole, which deliberately excludes ReadOnly. This is what lets
    a read-only role observe the queue without being able to claim jobs, submit
    results, or re-queue reviews.
    """

    message = "Reading the Casework Review System requires a role with read access."

    def has_permission(self, request, view):
        user = request.user
        if not (user and user.is_authenticated):
            return False
        if user.is_superuser:
            return True
        # JobPoller + ReadOnly get read access by group name; the Caseworker
        # content role comes through has_role.
        if user.groups.filter(name__in=["JobPoller", "ReadOnly"]).exists():
            return True
        return bool(has_role(user))


class IsContentStaff(permissions.BasePermission):
    """Allow the content-staff role (Caseworker) or a superuser.

    v3 authz model: there is one content role (Caseworker, folding in the old
    Moderator). Used to gate review-wide settings (e.g. the global scoring
    thresholds) to content staff; ReadOnly may read but not change them.
    """

    message = "This action requires the Caseworker role."

    def has_permission(self, request, view):
        user = request.user
        if not (user and user.is_authenticated):
            return False
        if user.is_superuser:
            return True
        return bool(is_admin_or_moderator(user))
