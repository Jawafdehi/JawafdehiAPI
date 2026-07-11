"""
Permission predicates for django-rules.

Predicates are reusable functions that return True/False for permission checks.
They can be combined using logical operators (&, |, ~) to create complex rules.
"""

from typing import TYPE_CHECKING, Optional

import rules
from django.contrib.auth.models import User

if TYPE_CHECKING:
    from cases.models import Case, CaseState


# ============================================================================
# Role-based Predicates
# ============================================================================


@rules.predicate
def is_admin(user: User) -> bool:
    """Check if user is an admin.

    v3 authz model: the ``Admin`` Django group is retired; admin == Django
    superuser (the Zitadel ``admin`` role sets ``is_superuser`` on every request;
    see ``jawafdehi_shared.auth.oidc``). User management is the admin-only
    capability (see ``can_manage_user``).
    """
    return user.is_superuser


@rules.predicate
def is_caseworker(user: User) -> bool:
    """Check if user is in the Caseworker group.

    v3 authz model: ``Caseworker`` is the single content-staff role — it carries
    the powers the retired ``Moderator`` role used to have (publish/close any
    case, edit any case, review-config, entities, CMS). The Zitadel keys
    ``moderator``, ``contributor`` (and legacy ``caseworker``) all map to this
    one group. Object-level assignment (the old ``Case.contributors``) is retired.
    """
    return user.groups.filter(name="Caseworker").exists()


@rules.predicate
def is_admin_or_moderator(user: User) -> bool:
    """Superuser or the single content-staff (Caseworker) role.

    Kept under this name to avoid churn at the many call sites; in the v3 model
    "admin or moderator" == "superuser or Caseworker". This is the ONLY predicate
    below that carries the ``is_superuser`` term, so combined predicates that
    must admit superusers (e.g. ``can_view_case``) must keep it.
    """
    return user.is_superuser or user.groups.filter(name="Caseworker").exists()


@rules.predicate
def has_role(user: User) -> bool:
    """Check if user has the content-staff role (superuser or Caseworker).

    In the v3 model this is identical to ``is_admin_or_moderator``; kept as a
    separate symbol for the DRF permission classes that call it by this name.
    """
    return user.is_superuser or user.groups.filter(name="Caseworker").exists()


@rules.predicate
def is_readonly(user: User) -> bool:
    """Check if user is in the org-wide ReadOnly group.

    ReadOnly is an assign-to-anyone role that grants system-wide read access
    INCLUDING casework (view draft/in-review cases, sources, and the review
    queue) via view_* model perms, but no write perms. It deliberately does NOT
    imply has_role, so write rules that build on is_admin_or_moderator continue
    to exclude ReadOnly users.
    """
    return user.groups.filter(name="ReadOnly").exists()


# ============================================================================
# Case-specific Predicates
# ============================================================================


@rules.predicate
def can_change_case(user: User, case: Optional["Case"] = None) -> bool:
    """Whether ``user`` may edit/delete ``case``.

    v3 authz model: object-level assignment is retired, so this no longer
    depends on the case — any content-staff user (superuser or Caseworker) may
    change any case. Kept as a 2-arg predicate (``case`` accepted but ignored)
    because call sites pass ``(user, case)`` — e.g. ``cases/api_views.py``
    partial_update/destroy — and it is also composed into django-rules rules.
    """
    return bool(is_admin_or_moderator(user))


def can_transition_case_state(
    user: User, case: Optional["Case"], to_state: "CaseState"
) -> bool:
    """
    Check if user can transition case from its current state to the target state.

    v3 authz model: there is a single content-staff role (Caseworker, +
    superuser) that can transition to ANY state, including PUBLISHED and CLOSED.
    The old Caseworker-confined-to-{DRAFT, IN_REVIEW} tier is retired with the
    Caseworker/Moderator collapse.

    Args:
        user: The user attempting the transition
        case: The case being transitioned (contains current state)
        to_state: The target state (CaseState enum value)

    Returns:
        bool: True if the transition is allowed
    """
    if case is None:
        return True

    # Content staff (superuser or Caseworker) can transition to any state.
    return bool(is_admin_or_moderator(user))


# ============================================================================
# User Management Predicates
# ============================================================================


@rules.predicate
def can_manage_user(user: User, target_user: Optional[User]) -> bool:
    """
    Check if user can manage the target user.

    v3 authz model: user management is superuser-only. The content-staff
    (Caseworker) role does NOT manage users, and the old
    "moderators can manage users except other moderators" asymmetry is retired.
    """
    return bool(is_admin(user))


# ============================================================================
# Combined Predicates for Common Patterns
# ============================================================================

# Case permissions
# These are the *casework* view predicates: they expose draft/in-review cases.
# ReadOnly joins so the org-wide read role can list AND retrieve all non-CLOSED
# cases (the retrieve() DRAFT gate uses can_view_case). Anonymous/unauthenticated
# callers see only PUBLISHED. Write predicates intentionally omit is_readonly.
#
# NOTE: ``is_admin_or_moderator`` is the only term here that carries the
# ``is_superuser`` short-circuit — do NOT drop it in favour of
# ``is_caseworker | is_readonly``, or a superuser (who has no group) would lose
# draft-case view. ``is_caseworker`` is now redundant with it but kept explicit.
can_view_case = is_admin_or_moderator | is_caseworker | is_readonly
# can_change_case is defined above as a 2-arg predicate (callers pass the case).

# Source permissions were removed with DocumentSource (ADR: cases own no
# documents). Documents are NGM Materials, gated by the NGM write role.
