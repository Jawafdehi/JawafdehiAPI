"""DRF permissions for the cases app.

Lives here rather than in ``jawafdehi_shared/drf/`` because these wrap
``cases.rules.predicates``, which knows about the Caseworker group; the shared
package is cross-service (nes/ngm/jawafdehi) and deliberately knows nothing
about this platform's role model.
"""

from rest_framework import permissions

from .rules.predicates import is_admin_or_moderator


class IsFeedbackTriager(permissions.BasePermission):
    """Superuser (admin) or the Caseworker content-staff role.

    The same principal set as ``review.permissions.IsContentStaff``, built on the
    same ``is_admin_or_moderator`` predicate. Kept as a second class rather than
    imported from ``review``: that app already imports ``cases``, and the reverse
    edge would close a cycle. Named for what it gates because triaging public
    feedback is not casework. **If you change the content-staff principal set,
    change both** — ``review/permissions.py`` carries a pointer back here.

    ReadOnly is deliberately excluded even though it is an org-wide *read* role
    and this class also gates reads. The systemwide-read invariant is about
    platform records; a feedback submission is a message from a member of the
    public, sometimes naming an official, and its audience is the people who act
    on it rather than everyone who can read the platform. JobPoller is excluded
    for the same reason — no machine identity needs to read reporter prose.
    """

    message = "Reading or triaging feedback requires the Caseworker role."

    def has_permission(self, request, view):
        user = request.user
        if not (user and user.is_authenticated):
            return False
        # is_admin_or_moderator already carries the is_superuser term (it is
        # documented as the only predicate that does), so don't re-OR it.
        return bool(is_admin_or_moderator(user))


class IsCaseImageUploader(permissions.BasePermission):
    """Content staff, for uploading a case thumbnail or hero image.

    Same principal set as ``IsFeedbackTriager`` / ``IsCaseAuthorPicker``, gating
    a different thing, so it is a separate class for the same reason they are.
    An upload here writes a permanent, publicly-readable object to R2 and adds a
    row to the shared Wagtail image library, which is not something an
    unauthenticated caller may do. ReadOnly is excluded: it is a read role, and
    an upload is a write.
    """

    message = "Uploading a case image requires the Caseworker role."

    def has_permission(self, request, view):
        user = request.user
        if not (user and user.is_authenticated):
            return False
        return bool(is_admin_or_moderator(user))


class IsCaseAuthorPicker(permissions.BasePermission):
    """Content staff, for the case-author picker's roster of accounts.

    Same principal set as ``IsFeedbackTriager``, gating a different thing, so it
    is a separate class rather than a reuse: this one hands out a list of staff
    names, and the reason to restrict it is that a roster of who works here is
    not public. ReadOnly is excluded because the picker exists only to build a
    byline, which ReadOnly cannot write anyway.
    """

    message = "Listing case-author candidates requires the Caseworker role."

    def has_permission(self, request, view):
        user = request.user
        if not (user and user.is_authenticated):
            return False
        return bool(is_admin_or_moderator(user))
