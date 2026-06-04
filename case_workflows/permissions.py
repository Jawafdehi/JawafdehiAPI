from rest_framework.permissions import BasePermission, SAFE_METHODS


class IsAdminOrModerator(BasePermission):
    """
    Allows access only to Admin or Moderator group members (and superusers).

    Mirrors the ``is_admin_or_moderator`` predicate in ``cases/rules/``.
    """

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        if request.user.is_superuser:
            return True
        return request.user.groups.filter(name__in=["Admin", "Moderator"]).exists()


class IsAdminOrModeratorOrContributorReadOnly(BasePermission):
    """
    Admin/Moderator get full access; Contributors get read-only access.

    Mirrors the expanded ``can_view_case`` / ``can_view_source`` predicates.
    """

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        if request.user.is_superuser:
            return True
        if request.user.groups.filter(name__in=["Admin", "Moderator"]).exists():
            return True
        if request.method in SAFE_METHODS:
            return request.user.groups.filter(name="Contributor").exists()
        return False
