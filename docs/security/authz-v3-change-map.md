# Authz v3 — implementation change map

Companion to `authz-model.md`. Enumerates every code change, keyed to the
decided v3 model. Verified against `origin/main` (API `c743b61`, FE `33026e3`)
on 2026-07-11. Backend worktree: `worktrees/wt-authz-v3-api` (branch `authz-v3`);
frontend: `worktrees/wt-authz-v3-fe` (branch `authz-v3`).

## Final model recap

- **superuser** ← Zitadel `admin` (no group). Only principal that manages users.
- **`Caseworker`** ← Zitadel `moderator` + `contributor` (+ legacy `caseworker`).
  Single content-staff role with the *old Moderator's* full powers: view/create/
  edit/delete/publish any case, entities, courts/materials + NGM query/ingest,
  reviews + review-config PUT, `is_staff` + Wagtail CMS. **Cannot manage users.**
- **`ReadOnly`** ← `readonly`. Reads incl. casework, no writes.
- **`JobPoller`** ← `job_poller` (renamed from `ReviewAssistant`). Review r/w +
  jobs consume.
- **anonymous** replaces `Public`.
- Orthogonal (unchanged): `ngm.query` scope; chat-identity subject allowlist.

Groups after v3: **`Caseworker`, `ReadOnly`, `JobPoller`** + `is_superuser`.
Dropped: `Admin`, `Moderator`, `Public`, `NGM_{Silver,Gold,Platinum}Tier`.
`Caseworker` group survives but is *repowered* to full former-Moderator perms.

---

## ⚠️ Signed-off privilege expansion (review backend #2)

Redefining `is_admin_or_moderator`/`IsAdminOrModerator`/`ENTITY_ADMIN_GROUPS` in
place means **every former plain contributor is promoted to full moderator power**
at three sites that were previously moderator-only: **review-config PUT**
(`review/views.py:334`), **entity reindex** (`entities/permissions.py:35`), and
**publish/close/reopen** (`can_transition_case_state`). This is the intended crux
of the collapse (§8.2) but is invisible in per-site diffs — it is explicitly
accepted, not a bug. Listed here so it is a conscious decision, not a surprise.

## BACKEND changes

### B1. `cases/rules/predicates.py` — the core
- `is_admin` (:25): drop `groups.filter(name="Admin")` → `return user.is_superuser`.
- `is_moderator` (:31): **KEEP for now** (see below) — do NOT delete before its
  one consumer (`cases/admin.py:754`, B15) is rewritten, or that call NameErrors.
- `is_caseworker` (:44): keep — now the single content role. Group name unchanged.
- `is_admin_or_moderator` (:48): rename semantics → the role check is now just
  "superuser or Caseworker". Keep the symbol as an alias to avoid churn, OR
  replace all consumers with `has_role`. **Decision: keep symbol, redefine as
  `is_superuser | is_caseworker`** (least churn), update docstring.
- `has_role` (:59): `groups.filter(name__in=["Caseworker"])` (+ superuser via
  the OR sites). Drop Admin/Moderator names.
- `is_moderator` (:31): **KEEP** — `cases/admin.py:754` calls it in
  `CustomUserAdmin.get_queryset` (see B15). Deleting it NameErrors at runtime.
  It stays valid (checks the old Moderator group) but is effectively dead once
  Moderator is gone; simplest is to keep the symbol and rewrite its one consumer
  (B15) so the predicate can then be removed safely. **Decision: rewrite the B15
  consumer FIRST, then delete `is_moderator` + its `__init__` re-export.**
- `has_role` (:59): `groups.filter(name__in=["Caseworker"])` (+ superuser via
  the OR sites). Drop Admin/Moderator names.
- `is_public` (:76): **delete** (zero consumers — reviewer re-verified). Remove
  from `__init__.py`.
- `is_case_contributor` (:94): **delete** (assignment retired).
- `can_transition_case_state` (:107): remove the `is_caseworker` DRAFT/IN_REVIEW
  confinement branch (:135) — the single role transitions to any state. Reduces
  to: `is_superuser or is_caseworker → any; else False`.
- `can_manage_user` (:148): collapse to `is_admin` (superuser-only). Remove the
  Moderator branch entirely.
- `can_view_case` (:180): `is_admin_or_moderator | is_caseworker | is_readonly`.
  **CORRECTION (review blocker #1):** do NOT drop the `is_admin_or_moderator`
  term. Predicates are plain function calls with NO django-rules superuser
  auto-allow, and `is_caseworker`/`is_readonly` carry NO `is_superuser` term. A
  superuser has NO group, so `is_caseworker | is_readonly` alone would 404 a
  superuser on DRAFT/IN_REVIEW cases (`api_views.py:600` retrieve, `:642`
  history, `admin.py:673`). **Keep as `is_admin_or_moderator | is_caseworker |
  is_readonly`** — the `is_admin_or_moderator` term is the ONLY superuser
  carrier here. (`is_caseworker` is now redundant with the redefined
  `is_admin_or_moderator` but harmless; keep for clarity.)
- `can_change_case` (:181): drop `is_case_contributor` → `is_admin_or_moderator`
  (i.e. superuser|caseworker). Safe: retains the `is_superuser` term.
- `can_manage_user_account` (:187): has ZERO production consumers and collapses
  to `is_admin | is_admin`. **Delete it** rather than leave a tautology.

### B2. `cases/rules/__init__.py`
- Remove re-exports of `is_public` (:23) and `is_case_contributor` (:14).
- Remove `is_moderator` re-export (:20) — only AFTER B15 rewrites its consumer.

### B3. `jawafdehi_shared/auth/oidc.py`
- `DEFAULT_ROLE_TO_GROUP` (:101) → `{"moderator": "Caseworker", "contributor":
  "Caseworker", "caseworker": "Caseworker", "readonly": "ReadOnly",
  "job_poller": "JobPoller"}`. Remove `admin` (superuser-only via
  `DEFAULT_SUPERUSER_ROLE`), `public`, `review_assistant`, `ngm_*`.
- `DEFAULT_SUPERUSER_ROLE = "admin"` (:56): keep.

### B4. `config/oidc_admin.py`
- `STAFF_ROLES` (:24) → `{DEFAULT_SUPERUSER_ROLE, "moderator", "contributor"}`
  (+ legacy `caseworker`). Every key mapping to `Caseworker` must get `is_staff`
  so CMS/admin is reachable (§8.6).

### B5. `cases/management/commands/create_groups.py`
- Delete the `Admin` block (:88-106) and `Moderator` block (:109-127).
- Delete the `Public` block (:187-204) and the NGM tier loop (:206-217).
- Replace the `ReviewAssistant` block (:151-164) with a `get_or_create(
  name="JobPoller")` block (empty perms). **NOTE (review migration #1):** this is
  a plain `get_or_create`, NOT a rename — the migration 0050 does the row rename
  that carries members. `create_groups` must therefore run AFTER `migrate`
  (see B16 ordering) or the fresh empty `JobPoller` it creates collides with
  0050's `update(name="JobPoller")` on the UNIQUE `auth_group.name`.
- **Repower `Caseworker`** (:129-149): grant the FULL former-Moderator perm set
  incl. `delete_case` (currently Caseworker lacks delete). It must equal what
  Moderator had (:116-127).

### B6. `content/permissions.py` (Wagtail/CMS — §8.6)
- `GROUP_PAGE_PERMS` (:38): single `"Caseworker": _ALL_PAGE_PERMS` (full, incl.
  publish). Drop `Admin`/`Moderator` keys; former `Caseworker` was editor-only
  → now full.
- `GROUP_COLLECTION_PERMS` (:54): single `"Caseworker": _FULL_COLLECTION`.
- `_ACCESS_ADMIN_GROUPS` (:61): `("Caseworker",)`.

### B7. `courts/permissions.py`
- `NGM_ROLE_GROUPS` (:23): `{"Caseworker"}` (drop Admin, Moderator, 3 tiers).
  Superuser short-circuits at :72. `ngm.query` scope path unchanged.

### B8. `entities/permissions.py`
- `ENTITY_WRITE_GROUPS` (:32): `{"Caseworker"}`.
- `ENTITY_ADMIN_GROUPS` (:35): `{"Caseworker"}` (was {Moderator, Admin} — reindex
  now allowed for the single content role; superuser too).

### B9. `review/permissions.py`
- `HasContributorRole` (:48): `"ReviewAssistant"` → `"JobPoller"`.
- `CanReadReview` (:77): `["ReviewAssistant", "ReadOnly"]` → `["JobPoller",
  "ReadOnly"]`.
- `IsAdminOrModerator` (:82): now "superuser or Caseworker" (config PUT is a
  content-staff action). Rename to `IsContentStaff` OR keep name + redefine.
  **Decision: keep name, redefine** to `is_superuser | is_caseworker`.

### B10. `jobs/permissions.py`
- `CanConsumeJobs` (:35): `"ReviewAssistant"` → `"JobPoller"`.
- `CanObserveJobs` (:55): `["ReviewAssistant", "ReadOnly"]` → `["JobPoller",
  "ReadOnly"]`.

### B11. `review/views.py` (`_user_roles_payload` :44-53) — FE contract
- Superuser injects `"Admin"` into `roles` (:47-48) and computes `is_admin`
  (:52). Keep injecting a synthetic marker for the SPA, but align names: inject
  `"admin"` (lowercase, matches FE `isAdmin`) OR keep `is_admin` bool as the
  authoritative signal. **Decision: keep `is_admin` bool; stop injecting a fake
  group into `roles`; emit real group names only.** Coordinate with FE F2.

### B12. `cases/api_views.py`
- `get_permissions` (:330): `create`/`destroy` keep `DjangoModelPermissions`
  (add_case/delete_case) — ensure Caseworker group now HAS delete_case (B5).
- `get_queryset` (:397-410): remove the `is_caseworker` branch redundancy and
  the `Q(contributors=self.request.user)` assignment branch (:407). Non-role
  authed users → PUBLISHED only.
- `perform_create`/create (:528): remove `case.contributors.add(request.user)`.
- history endpoint (:642-644): drop `is_case_contributor` fallback → `can_view_case`.
- delete (:1053): `can_change_case` (now superuser|caseworker) — unchanged call,
  new meaning.
- Remove `is_case_contributor`/`is_caseworker` imports as needed (:64).

### B13. `cases/serializers.py`
- casework-access memo (:56): drop `is_caseworker`/keep — since it's the content
  role, keep `is_caseworker | is_readonly` (+ superuser). Remove
  `is_admin_or_moderator` import if redefined. `/contributors` in
  BLOCKED_PATH_PREFIXES (:55): remove (field gone).

### B14. `cases/caseworker_serializers.py`
- `/contributors` in blocked-path list (:55): remove.

### B15. `cases/admin.py`
- `is_caseworker` state-field-disable branch (:171) + queryset branch (:643):
  the single role sees all; simplify.
- `contributors` in `list_display` (:451), `Assignment` fieldset (:519),
  `filter_horizontal` (:533), `contributors_list()` (:554), `save_related`
  auto-add (:707): **remove all** (field gone).
- `UserAdmin.has_change/has_delete_permission` (:774,:786) → `can_manage_user`
  (now superuser-only): unchanged calls, new meaning.
- **`CustomUserAdmin.get_queryset` (:754) — REVIEW BLOCKER #3.** It calls
  `is_moderator(request.user)` and builds a Moderator-exclusion list. This is the
  one live consumer of `is_moderator`. Rewrite it BEFORE deleting the predicate:
  `if is_admin(request.user): return qs` (is_admin now = superuser) `; return
  qs.none()`. Leaving the body while removing the import → `NameError` on the
  User changelist for any non-superuser staff.
- `is_admin`/`is_moderator` imports (:26-28): update to what survives (drop
  `is_moderator` only after the get_queryset rewrite above).
- `static/admin/js/case_admin.js:23` + the `contributor-state-field` class
  (:176): remove (cosmetic, tied to the removed confinement).

### B16. `cases/models.py` + migration
- `Case.contributors` M2M (:610): **remove field** → SCHEMA migration.
- New migration `0050_authz_v3`. **Corrected per migration review (#2,#3,#6,#8):**
  - `dependencies = [("cases", "0049_casestatechange"),
    migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ("auth", "0012_alter_user_first_name_max_length")]` — declare the auth dep so
    Group/User tables are guaranteed present and ordering is deterministic (0039
    does this).
  - Operations order matters. **RunPython data step runs BEFORE `RemoveField`**
    conceptually independent, but the data step must NOT touch
    `Case.contributors` (it doesn't — it only touches `auth.Group`/memberships),
    so either order is safe; keep the group RunPython first, then RemoveField.
  - Data step, in THIS order:
    1. `Group.objects.get_or_create(name="Caseworker")` — **0050 must self-create
       it**; no migration creates Caseworker today (only create_groups), and a
       fresh prod apply needs it to exist before granting perms / moving members.
    2. Grant `Caseworker` the `delete_case` (+ full former-Moderator) perms via
       the historical `Permission` model (look up by `codename`/content type).
    3. **Move members BEFORE deleting Moderator:** for each user in `Moderator`,
       add to `Caseworker`; THEN delete the `Moderator` group row. (Deleting first
       cascades `auth_user_groups` and loses them — review #3/#4.) This move is
       **MANDATORY, not optional**: session/admin-site and seed_dev users are NOT
       re-synced per request, so a Moderator-only user who never re-OIDC-auths
       would lose all access otherwise (review #4).
    4. Rename `ReviewAssistant`→`JobPoller`:
       `Group.objects.filter(name="ReviewAssistant").update(name="JobPoller")`
       (preserves PK + memberships).
    5. Delete `Admin`, `Public`, and the three `NGM_*Tier` rows. **Inline the
       delete** — do NOT import 0039's `remove_ngm_rate_tier_groups`: its module
       name starts with a digit (unimportable) and 0039 is immutable (depended on
       by 0040). Just `Group.objects.filter(name__in=[...]).delete()`.
  - **Reverse: `migrations.RunPython.noop` for the group step (irreversible).**
    The collapse is lossy — can't reconstruct native-Caseworker vs
    ex-Moderator, and deleted memberships are gone. The `RemoveField` reverse
    re-adds an EMPTY M2M (assignments permanently lost). Document this; do NOT
    claim clean reversibility.
- **Deploy atomicity (review #5):** B5 (`create_groups`) + B6
  (`content/permissions.py`) code MUST ship in the same release as 0050.
  `content.apps` runs `sync_cms_group_permissions` on `post_migrate` (end of
  every `migrate`, using the DEPLOYED code). If old code is live when `migrate`
  runs, the post_migrate hook re-creates empty `Admin`/`Moderator` groups (with
  Wagtail perms) and a later `create_groups` re-creates Public/tiers — undoing
  0050. With new code deployed, it converges to exactly
  `{Caseworker, ReadOnly, JobPoller}`.

### B17. `cases/management/commands/seed_dev.py`
- `USERS` (:49-53): `moderator` seed user → `["Caseworker"]`; drop/rename
  `caseworker` entry (or map to `Caseworker`); keep `admin` as is_superuser
  no-group. Set `is_staff` appropriately.
- **Dev-login superuser (review FE #1):** the `admin` seed user is
  `("admin", [], True)` — superuser with NO group. Combined with B11 (stop
  injecting synthetic `"Admin"` into the payload), dev-login returns
  `roles=[], is_admin=true`. The FE MUST honor the `is_admin` bool (F2), else the
  group-less dev superuser is locked out of the whole admin panel. This is why
  F2 is a blocker, not a polish item.

### B18. MCP — `jawafdehi-mcp/identity.py:66`
- `_DEFAULT_WRITE_ROLES = ("contributor", "admin", "moderator")` — keep
  `contributor` (Zitadel key survives) + `moderator`; these still map to the
  content role. No change strictly required since it matches token keys, but add
  a comment. Tests (`test_identity.py:35`) assert the set — leave unless keys
  change. **Out of scope for this PR unless we retire the `contributor` key.**

---

## FRONTEND changes (`worktrees/wt-authz-v3-fe`)

### F1. `src/lib/roles.ts` — the semantic inversion (highest risk)
- Today: `MODERATOR_ROLES = [admin, moderator]` gates privileged actions and
  EXCLUDES `caseworker`/`contributor`. After v3, the content role (Zitadel
  `moderator` + `contributor` + `caseworker`) IS privileged. **`contributor` is a
  LIVE Zitadel key (review FE #2)** — the FE reads RAW token keys, so a real
  content-staff user in prod carries `contributor` or `moderator`. EVERY
  content-staff list MUST include `contributor` (and `caseworker` for dev-login
  group-name folding), or that user sees no privileged UI on that surface.
  - Privileged gate (`isModerator`) → `[admin, moderator, contributor,
    caseworker]`. Optionally rename `isContentStaff` (keep alias to reduce churn).
  - `ADMIN_ROLES` (panel entry) → `[admin, moderator, contributor, caseworker,
    readonly]`.
  - `NGM_WRITE_ROLES` → drop the 3 tier variants; set `[admin, moderator,
    contributor, caseworker]`.
  - `NES_WRITE_ROLES` → **explicitly** `[admin, moderator, contributor,
    caseworker]` (review FE #3). The old `nes_*` keys are retired (B3 drops them);
    do NOT leave it vague — if it omits the content keys, `hasNesWriteAccess`
    gates OUT all content staff and the `/admin/entities` nav (`AdminLayout:72`)
    + create/edit forms (`AdminApp.tsx:66,74`) vanish despite backend allowing.
  - `isAdmin` → only `admin` (unchanged) — but see F2: it must ALSO honor the
    `is_admin` payload bool for the group-less superuser.
- **Delete the stale comments (roles.ts:1-24, `roles.test.ts:18-25`) that claim
  "contributor was renamed to caseworker; no backend emits it."** That belief is
  now false and is what justifies the wrong `contributor=false` behavior; leaving
  it invites a future reader to "restore" the broken lists (review FE #4,#7).
- `src/lib/roles.test.ts`: INVERT the `contributor=false` assertions →
  `hasAdminAccess(["contributor"])`, `isModerator(["caseworker"])`,
  `isModerator(["contributor"])`, `hasNesWriteAccess(["caseworker"|"moderator"|
  "contributor"])`, `hasNgmWriteAccess(["contributor"])` all now === **true**.

### F2. Admin-ness must honor the `is_admin` bool — BLOCKER (review FE #1)
The SPA derives ALL privilege signals from the `roles` ARRAY, never the
`is_admin` field: `CaseworkAuthContext.tsx:100-101` (`isAdmin`/`isModerator` via
`roles.ts`) and `AdminLayout.tsx:183` panel gate (`hasAdminAccess(roles)`). The
`CaseworkUser.is_admin` field (set at `CaseworkAuthContext.tsx:50`) is unused.
Once B11 stops injecting synthetic `"Admin"` and the seed superuser has no group,
dev-login returns `roles=[], is_admin=true` → `hasAdminAccess([])`=false → the
superuser is bounced to the no-access screen. **Fix: fold the `is_admin` bool
into the panel gate AND the admin/moderator helpers** — e.g. pass `is_admin`
through the context and OR it into `hasAdminAccess`/`isAdmin`/`isModerator`, not
just store it on the field. Prod OIDC is unaffected (token carries `admin`
directly); this bites dev/local/Playwright specifically.

### F3. User-management UI — NO-OP (review FE #6)
There is **no user-management UI in the SPA** — routes/pages/services confirmed
absent (`AdminApp.tsx:56-167`, `src/pages/admin/**`). User management is
Django-admin/server-side only (handled by B1's `can_manage_user`→superuser). So
there is nothing to gate here and the feared "caseworker manages users" over-open
cannot happen client-side. Stated so the implementer doesn't chase a phantom.

### F4. `src/components/admin/AdminLayout.tsx:100`
- Inline `roles: ["admin","moderator"]` for the Moderation nav link (gated via
  the `it.roles` mechanism at :114, NOT via `isModerator`, so F1 does not fix it
  automatically). Add BOTH `contributor` AND `caseworker` (review FE #5) — the
  naive "rename moderator→caseworker" would omit `contributor` and hide
  Moderation from contributor-token users. Cleaner: replace the inline list with
  the `isModerator`/`isContentStaff` helper. This is the ONLY inline role literal
  in the FE (grep-confirmed).

### F5. Verify no `Public` role assumption
- Confirmed none in census; re-verify after changes.

### Pinned FE↔backend contract facts (for the implementer)
- Prod OIDC roles = `auth.user.profile.roles` (raw lowercase Zitadel keys:
  `admin, moderator, contributor, readonly`) — `CaseworkAuthContext.tsx:40-50`.
  SPA does NOT call `/caseworker/me`.
- Dev-login roles = backend group-name payload (`_user_roles_payload`,
  `review/views.py:44-52`): `["Caseworker"]`, `["ReadOnly"]`, and (today)
  `["Admin"]` for superuser. `roles.ts:68-74` case-folds, so lists must carry OIDC
  keys (`moderator`,`contributor`) AND rely on folding for group names
  (`Caseworker`→`caseworker`, `ReadOnly`→`readonly`).

---

## OUT OF SCOPE (this PR)
- Zitadel `terraform apply` to drop `ngm_*` roles + `ngm-svc` grant: separate
  IdP-coordination change. Runtime-safe to defer (stale keys ignored).
- `/caseworker/me` → `/authz/me` rename + open-access: no client calls it;
  can be a follow-up. NOT required for the role collapse.
- Retiring the Zitadel `contributor` key (keep it; it maps to Caseworker).
- The full `jawafdehi_shared/authz/` registry consolidation (§9) — this PR does
  the model change; centralization is the next phase.

## TEST WORK (see authz-model §9-G)
- Fix chokepoints first: `tests/conftest.py::create_user_with_role`,
  `tests/strategies.py::user_with_role`.
- Delete `tests/test_public_role_permissions.py`; invert the ~8 caseworker-
  blocked-boundary tests; rewrite ~9 `Case.contributors.add` files; rename-only
  ~11 files. Preserve golden anchors (ReadOnly r/no-w, anon→PUBLISHED-only,
  superuser-full, authed-no-role→403).
