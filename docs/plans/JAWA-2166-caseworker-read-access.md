# Plan: JAWA-2166 — Caseworker Read-Only Access

## Summary

Grant the existing **Contributor** role global read-only access to all case materials. No new group needed — "Caseworker" maps directly to the Contributor group. Contributors gain visibility to all cases/sources but retain their existing write restrictions (assigned cases only).

## Clarification

> **Contributor == Caseworker**: Contributors ARE caseworkers. There is no separate "Caseworker" group. This plan modifies existing Contributor permissions rather than creating a new role.

## Current State

### Existing Roles

| Role | is_staff | is_superuser | Access Pattern |
|------|----------|--------------|----------------|
| Admin | yes | yes | Full access to everything |
| Moderator | yes | no | Full access to cases/sources; can manage users |
| **Contributor** | yes | no | **Currently**: only assigned cases. **Goal**: all cases read-only, write only assigned |

### Current Layer 1 — `cases/rules/predicates.py`

| Predicate | Current Rule | Change |
|-----------|-------------|--------|
| `can_view_case` | `is_admin_or_moderator \| is_case_contributor` | Add `is_contributor` (→ all cases visible) |
| `can_change_case` | `is_admin_or_moderator \| is_case_contributor` | **Unchanged** (write stays assigned-only) |
| `can_view_source` | `is_admin_or_moderator \| is_source_contributor \| is_case_contributor_for_source` | Simplify to `is_admin_or_moderator \| is_contributor` (`is_contributor` subsumes the other two) |
| `can_change_source` | `is_admin_or_moderator \| is_source_contributor` | **Unchanged** |
| `can_delete_source` | `is_admin_or_moderator` | **Unchanged** |

### Current Layer 2 — DRF Permission Classes

| Resource | Current | Change |
|----------|---------|--------|
| CaseWorkflowRunViewSet | `IsAdminOrModerator` | Add Contributor read via SAFE_METHODS |
| EligibleCasesView | `IsAdminOrModerator` | Same |
| CaseViewSet (list) | Admin/Mod: all non-CLOSED. Contributor: PUBLISHED+assigned | Contributor sees all non-CLOSED |
| CaseViewSet (retrieve) | DRAFT requires `can_view_case` | Contributor now passes `can_view_case` for all |

## Changes Required

### 1. `cases/rules/predicates.py` — Update combined predicates

```python
# BEFORE:
can_view_case = is_admin_or_moderator | is_case_contributor
can_view_source = is_admin_or_moderator | is_source_contributor | is_case_contributor_for_source

# AFTER:
can_view_case = is_admin_or_moderator | is_contributor
can_view_source = is_admin_or_moderator | is_contributor
```

`is_contributor` subsumes `is_case_contributor`, `is_source_contributor`, and `is_case_contributor_for_source` — a Contributor already has all of these by virtue of being a group member. `can_change_case` / `can_change_source` / `can_delete_source` are **unchanged** (write still requires specific assignment).

### 2. `case_workflows/permissions.py` — New read-through permission

```python
class IsAdminOrModeratorOrContributorReadOnly(BasePermission):
    """Admin/Moderator full access; Contributors read-only access."""
    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        if request.user.is_superuser:
            return True
        if request.user.groups.filter(name__in=["Admin", "Moderator"]).exists():
            return True
        if request.method in permissions.SAFE_METHODS:
            return request.user.groups.filter(name="Contributor").exists()
        return False
```

### 3. `cases/api_views.py` — Update CaseViewSet visibility

- **`get_queryset()` list**: Add `is_contributor(request.user)` to the branch that returns all non-CLOSED cases (line ~242). Currently only `is_admin_or_moderator` gets this branch — Contributors instead get filtered PUBLISHED+assigned. After: Contributors see all non-CLOSED.
- **`retrieve()`**: No code change — `can_view_case` now includes Contributors via `is_contributor`, so DRAFT cases become visible to them.
- **`partial_update()`**: No change — `can_change_case` still uses `is_case_contributor` only, Contributors still need explicit assignment to edit a case.
- **`create()`**: No change — already requires `[IsAuthenticated()]` for create. Contributors can continue creating DRAFT cases.

### 4. `case_workflows/views.py` — Update permission classes

Replace `IsAdminOrModerator` with `IsAdminOrModeratorOrContributorReadOnly` on:
- `CaseWorkflowRunViewSet` (line 28)
- `EligibleCasesView` (line 98)

### 5. `cases/admin.py` — CaseAdminForm (optional QoL fix)

The `CaseAdminForm.__init__` already handles Contributor source filtering (lines 178–203): Contributors see assigned sources + sources referenced in their case's evidence. Since Contributors are now `is_staff` (already true), they can still access `/admin/`. The form logic already limits their source choices — no change strictly needed, but consider whether to relax the source filter too. Recommend: **no change** (admin form is already correct for write operations; read-only users can click through to sources directly).

### 6. Tests — `tests/test_contributor_read_permissions.py`

Test properties:
1. Contributor can list all cases (including DRAFT/IN_REVIEW) — regression from PUBLISHED+assigned
2. Contributor can retrieve any case (including unassigned DRAFT)
3. Contributor cannot patch unassigned cases → 403
4. Contributor can patch assigned cases (existing behavior, regression check)
5. Contributor can list/retrieve workflow runs
6. Contributor cannot resume workflow runs
7. Contributor can view eligible cases
8. Contributor cannot create document sources (unchanged)
9. User without role still has no access (regression check)

### No database changes

No migrations, no new groups. The Contributor group already exists. Changes are purely in permission logic.

## Files Changed

| File | Change |
|------|--------|
| `cases/rules/predicates.py` | Update `can_view_case`, `can_view_source` to include `is_contributor` |
| `case_workflows/permissions.py` | Add `IsAdminOrModeratorOrContributorReadOnly` |
| `cases/api_views.py` | Update `CaseViewSet.get_queryset()` list — add Contributors to all-non-CLOSED branch |
| `case_workflows/views.py` | Update `CaseWorkflowRunViewSet` and `EligibleCasesView` permission classes |
| `tests/test_contributor_read_permissions.py` | New test file |

## What Does NOT Change

- **Case modification** — `can_change_case`/`can_change_source` unchanged.
- **Workflow mutations** — Contributors cannot resume/create/modify workflow runs.
- **Case creation** — Contributors can still create DRAFT cases (already true today).
- **NGM/NESQ** — No permission changes.
- **Summary/Draft ownership** — Still owner-scoped via queryset.
- **Django Admin** — Contributors remain `is_staff` (already true); no change.

## Rollback

Revert the commit. Pure logic change — no data to migrate.
