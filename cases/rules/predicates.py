"""
Permission predicates for django-rules.

Predicates are reusable functions that return True/False for permission checks.
They can be combined using logical operators (&, |, ~) to create complex rules.
"""

from typing import TYPE_CHECKING, Optional

import rules
from django.contrib.auth.models import User

if TYPE_CHECKING:
    from cases.models import Case, CaseState, DocumentSource


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
def is_contributor(user: User) -> bool:
    """Check if user is in the Contributor group."""
    return user.groups.filter(name="Contributor").exists()


@rules.predicate
def is_admin_or_moderator(user: User) -> bool:
    """Check if user is Admin or Moderator."""
    return (
        user.is_superuser
        or user.groups.filter(name__in=["Admin", "Moderator"]).exists()
    )


@rules.predicate
def has_role(user: User) -> bool:
    """Check if user has any role (Admin, Moderator, or Contributor)."""
    return user.groups.filter(name__in=["Admin", "Moderator", "Contributor"]).exists()


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
    - Contributors: Can only transition when BOTH source and destination states are in {DRAFT, IN_REVIEW}

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

    # Contributors can only transition when both states are in {DRAFT, IN_REVIEW}
    if is_contributor(user):
        allowed_states = {CaseState.DRAFT, CaseState.IN_REVIEW}
        return case.state in allowed_states and to_state in allowed_states

    return False


# ============================================================================
# DocumentSource-specific Predicates
# ============================================================================


@rules.predicate
def is_source_contributor(user: User, source: Optional["DocumentSource"]) -> bool:
    """
    Check if user is assigned as a contributor to the source.

    Note: This is a pure assignment check. Admins/Moderators are NOT automatically
    considered contributors. Use combined predicates like (is_admin_or_moderator | is_source_contributor)
    for permission rules where Admins/Moderators should have access to all sources.
    """
    if source is None:
        return False
    return source.contributors.filter(id=user.id).exists()


@rules.predicate
def is_case_contributor_for_source(
    user: User, source: Optional["DocumentSource"]
) -> bool:
    """
    Check if user is a contributor to any case that references this source in its evidence.

    Args:
        user: The user to check
        source: The DocumentSource to check

    Returns:
        bool: True if the source appears in evidence of any case the user contributes to
    """
    if source is None:
        return False

    from cases.models import Case

    # Query all cases where user is a contributor
    cases_to_check = Case.objects.filter(contributors=user)

    # Check if source_id appears in evidence of any of these cases
    for case in cases_to_check:
        if case.evidence:
            for evidence_item in case.evidence:
                if (
                    isinstance(evidence_item, dict)
                    and evidence_item.get("source_id") == source.source_id
                ):
                    return True

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
can_view_case = is_admin_or_moderator | is_contributor
can_change_case = is_admin_or_moderator | is_case_contributor

# Source permissions
can_view_source = is_admin_or_moderator | is_contributor
can_change_source = is_admin_or_moderator | is_source_contributor
can_delete_source = is_admin_or_moderator

# User management permissions
can_manage_user_account = is_admin | can_manage_user
