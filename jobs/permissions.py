"""Permissions for the job queue API.

The queue is a platform-internal surface: enqueuing/claiming/finalizing work
mutates the queue and must be gated on a real role, while the read-only
dashboard (GET /api/jobs) is visible to any role with casework read access.

We reuse the review app's role predicates rather than invent a parallel system
(the first consumer, ``case_review``, is a casework job, and the poller already
authenticates as a Caseworker/ReviewAssistant service account). When new kinds
arrive that need a different role (e.g. an NGM-role material consumer), gate
per-kind inside the view rather than widening these classes.
"""

from rest_framework import permissions

from cases.rules.predicates import has_role


class CanConsumeJobs(permissions.BasePermission):
    """Allow claim/stage/result/enqueue — mutating queue operations.

    Satisfied by a superuser, any Admin/Moderator/Caseworker (``has_role``), or a
    ReviewAssistant service account (the poller). Anonymous, ReadOnly and Public
    are denied — they may observe the queue but not drive it.
    """

    message = "Driving the job queue requires at least the Caseworker role."

    def has_permission(self, request, view):
        user = request.user
        if not (user and user.is_authenticated):
            return False
        if user.is_superuser:
            return True
        if user.groups.filter(name="ReviewAssistant").exists():
            return True
        return bool(has_role(user))


class CanObserveJobs(permissions.BasePermission):
    """Allow the read-only queue dashboard (GET /api/jobs).

    Admits everyone ``CanConsumeJobs`` does, plus the org-wide ReadOnly role
    (observe without driving). Public is excluded.
    """

    message = "Observing the job queue requires a role with casework read access."

    def has_permission(self, request, view):
        user = request.user
        if not (user and user.is_authenticated):
            return False
        if user.is_superuser:
            return True
        if user.groups.filter(name__in=["ReviewAssistant", "ReadOnly"]).exists():
            return True
        return bool(has_role(user))
