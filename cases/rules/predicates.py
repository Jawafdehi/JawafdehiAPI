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
    """Check if user is in the Admin group."""
    return user.is_superuser or user.groups.filter(name="Admin").exists()


@rules.predicate
def is_moderator(user: User) -> bool:
    """Check if user is in the Moderator group."""
    return user.groups.filter(name="Moderator").exists()


@rules.predicate
def is_caseworker(user: User) -> bool:
    """Check if user is in the Caseworker group.

    "Caseworker" is the v2 name for the role formerly called "contributor": a
    content role that can create/edit casework. NOTE: this is distinct from the
    ``is_case_contributor`` / ``is_source_contributor`` predicates below, which
    are per-object *assignment* checks against the ``Case.contributors`` /
    ``DocumentSource.contributors`` model fields (unrelated to the role name).
    """
    return user.groups.filter(name="Caseworker").exists()


@rules.predicate
def is_admin_or_moderator(user: User) -> bool:
    """Check if user is Admin or Moderator."""
    return (
        user.is_superuser
        or user.groups.filter(name__in=["Admin", "Moderator"]).exists()
    )


@rules.predicate
def has_role(user: User) -> bool:
    """Check if user has any content role (Admin, Moderator, or Caseworker)."""
    return user.groups.filter(name__in=["Admin", "Moderator", "Caseworker"]).exists()


@rules.predicate
def is_readonly(user: User) -> bool:
    """Check if user is in the org-wide ReadOnly group.

    ReadOnly is an assign-to-anyone role that grants system-wide read access
    INCLUDING casework (view draft/in-review cases, sources, and the review
    queue) via view_* model perms, but no write perms. It deliberately does NOT
    imply has_role, so write rules that build on is_admin_or_moderator /
    is_*_contributor continue to exclude ReadOnly users.
    """
    return user.groups.filter(name="ReadOnly").exists()


@rules.predicate
def is_public(user: User) -> bool:
    """Check if user is in the Public group.

    Public is read-only like ReadOnly, but EXCLUDES casework: a Public user can
    read the public surface (PUBLISHED cases) but cannot view draft/in-review
    casework, draft-only sources, or the review queue. It does NOT join the
    casework view predicates (can_view_case / can_view_source / CanReadReview)
    and, like ReadOnly, does not imply has_role.
    """
    return user.groups.filter(name="Public").exists()


# ============================================================================
# Case-specific Predicates
# ============================================================================


@rules.predicate
def is_case_contributor(user: User, case: Optional["Case"]) -> bool:
    """
    Check if user is assigned as a contributor to the case.

    Note: This is a pure assignment check. Admins/Moderators are NOT automatically
    considered contributors. Use combined predicates like (is_admin_or_moderator | is_case_contributor)
    for permission rules where Admins/Moderators should have access to all cases.
    """
    if case is None:
        return False
    return case.contributors.filter(id=user.id).exists()


def can_transition_case_state(
    user: User, case: Optional["Case"], to_state: "CaseState"
) -> bool:
    """
    Check if user can transition case from its current state to the target state.

    Rules:
    - Admins and Moderators: Can transition to any state
    - Caseworkers: Can only transition when BOTH source and destination states are in {DRAFT, IN_REVIEW}

    Args:
        user: The user attempting the transition
        case: The case being transitioned (contains current state)
        to_state: The target state (CaseState enum value)

    Returns:
        bool: True if the transition is allowed
    """
    from cases.models import CaseState

    if case is None:
        return True

    # Admins and Moderators can transition to any state
    if is_admin_or_moderator(user):
        return True

    # Caseworkers can only transition when both states are in {DRAFT, IN_REVIEW}
    if is_caseworker(user):
        allowed_states = {CaseState.DRAFT, CaseState.IN_REVIEW}
        return case.state in allowed_states and to_state in allowed_states

    return False


# ============================================================================
# User Management Predicates
# ============================================================================


@rules.predicate
def can_manage_user(user: User, target_user: Optional[User]) -> bool:
    """
    Check if user can manage the target user.

    Rules:
    - Admins: Can manage all users
    - Moderators: Can manage all users EXCEPT other Moderators
    """
    if target_user is None:
        return True

    # Admins can manage everyone
    if is_admin(user):
        return True

    # Moderators cannot manage other Moderators
    if is_moderator(user):
        return not is_moderator(target_user)

    return False


# ============================================================================
# Combined Predicates for Common Patterns
# ============================================================================

# Case permissions
# These are the *casework* view predicates: they expose draft/in-review cases.
# ReadOnly joins so the org-wide read role can list AND retrieve all non-CLOSED
# cases (the retrieve() DRAFT gate uses can_view_case). Public is deliberately
# EXCLUDED — it is a public-surface read role with no casework access. Write
# predicates intentionally omit both is_readonly and is_public.
can_view_case = is_admin_or_moderator | is_caseworker | is_readonly
can_change_case = is_admin_or_moderator | is_case_contributor

# Source permissions were removed with DocumentSource (ADR: cases own no
# documents). Documents are NGM Materials, gated by the NGM write role.

# User management permissions
can_manage_user_account = is_admin | can_manage_user
