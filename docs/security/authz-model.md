# Authorization Model — the single reference

> **Status:** descriptive. This file is a hand-maintained *census* of every
> authorization action and special rule currently enforced in `jawafdehi-api`,
> produced by a full code scan (2026-07-11). It is the intended single source of
> truth for **who can do what**. Enforcement today is still scattered across the
> files cited in each row (see [Enforcement surfaces](#enforcement-surfaces));
> the goal is to make those surfaces *reference* this model rather than
> re-declare it. Until then, **if code and this doc disagree, code wins — and
> that's a bug to fix here.**

## 1. Principals (roles)

Identity is OIDC/Zitadel only. Zitadel is the source of truth for roles; the
DRF authenticator (`jawafdehi_shared/auth/oidc.py`) validates the JWT and, on
**every request**, overwrites the Django user's Group membership from the
token's project-role claim (`urn:zitadel:iam:org:project:roles`). Django Groups
are therefore a cache of Zitadel roles, never edited by hand in prod.

| Zitadel role key | Django Group | Meaning |
|---|---|---|
| `admin` | `Admin` | Full CRUD. **Also sets `is_superuser=True`** (and clears it when the role is absent). |
| `moderator` | `Moderator` | Full casework CRUD; grants Django-admin `is_staff`. Cannot manage other Moderators. |
| `caseworker` | `Caseworker` | Content author/editor (was "Contributor"). Case writes are assignment-gated. |
| `readonly` | `ReadOnly` | Org-wide read **including casework** (draft/in-review cases, sources, review queue). No writes. |
| `public` | `Public` | Public-surface read only. **Excludes** casework. No writes. |
| `review_assistant` | `ReviewAssistant` | Review-system helper: read + drive the review queue, but not a general content role. |
| `ngm_silver` / `ngm_gold` / `ngm_platinum` | `NGM_SilverTier` / `GoldTier` / `PlatinumTier` | Court-data (NGM) rate tiers; also grant the NGM write/query planes. |

Special non-role principals:

- **`superuser`** — short-circuits to allow in nearly every check (`is_superuser`
  returns True first). Set only by the `admin` role.
- **Anonymous** — unauthenticated. Sees only the public surface.
- **Service account** (poller/MCP) — a Zitadel machine user. Indistinguishable
  from a human at transport; recognized out-of-band by `sub` ∈
  `OIDC_SERVICE_ACCOUNT_SUBJECTS`. Carries a real role (poller = `Caseworker` /
  `ReviewAssistant`).

## 2. Cross-cutting rules (read these first)

These hold everywhere unless a row below says otherwise:

- **R1 — Superuser override.** `is_superuser` is checked first in every
  permission class and most predicates → allow. (`admin` role ⇒ superuser.)
- **R2 — Read-public default.** The global DRF default is
  `ReadOnlyOrAuthenticatedWrite` (`jawafdehi_shared/drf/base.py`): safe methods
  (GET/HEAD/OPTIONS) are open to everyone; unsafe methods require authentication.
  Per-view classes layer role checks on top.
- **R3 — 401 vs 403.** The OIDC authenticator sets `WWW-Authenticate`, so
  *unauthenticated* → **401**; *authenticated but under-privileged* → **403**.
- **R4 — ReadOnly ≠ has_role.** `ReadOnly`/`Public` deliberately do **not** imply
  any content role, so every write rule built on `has_role` /
  `is_admin_or_moderator` excludes them.
- **R5 — Zitadel authoritative on every request.** Group + `is_superuser` +
  `is_staff` are re-synced from the token each request; revoking a role in
  Zitadel takes effect immediately.
- **R6 — Object-level gates are separate from role gates.** Some case writes
  require BOTH a role/model-permission *and* a per-object assignment check
  (`case.contributors`). See §4.

## 3. Role catalogue as code (the sets to centralize)

The same role-sets are re-declared in ≥5 files today. Canonical definitions:

| Set name | Members | Declared today in |
|---|---|---|
| `CONTENT_ROLES` (`has_role`) | Admin, Moderator, Caseworker | `cases/rules/predicates.py` |
| `ENTITY_WRITE_GROUPS` | Caseworker, Moderator, Admin | `entities/permissions.py` — **duplicate of `CONTENT_ROLES`** |
| `ENTITY_ADMIN_GROUPS` | Moderator, Admin | `entities/permissions.py` |
| `NGM_ROLE_GROUPS` | Admin, Moderator, Caseworker, NGM_{Silver,Gold,Platinum}Tier | `courts/permissions.py` |
| Review-read set | Admin, Moderator, Caseworker, ReviewAssistant, ReadOnly | `review/permissions.py` (inline) |
| Jobs-consume set | Admin, Moderator, Caseworker, ReviewAssistant | `jobs/permissions.py` (inline) |
| `STAFF_ROLES` (Django admin) | admin, moderator | `config/oidc_admin.py` |
| Wagtail editor set | Admin, Moderator, Caseworker | `content/permissions.py` |

> **Centralization note:** `ENTITY_WRITE_GROUPS` and `has_role` are the same set
> written twice. These are the first things to collapse to one import.

## 4. Action matrix

Legend: ✅ allow · ❌ deny · **A** authenticated (any role) · **cond** conditional (see notes).
Superuser (R1) is allow for every row and omitted from columns.

### 4a. Cases (`cases/api_views.py`, `cases/rules/predicates.py`)

| Action | Anon | Public | ReadOnly | Caseworker | Moderator | Admin | Rule / notes |
|---|---|---|---|---|---|---|---|
| List/retrieve PUBLISHED case | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | Public surface; CLOSED never exposed. |
| List/retrieve DRAFT / IN_REVIEW case | ❌ | ❌ | ✅ | ✅ | ✅ | ✅ | `can_view_case = is_admin_or_moderator │ is_caseworker │ is_readonly`. Other authed users: only cases they're a contributor on (queryset scope). |
| Create case (POST) | ❌ | ❌ | ❌ | ✅ | ✅ | ✅ | `IsAuthenticated + DjangoModelPermissions` → `cases.add_case`. Forced to DRAFT state. |
| Change case (PATCH) | ❌ | ❌ | ❌ | cond | ✅ | ✅ | `can_change_case = is_admin_or_moderator │ is_case_contributor`. **Caseworker only if assigned** (`case.contributors`) — R6. |
| Delete case (soft→CLOSED) | ❌ | ❌ | ❌ | cond | ✅ | ✅ | `DjangoModelPermissions` → `cases.delete_case` (Caseworker lacks it by default) **AND** `can_change_case`. Effectively Moderator/Admin. |
| Transition case state | ❌ | ❌ | ❌ | cond | ✅ | ✅ | `can_transition_case_state`: Admin/Mod → any; Caseworker → only when **both** from- and to-state ∈ {DRAFT, IN_REVIEW}. |

### 4b. Entities (`entities/views.py`, `entities/permissions.py`)

| Action | Anon | Public/ReadOnly | Caseworker | Moderator | Admin | Rule / notes |
|---|---|---|---|---|---|---|
| List / search / get entity | ✅ | ✅ | ✅ | ✅ | ✅ | `AllowAny` on GET. |
| Create / patch / delete entity | ❌ | ❌ | ✅ | ✅ | ✅ | `HasEntityWriteRole` = {Caseworker, Moderator, Admin}. |
| Reindex entities | ❌ | ❌ | ❌ | ✅ | ✅ | `HasEntityAdminRole` = {Moderator, Admin}. |

### 4c. Courts + Materials — court-data plane (`courts/`, `materials/`)

| Action | Anon | Public/ReadOnly | Caseworker | NGM tiers | Moderator/Admin | Rule / notes |
|---|---|---|---|---|---|---|
| Read court / material (public visibility) | ✅ | ✅ | ✅ | ✅ | ✅ | `AllowAny` on GET. |
| See PRIVATE (draft-only) material | ❌ | ✅ (ReadOnly) · ❌ (Public) | ✅ | ✅ | ✅ | `materials._can_see_nonpublic`: any staff-ish/NGM-role/superuser principal. |
| Write court (POST/PUT) | ❌ | ❌ | ✅ | ✅ | ✅ | `HasNgmRole` (writes). |
| Write / upsert material | ❌ | ❌ | ✅ | ✅ | ✅ | `HasNgmRole` enforced manually in `@api_view` handlers. |
| Gated SQL query plane | ❌ | ❌ | ✅ | ✅ | ✅ | `HasNgmQueryAccess` = `HasNgmRole` **OR** OAuth scope `ngm.query` (**cond**: scope-only tokens like MCP pass without a role). |
| NGM ingestion | ❌ | ❌ | ✅ | ✅ | ✅ | `HasNgmRole`. |

### 4d. Review system (`review/`)

| Action | Anon | Public | ReadOnly | ReviewAssistant | Caseworker | Moderator/Admin | Rule / notes |
|---|---|---|---|---|---|---|---|
| Read review queue / rules / config / `me` | ❌ | ❌ | ✅ | ✅ | ✅ | ✅ | `CanReadReview`. Public excluded. |
| Drive review (claim/submit/regrade, mutations) | ❌ | ❌ | ❌ | ✅ | ✅ | ✅ | `HasContributorRole` (`has_role` + ReviewAssistant). |
| Change global review config (PUT) | ❌ | ❌ | ❌ | ❌ | ❌ | ✅ | `config_view` is GET+PUT one view; PUT gated **inline** by `IsAdminOrModerator`. |

### 4e. Jobs queue (`jobs/`)

| Action | Anon | Public/ReadOnly | ReviewAssistant | Caseworker | Moderator/Admin | Rule / notes |
|---|---|---|---|---|---|---|
| Observe queue (GET /api/jobs) | ❌ | ✅ (ReadOnly) · ❌ (Public) | ✅ | ✅ | ✅ | `CanObserveJobs` (adds ReadOnly to consume set). |
| Consume queue (enqueue/claim/stage/result) | ❌ | ❌ | ✅ | ✅ | ✅ | `CanConsumeJobs`. |

### 4f. Search (`search/views.py`)

| Action | Anon | everyone | Rule |
|---|---|---|---|
| Unified search | ✅ | ✅ | `AllowAny`. Result *visibility* is enforced downstream by per-type queryset scoping, not here. |

### 4g. Django admin (`cases/admin.py`, `config/oidc_admin.py`)

| Action | Rule / notes |
|---|---|
| Access `/django-admin` at all | `is_staff`, granted to `STAFF_ROLES = {admin, moderator}` (superuser always). Synced from the userinfo `roles` claim by `AdminOIDCBackend`. |
| Case / relationship / state-change admin | **View-only for everyone** — all `has_add/change/delete_permission` return `False`; writes go through the SPA `/admin`. `has_view_permission` delegates to `can_view_case`. Slug editable only while DRAFT. |
| Manage users (User admin) | `can_manage_user`: Admin → all; Moderator → all **except other Moderators**. |

### 4h. Service-account / chat identity (`cases/api_views.py` ~1620)

| Action | Rule / notes |
|---|---|
| `GET /api/caseworker/me` (MCP) | Caller `sub` must be in `OIDC_SERVICE_ACCOUNT_SUBJECTS`; requires `X-Jawafdehi-User-Id` header; resolves the mapped Jawafdehi user and returns its roles. 403 otherwise. |

## 5. Special / conditional rules (the "not just a role" list)

These are the rows that a flat role×action grid cannot express. Preserve them
explicitly in any centralization:

1. **Object assignment (cases).** `can_change_case` for a Caseworker requires
   membership in `case.contributors`. Admin/Moderator bypass. (`is_case_contributor`)
2. **State-transition constraints.** Caseworker transitions confined to
   {DRAFT, IN_REVIEW}↔{DRAFT, IN_REVIEW}; Admin/Moderator unrestricted.
   (`can_transition_case_state`)
3. **Model-permission gate on delete.** Case delete needs `cases.delete_case`
   (a Django model permission Caseworker isn't granted) **in addition to**
   `can_change_case`.
4. **OAuth scope bypass (NGM SQL).** `HasNgmQueryAccess` accepts the
   `ngm.query` scope on the token *instead of* a role — for scope-only machine
   clients (MCP). **Disclosure is a second, separate decision:** whether
   `POST /query` applies `apply_public_projection` (hiding `is_deleted`,
   soft-deleted rows and internal columns) normally follows
   `has_ngm_query_role`, i.e. Group membership. A request may also set
   `public_projection: true` in the body to force the narrow plane regardless of
   its own entitlement. That flag is **one-way** — it can only turn the
   projection on, never off — so it is safe to honour from an untrusted body, and
   a falsy value grants nothing.

   The embedded MCP server sets it on every anonymous `ngm_query_judicial`
   request, because those authenticate as a shared service account rather than a
   person. Without it, the safety of an unauthenticated internet-facing SQL
   surface would depend on that account never being provisioned with `ReadOnly`
   or an NGM role — `_sync_user` rewrites Group membership from the token's role
   claims on every request, so such drift would silently publish the internal
   plane. Requests carrying a real caller's forwarded bearer are unaffected, and
   local stdio callers keep the internal plane.
5. **Service-account subject allowlist.** `/caseworker/me` gates on `sub ∈
   OIDC_SERVICE_ACCOUNT_SUBJECTS`, orthogonal to Group roles.
6. **Superuser sync + admin `is_staff`.** `admin` role ⇒ `is_superuser`;
   `{admin, moderator}` ⇒ Django-admin `is_staff`. Both re-derived per request.
7. **Material draft-leak guard.** PRIVATE material visibility = MAX over
   referring case states; only staff-ish/NGM/ReadOnly principals see drafts.
8. **User-management asymmetry.** Moderators cannot manage other Moderators
   (only Admin can). (`can_manage_user`)

## 6. Enforcement surfaces

The five mechanisms this doc consolidates (all reduce to Group membership +
`is_superuser`, except where §5 notes an extra axis):

1. **Group → Django `Permission` grants** — `cases/management/commands/create_groups.py` (also seeded by migration `cases 0039`).
2. **Wagtail page/collection perms** — `content/permissions.py` (`post_migrate`).
3. **django-rules predicates** — `cases/rules/predicates.py` (`can_view_case`, `can_change_case`, `can_transition_case_state`, `can_manage_user`, role predicates).
4. **DRF permission classes** — `review/permissions.py`, `entities/permissions.py`, `courts/permissions.py`, `jobs/permissions.py`, `jawafdehi_shared/drf/base.py`, plus per-view `get_permissions()` in `cases/api_views.py`, `entities/views.py`, `courts/views.py` and manual checks in `materials/views.py`.
5. **Role → Group / superuser / is_staff sync** — `jawafdehi_shared/auth/oidc.py` (DRF bearer) and `config/oidc_admin.py` (Django-admin session).

## 8. Target model (v3 — decided 2026-07-11)

A simplification pass. The net effect is **3 Django groups + `is_superuser`**
replacing today's seven-plus.

### 8.1 Role changes (all decided)

| Change | Consequence |
|---|---|
| Drop the `Admin` **group**; use `is_superuser` | `admin` Zitadel role already sets `is_superuser`, which short-circuits every check. `is_admin` / `is_admin_or_moderator` / `has_role` keep working (they already OR in `is_superuser`). The group's model-perm grants were redundant. |
| Drop `Public` group → treat as unauthenticated | **`is_public` is consumed by zero enforcement code today** — a Public user already behaves identically to anonymous. Delete the group, the `public` role key, and the `is_public` predicate. |
| Drop NGM `Silver/Gold/Platinum` tiers | NGM plane access becomes `Moderator` (+ superuser). *Confirm no throttle scope keys on the tier name before deleting.* |
| Rename `ReviewAssistant` → `JobPoller` (role key `job_poller`) | Its only holder is the poller service account (review read+write, jobs consume). Rename + data migration. |
| **Collapse `Caseworker` + `Moderator` into ONE content-staff role, named `Caseworker`** | The surviving role has **Moderator's full powers** under the name `Caseworker`. See 8.2. |

### 8.2 The content-role collapse (decided)

There is now **one content-staff role. Its Django group name is `Caseworker`,
and it carries the full powers the old `Moderator` had** (publish, edit/delete
any case, review-config PUT, user management, CMS, `is_staff`). The old
two-tier Caseworker/Moderator split is gone.

Zitadel side: **both** `moderator` and `contributor` role keys map to the
`Caseworker` Django group (Zitadel declares `contributor`, not `caseworker` —
see §9a-D). The legacy `caseworker` key, if any token still carries it, also
maps to `Caseworker` during transition.

This **retires object-level assignment-gating**:

1. **Publish** — the role transitions to any state (the old Caseworker
   `{DRAFT, IN_REVIEW}` confinement in `can_transition_case_state` is dropped).
2. **Edit/delete any case** — `can_change_case` no longer needs
   `case.contributors` assignment. **`is_case_contributor`, `case.contributors`,
   and the `delete_case` model-perm split become dead code and are removed**
   (not left half-wired).
3. Keeps global review-config PUT and **Django-admin `is_staff` + Wagtail CMS
   access** (see 8.6).
4. **Does NOT manage users.** User management is superuser-only (decided —
   §8.7). `can_manage_user` collapses to `is_admin` (superuser); the old
   "moderator can manage users but not other moderators" asymmetry is removed
   entirely.

### 8.3 Resulting principals

| Django group | Zitadel role key(s) | Notes |
|---|---|---|
| *(none — `is_superuser`)* | `admin` | Full access via superuser short-circuit. |
| `Caseworker` | `moderator` + `contributor` (+ legacy `caseworker`) | The single content-staff role, with the old Moderator's powers; `is_staff` + CMS. |
| `ReadOnly` | `readonly` | Org-wide read incl. casework. |
| `JobPoller` | `job_poller` | Machine role: review read+write, jobs consume. |
| *(none — anonymous)* | — | Replaces `Public`. |

### 8.6 Django-admin & CMS access (decided — corner case A1/C8)

**Accepted:** the merged `Caseworker` role KEEPS `is_staff` and Wagtail CMS
access. Moderators-on-Zitadel must be able to reach the CMS, so this is
intended, not an accidental expansion. Concretely:

- `STAFF_ROLES` (`config/oidc_admin.py`) must grant `is_staff` to every Zitadel
  key that maps to `Caseworker` → `{"admin", "moderator", "contributor"}` (add
  `contributor`; keep `moderator`).
- `content/permissions.py` must route full CMS page/collection perms +
  `access_admin` to the `Caseworker` group (fold the old `Moderator` +
  `Caseworker` CMS entries into the single `Caseworker` key; drop `Admin`).

### 8.7 User management is superuser-only (decided)

Caseworkers CANNOT manage users — that is an admin (superuser) capability.

- `can_manage_user` (`cases/rules/predicates.py:147`) collapses to `is_admin`
  (superuser). The Moderator branch (Moderators manage all users except other
  Moderators) is removed; there is no longer any content-role user-management.
- `UserAdmin` in `cases/admin.py` (`has_change_permission`/`has_delete_permission`
  → `can_manage_user`, `get_queryset` at ~:750) now admits superusers only.
- Test impact: `test_moderators_cannot_manage_other_moderators` and the
  moderator-can-manage-users assertions become obsolete → assert
  superuser-only + non-superuser-denied instead.

### 8.4 Self-introspection: `/authz/me` (replaces `/caseworker/me`)

`/caseworker/me` is renamed **`/authz/me`** and repurposed as the caller's own
authorization view — **open to anyone** (any authenticated principal sees their
own roles + capabilities; an unauthenticated caller sees the anonymous
capability set). This is the small consumer-facing face of the consolidated
registry (§9): "what can *I* do."

**Chat-identity resolution stays intact and separate.** The impersonation path
— resolve an `X-Jawafdehi-User-Id` header into *another* user's identity/roles
— keeps its existing trust boundary: caller `sub ∈
OIDC_SERVICE_ACCOUNT_SUBJECTS`. It is NOT merged into `/authz/me` and is NOT
opened up; only the "who am I" self-view is made public.

### 8.5 Axes that stay orthogonal to roles

These are NOT roles and are not folded into the group model — they remain
separate conditions on specific actions (§5), unchanged by v3:

- **`ngm.query` OAuth scope** — bypasses the NGM-role gate for scope-only
  machine clients (MCP). Kept as-is.
- **Chat-identity-resolution subject allowlist** — the `OIDC_SERVICE_ACCOUNT_SUBJECTS`
  gate on the resolution endpoint (see 8.4). Kept intact.

## 9. Consolidated authz surface (design)

Goal: the five enforcement surfaces in §6 stop re-declaring role logic and
instead **read from one module**, of which this doc is the human projection.

```
jawafdehi_shared/authz/
  roles.py     # Role enum + role-sets (CONTENT, READ, NGM, POLLER) + Zitadel key map
  actions.py   # Action registry: every §4 row as data —
               #   Action(id="case.change", grant=<rule>, obj_rule=<object predicate>, ...)
  rules.py     # composable grant primitives: superuser, in_role(...), has_scope(...),
               #   is_object_contributor, allow_authenticated, allow_any
  check.py     # THE entry point: check(user, action, obj=None, request=None) -> bool
  adapters.py  # generators that project the registry onto each surface:
               #   drf_permission("case.change")   -> a BasePermission subclass
               #   rules_predicate("case.change")  -> a django-rules predicate
               #   group_grants()                  -> create_groups.py's Permission map
               #   oidc_role_to_group()            -> the auth sync mapping
```

Migration is **strangler + matrix-gated**: keep this doc's §4 matrix as a golden
characterization test, introduce `authz/`, migrate one surface at a time keeping
the matrix byte-identical, then delete the scattered `NGM_ROLE_GROUPS` /
`ENTITY_WRITE_GROUPS` / inline role lists once every surface imports from the
registry. `ENTITY_WRITE_GROUPS` (== `has_role`, a literal duplicate) is the
first to collapse.

Object-level checks (§4a `cond` rows) stay expressible: `check()` takes `obj`,
and django-rules remains the evaluator underneath — we centralize the
*declarations*, not the evaluator.

## 9a. v3 repercussions & corner-case catalog (code scan 2026-07-11)

Result of a four-surface code scan (backend predicates, test suite,
frontend/MCP, migrations/Zitadel/config). This is the pre-implementation
reference: every impact site and every corner scenario found. Organized by
severity, not by change number.

### A. Behavior changes that EXPAND privilege (need explicit sign-off)

1. **Merged content role keeps `/django-admin` + CMS access — ACCEPTED
   (§8.6).** `STAFF_ROLES` (`config/oidc_admin.py:24`) drives `is_staff`.
   Decision: moderators-on-Zitadel must reach the CMS, so the merged
   `Caseworker` role KEEPS `is_staff` and Wagtail access. Action, not open
   question: add `contributor` to `STAFF_ROLES` (so `{admin, moderator,
   contributor}`) and route CMS perms to the `Caseworker` group (§8.6, C8).
2. **Caseworkers gain publish/close/reopen + config PUT + user management.** The
   merge erases the `{DRAFT, IN_REVIEW}` transition confinement
   (`can_transition_case_state`, `predicates.py:135`), the `IsAdminOrModerator`
   config-PUT gate, and `can_manage_user`. Intended, but it's the crux of the
   merge.
3. **Frontend silently surfaces privileged controls.** The SPA gates
   Publish/Close/Un-publish/Reopen (`CaseStateControl.tsx`), the Moderation
   queue (`Moderation.tsx`), and regrade-all (`CaseworkReviews.tsx`) on
   `isModerator = [admin, moderator]`. A merged caseworker principal lights all
   of these up **with zero FE code change** — the escalation is invisible in the
   backend diff. Must be verified in the SPA, not just the API.
4. **Dropping NGM tiers is an AUTHZ change, not a rate-limit change.** There is
   NO throttle keyed on tier names (throttle scopes are only `anon`/`user`,
   `settings.py:737-738`). The tiers are pure authorization gates
   (`courts/permissions.py:NGM_ROLE_GROUPS`). A tier-ONLY principal currently
   has NGM write/query access and LOSES it. **Corner case:** the `ngm-svc`
   service account is granted `ngm_platinum` in Zitadel (`provision.tf:135`) —
   confirm it also holds `contributor`/content role, or it loses NGM write on
   the drop. It does (grant list is `[contributor, ngm_platinum]`), so it
   survives via the content role — but verify before applying.

### B. Behavior changes that REDUCE capability (confirm no persona relies on them)

5. **Authenticated-no-role users lose "my assigned cases."** `get_queryset`
   (`cases/api_views.py:403-410`) returns `PUBLISHED OR contributors=me` for
   authenticated users without a content role. Retiring assignment collapses
   this to PUBLISHED-only. Who is "authenticated but role-less"? A logged-in
   user whose Zitadel token carries no mapped role. Confirm this persona is
   empty/irrelevant.
6. **Case author loses the history-view fallback.** `/history` access is
   `can_view_case(...) OR is_case_contributor(...)` (`api_views.py:642-644`).
   The contributor fallback (author feedback loop) disappears.
7. **Creator auto-assignment stops.** `case.contributors.add(request.user)` on
   create (`api_views.py:528`) and admin create (`admin.py:718`) become no-ops
   to remove. No user-visible field is lost (contributors is NOT serialized —
   `CaseSerializer` excludes it; `test_public_api.py::test_api_does_not_expose_contributors`
   becomes vacuous).

### C. Silent breakage / easy-to-miss consumers

8. **Wagtail/CMS permissions key on group NAMES directly**
   (`content/permissions.py:39-61`: `GROUP_PAGE_PERMS`, `GROUP_COLLECTION_PERMS`,
   `_ACCESS_ADMIN_GROUPS` all list `"Admin"`/`"Moderator"`/`"Caseworker"`). This
   is a `post_migrate` hook outside the predicate system. **Decision (§8.6):**
   route the FULL page/collection perms + `access_admin` to the single
   `Caseworker` group (fold the old `Moderator` full-perms and `Caseworker`
   editor-only entries into one full-perms `Caseworker` entry; drop the `Admin`
   entry — superuser doesn't need CMS group perms). The old
   editor-only-cannot-publish tier is gone.
9. **The `me` / dev-login payload is a FE contract that changes.**
   `review/views.py:44-53` (`_user_roles_payload`) special-cases superuser to
   inject `"Admin"` into `roles` and computes `is_admin`. Dropping the Admin
   group means superuser no longer yields `"Admin"` in `roles`. The SPA's
   `roles.ts` (`ADMIN_ROLES`, `MODERATOR_ROLES`, `isAdmin`) and
   `CaseworkAuthContext.tsx` consume this shape. Coordinate: either keep
   injecting a synthetic `"admin"` role for superusers, or update `roles.ts` +
   `isAdmin` to key on the payload's `is_admin` flag.
10. **MCP catalog visibility is authentication-based, not role-based.**
    Any verified bearer receives the full MCP tool catalog. Write tools forward
    that bearer to Django, so the API's role and object-level permissions remain
    authoritative; anonymous callers retain a restricted catalog.
11. **`ReviewAssistant`→`JobPoller` MUST be a Group-row RENAME migration, not a
    mapping change.** The OIDC sync only *attaches existing* groups
    (`oidc.py:277`), never creates them. A mapping-only change would silently
    stop attaching the poller's role. Use `Group.objects.filter(
    name="ReviewAssistant").update(name="JobPoller")` (preserves PK + M2M).

### D. The Zitadel key-vs-group mismatch (pre-existing, must reconcile)

12. **Zitadel declares `contributor`, Django expects `caseworker`, neither has
    `public`.** `infra/zitadel/provision.tf` `local.roles` declares `admin,
    moderator, contributor, readonly, review_assistant, ngm_silver/gold/platinum`
    — key `contributor`, NOT `caseworker`, and **no `public` role at all**.
    Django's `DEFAULT_ROLE_TO_GROUP` (`oidc.py:101`) keys on `caseworker` and
    `public`. So today a real Zitadel `contributor` token maps to NOTHING in
    Django (the content role is effectively unreachable via real Zitadel — only
    dev-login/seed uses the `Caseworker` group). **This must be reconciled in the
    merge:** map Zitadel's real key (`contributor`) → the surviving `Caseworker`
    group, and fold `moderator` → `Caseworker` too. Cleanest:
    `DEFAULT_ROLE_TO_GROUP = {"moderator": "Caseworker", "contributor":
    "Caseworker", "caseworker": "Caseworker", "readonly": "ReadOnly",
    "job_poller": "JobPoller"}` — note `admin` is NOT in the map (it drives
    `is_superuser` only, via `DEFAULT_SUPERUSER_ROLE`); `public`/`ngm_*` dropped.
13. **Dropping Public needs NO Zitadel change** (there's no `public` role to
    drop). Dropping NGM tiers DOES need `terraform apply` (remove the three
    roles + the `ngm-svc` `ngm_platinum` grant). `review_assistant` rename is
    optional IdP-side if you remap in Django.

### E. Transition-window safety (good news)

14. **Stale tokens are harmless.** `_sync_user` guards with `if r in
    role_to_group` (`oidc.py:271`) and `Group.objects.filter(name__in=...)`
    tolerates missing rows — an unmapped/renamed role key contributes no group,
    never errors. So a token carrying old `caseworker`/`public`/`ngm_gold` keys
    mid-rollout maps to nothing (or to the remapped group). **Safe rollout
    order:** (1) update Django map to remap surviving keys, (2) apply DB
    migrations (rename JobPoller, delete Admin/Public/tiers, drop
    Case.contributors), (3) update FE `roles.ts`, (4) optionally `terraform
    apply` Zitadel last. MCP catalog visibility does not depend on role names.

### F. Schema vs data migrations required

- **Schema migration:** drop `Case.contributors` M2M (`cases/models.py:610`) —
  drops the through-table. Zero API-response impact (not serialized).
- **Data migration:** rename `ReviewAssistant`→`JobPoller` (row update);
  delete `Admin`, `Public`, `NGM_*Tier` group rows (reuse `0039`'s reverse
  `remove_ngm_rate_tier_groups`). **Group collapse:** the surviving content
  group is named `Caseworker`, so move any `Moderator` members into `Caseworker`
  and delete the `Moderator` row (functionally optional — re-synced per request
  — but avoids stale seeded/session rows). Grant the `Caseworker` group the full
  former-Moderator model perms (incl. `delete_case`).
- **Command/seed updates:** `create_groups.py` (stop creating dropped groups;
  make the single `Caseworker` group carry the FULL former-Moderator perm set,
  not the old editor-only subset); `seed_dev.py` (the `moderator` seed user →
  `Caseworker`; keep an `is_superuser` admin user with no group).

### G. Test blast radius (defines the golden-characterization surface)

- **~28 files must be rewritten**, **~11 rename-only**, **1 file deleted**
  (`tests/test_public_role_permissions.py`) + ~15 individual obsolete tests.
- **Two chokepoints to fix first:** `tests/conftest.py::create_user_with_role`
  (`:191`, must keep the `is_superuser` branch for Admin, remap Caseworker) and
  `tests/strategies.py::user_with_role` (`:282`). Fixing these resolves most
  rename-only churn.
- **Hard (ORM-level) failures, not assertion failures:** ~9 files call
  `case.contributors.add(...)`; removing the M2M breaks them with AttributeError.
- **Boundary tests that must be INVERTED (merge erases them):**
  `test_review_config_gate::test_caseworker_cannot_put_config`,
  `test_case_state_transitions::test_caseworker_cannot_publish` /
  `::test_caseworker_cannot_close`,
  `test_cms_permission_sync::test_caseworker_is_editor_only_cannot_publish`,
  `test_admin_case_management::test_contributor_cannot_publish_case`,
  `test_role_based_permissions` transition-confinement tests,
  `test_admin_e2e` contributor-state-restriction tests,
  `test_delete_endpoints::test_delete_case_unassigned_caseworker_is_403`.
- **Golden anchors to PRESERVE unchanged** (these encode invariants the refactor
  must NOT alter): ReadOnly-reads-but-cannot-write; unauthenticated-sees-only-
  PUBLISHED; superuser-full-access; authenticated-without-role → 403;
  ReadOnly-cannot-write-entities; NGM-authed-without-role → 403.

### H. Non-findings (verified NOT affected — don't touch)

- `lakehouse/config.py:52` `ngm_silver` namespace — a lakehouse table
  namespace, NOT an auth group. False positive.
- Throttle rates — no role/tier scope keys exist.
- MCP `/caseworker/me` — MCP never calls it (resolves roles from OIDC
  userinfo). SPA never calls it either. So the `/authz/me` rename is
  backend-only + no client breakage; only the endpoint path + its open-access
  change matter.
- `ngm.query` OAuth scope and the chat-identity subject-allowlist — orthogonal
  to this refactor, unchanged.

## 10. Maintenance

Any change to an authorization rule MUST update the relevant row here in the
same PR. When enforcement is centralized (single `authz` module referenced by
all surfaces), this doc becomes the human-readable projection of that module and
the §3 sets/§4 matrix should be derivable from it.

> Sections 1–7 describe the model **as enforced today**; sections 8–9 describe
> the **decided v3 target** (2026-07-11). Until the v3 migration lands, §1–7 are
> authoritative for current behavior.
