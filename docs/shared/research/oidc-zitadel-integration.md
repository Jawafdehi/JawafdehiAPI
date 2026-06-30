# OIDC / Zitadel Integration

> **STATUS (2026-06-28): STALE topology, sound findings — see ../../DOC-STATUS.md.**
> The core PyJWT + `PyJWKClient` + JWKS + Zitadel-roles→Django-groups path is exactly
> what shipped and is still authoritative. But the framing — "three services
> (Django + two FastAPI)", inter-service M2M emphasis, and the `wt-*-v2`/`nes-api`/
> `wt-ngm-v2`/`backend` paths — is wrong: NES and NGM are now **Django apps**
> (`nes_service`, `ngm_service`) in **one monolith** (`jawafdehi-platform`), so there
> is **one** OIDC-protected Django service and **in-process** (not M2M-over-HTTP)
> cross-app calls. Read the auth mechanics as current; read the per-service/path
> framing as the pre-monolith era. Live state: `../../ARCHITECTURE.md` §4.

**Status:** Research / decision draft (point-in-time, 2026-06-27)
**Date:** 2026-06-27
**Scope:** Standardize on **Zitadel (self-hosted, OIDC)** as the central IdP for three services and **drop DRF token auth entirely** (OIDC-only, including service-to-service via client-credentials / JWT-profile).

> Services in scope (verified against the local repos):
> - **Jawafdehi backend** — Django/DRF, at `/damodaha-volunteer/backend`. Today uses simplejwt (HS256), a custom `ChatServiceAccountAuthentication` (subclass of DRF `TokenAuthentication`), and `SessionAuthentication`. **This is where all of the auth/token/role/NGM-tier code lives.**
> - **NGM API** — FastAPI, at `/damodaha-volunteer/wt-ngm-v2/ngm/api`. Already **OIDC-stubbed** (`HTTPBearer`, `Principal`, `require_scope`) but `_verify_token` is unimplemented (fails closed / 501 in prod).
> - **NES API** — FastAPI, at `/damodaha-volunteer/nes-api/nes/api`. **Completely unauthenticated today** (permissive CORS `*`); Zitadel is greenfield here.

---

## 0. TL;DR recommendations

| Concern | Recommendation |
|---|---|
| Django/DRF token validation | **Custom DRF `BaseAuthentication` using PyJWT + `PyJWKClient`** (local JWKS validation). *Not* mozilla-django-oidc, *not* django-oauth-toolkit — neither does local JWKS validation of third-party access tokens. |
| FastAPI token validation | **PyJWT + `PyJWKClient`** behind `fastapi.security.HTTPBearer`. Avoid `python-jose` (CVE history + unfixed transitive `ecdsa` CVE). Authlib only if you need a full OAuth framework. |
| Token format | Set each Zitadel app's **Token Type = JWT** (default is opaque Bearer). Required for local JWKS validation. |
| Service-to-service | **JWT Profile (Private Key JWT)** — Zitadel's explicitly recommended M2M method. Client-credentials (`client_id`+`client_secret`) is the simpler fallback. Both must request the **audience scope**. |
| Roles as source of truth | Define **project roles** in Zitadel; grant them to users and service accounts via **Role Assignments**. Read them from the access-token claim `urn:zitadel:iam:org:project:roles`. |
| Migration of Silver/Gold/Platinum | They are **rate-limit groups only** (no scope semantics today). Map to **Zitadel project roles** `ngm_silver/ngm_gold/ngm_platinum`; keep the throttle, keying it off the role claim. |
| Migration of `chat-jawafdehi-org` | Replace with a **Zitadel service account** (machine user, JWT-Profile key) granted the `contributor` role; keep header-based end-user impersonation as a separate concern. |

**Shared library choice across all three services: PyJWT (`pyjwt[crypto]`) + `PyJWKClient`.** One mental model, one dependency, local validation everywhere.

---

## 1. Django/DRF + OIDC (resource-server mode)

### 1.1 Library decision

DRF here is a pure **resource server**: it must validate a Zitadel-issued **JWT access token** on each request. It does **not** run a browser login/SSO flow. That distinction kills two of the three candidates:

| | **PyJWT + PyJWKClient (custom DRF auth)** | **mozilla-django-oidc** | **django-oauth-toolkit (DOT)** |
|---|---|---|---|
| Primary design purpose | Low-level JWT/JWKS validation | Browser SSO / auth-code login (Relying Party) | OAuth2/OIDC authorization **server** (issues its own tokens) |
| LOCAL JWKS validation of a 3rd-party access JWT? | **Yes** (sig + `iss` + `aud` + `exp`) | **No** — its DRF `OIDCAuthentication.authenticate()` calls `get_or_create_user()`, which does a **remote `GET` to the userinfo endpoint** per request. JWKS is used only for the *ID token* in the login flow. | **No** — validates only tokens it issued (DB lookup) or via a **remote RFC 7662 introspection** endpoint (`RESOURCE_SERVER_INTROSPECTION_URL`). No JWKS path for access tokens. |
| Per-request network call to IdP? | No (JWKS cached ~5 min, auto-refresh on key rotation) | Yes (userinfo) | Yes (introspection) unless its own DB token |
| Fit for Zitadel resource server | **Best** | Poor | Poor |
| Maturity | PyJWT v2.x, MIT, actively maintained | MPL-2.0, Mozilla, active (login-oriented) | Jazzband, active but "seeking maintainers" |

**Decision: a small custom `BaseAuthentication` class on PyJWT + `PyJWKClient`** (~60 lines). It is the only option that validates Zitadel JWTs locally with no per-request IdP round-trip, and it lets the **existing permission predicates keep working** by populating a user object from claims.

> Trade-off to accept: pure local JWT validation **cannot detect revocation** — a token is valid until `exp`. Mitigate with short access-token lifetimes. If instant revocation is ever required, add an introspection call (defeats the local-only benefit). For most internal APIs, short lifetimes are the accepted norm.

### 1.2 Mapping OIDC roles/claims → DRF permissions

Zitadel is the **source of truth for roles**. Roles arrive in the access token (when Token Type = JWT and roles are requested/asserted) under:

```
urn:zitadel:iam:org:project:roles
```

…a **map keyed by role name**, each value mapping `orgId → orgPrimaryDomain`:

```json
{ "urn:zitadel:iam:org:project:roles": { "contributor": { "2233…": "jawafdehi.org" } } }
```

> **Verify against a live token:** Zitadel's docs render this example *array-wrapped* (`[ { "role": {...} } ]`) on the Claims reference page but as a plain map in the integration guide. The array brackets appear to be a docs artifact; real decoded tokens are a plain map. **The code below normalizes both shapes.** Also prefer the per-project variant `urn:zitadel:iam:org:project:{projectId}:roles` if you enable it (Zitadel recommends it over the generic claim).

The cleanest mapping for this codebase, which uses **Django auth Groups + django-rules predicates**, is to keep the predicates and feed them from the claim. Two viable user strategies:

- **(A) Lightweight non-persisted user** — fast, no DB write, but won't satisfy predicates that touch `request.user.groups` as an ORM relation.
- **(B) Real Django user via `get_or_create(username=sub)` + `user.groups.set(...)` from roles each request** — costs a per-request DB write and creates shadow accounts, **but plugs straight into the existing `Group`-based predicates, the django-auditlog actor, and any FK-to-User relations.**

**Recommendation for Jawafdehi: option (B).** The existing predicates (`HasContributorRole`, `CanReadReview`, the django-rules `is_contributor`/`is_readonly`/`has_role`, `DjangoModelPermissions`) all key on **Django Groups** (`Admin`, `Moderator`, `Contributor`, `ReadOnly`, `ReviewAssistant`), and audit attribution depends on a real `User`. Syncing roles→groups on each request preserves all of that untouched. (See §5 for the role↔group mapping table.)

### 1.3 Code sketch

`config/authentication.py`:

```python
import jwt
from jwt import PyJWKClient
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from rest_framework import authentication, exceptions

User = get_user_model()
ZITADEL_ROLES_CLAIM = "urn:zitadel:iam:org:project:roles"

# Map Zitadel project-role keys -> existing Django Group names.
ROLE_TO_GROUP = {
    "admin": "Admin",
    "moderator": "Moderator",
    "contributor": "Contributor",
    "readonly": "ReadOnly",
    "review_assistant": "ReviewAssistant",
    "ngm_silver": "NGM_SilverTier",
    "ngm_gold": "NGM_GoldTier",
    "ngm_platinum": "NGM_PlatinumTier",
}

# Module-level: JWKS fetched once, cached (default lifespan 300s),
# auto-refreshed when an unknown `kid` is seen (handles Zitadel key rotation).
_jwks_client = PyJWKClient(settings.ZITADEL_JWKS_URI)


def _extract_role_keys(claims) -> set[str]:
    raw = claims.get(ZITADEL_ROLES_CLAIM) or {}
    if isinstance(raw, list):                      # normalize docs' array-wrapped form
        merged = {}
        for item in raw:
            if isinstance(item, dict):
                merged.update(item)
        raw = merged
    return set(raw.keys()) if isinstance(raw, dict) else set()


class ZitadelJWTAuthentication(authentication.BaseAuthentication):
    keyword = "Bearer"

    def authenticate(self, request):
        header = authentication.get_authorization_header(request).split()
        if not header or header[0].lower() != self.keyword.lower().encode():
            return None  # not a bearer attempt
        if len(header) != 2:
            raise exceptions.AuthenticationFailed("Invalid Authorization header.")
        token = header[1].decode()

        try:
            signing_key = _jwks_client.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],                  # pin alg; never trust token header
                audience=settings.ZITADEL_AUDIENCE,    # your project ID / client ID
                issuer=settings.ZITADEL_ISSUER,        # instance custom domain
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
                leeway=settings.ZITADEL_LEEWAY,        # small clock-skew tolerance
            )
        except jwt.ExpiredSignatureError:
            raise exceptions.AuthenticationFailed("Token expired.")
        except jwt.InvalidAudienceError:
            raise exceptions.AuthenticationFailed("Invalid audience.")
        except jwt.InvalidIssuerError:
            raise exceptions.AuthenticationFailed("Invalid issuer.")
        except jwt.PyJWTError as e:
            raise exceptions.AuthenticationFailed(f"Invalid token: {e}")

        user = self._sync_user(claims)
        return (user, claims)  # request.user, request.auth

    def _sync_user(self, claims):
        # Option (B): real Django user mirrored from Zitadel each request.
        user, _ = User.objects.get_or_create(
            username=claims["sub"],
            defaults={"email": claims.get("email", ""), "is_active": True},
        )
        role_keys = _extract_role_keys(claims)
        group_names = {ROLE_TO_GROUP[r] for r in role_keys if r in ROLE_TO_GROUP}
        groups = Group.objects.filter(name__in=group_names)
        user.groups.set(groups)   # Zitadel is source of truth -> overwrite each request
        return user

    def authenticate_header(self, request):
        return f'{self.keyword} realm="api"'  # ensures 401 (not 403) on missing token
```

> If a per-request `user.groups.set()` write is too costly under load, cache the role→group sync (e.g. skip the write when the claim set is unchanged for that `sub`), or fall back to option (A) for read-heavy endpoints. Measure before optimizing.

### 1.4 Removing TokenAuthentication / SessionAuthentication safely

Today (`/damodaha-volunteer/backend/config/settings.py`):

```python
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework_simplejwt.authentication.JWTAuthentication",
        "config.auth.ChatServiceAccountAuthentication",   # subclass of DRF TokenAuthentication
        "rest_framework.authentication.SessionAuthentication",
    ],
    ...
}
```

Target:

```python
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "config.authentication.ZitadelJWTAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
}
```

`DEFAULT_AUTHENTICATION_CLASSES` is a full replacement (not a merge), so omitting the others removes them. Cleanup checklist:

1. Drop `rest_framework.authtoken` and `rest_framework_simplejwt` from `INSTALLED_APPS` (and migrate away the `authtoken_token` table when ready).
2. Remove the per-view `authentication_classes = [ChatServiceAccountAuthentication]` pins in `nesq/api_views.py` (L75, L210), `case_workflows/views.py` (L27, L97), `cases/api_views.py` (L39/L328) — they fall back to the new global default.
3. **Existing permission predicates need no changes** — they read `request.user.is_authenticated`, `is_superuser`, and `groups`, all of which option (B) populates from the JWT.
4. Keep Django's `SessionMiddleware`/`AuthenticationMiddleware` for the **admin site only** — the API surface no longer relies on session auth.
5. The `chat-jawafdehi-org` impersonation (the `X-Jawafdehi-User-Id` header → `ChatUserIdentity` → real user, with the `JWTAuditlogMiddleware` lazy actor) is a **separate concern** from transport auth — see §3.4 for how it survives the cutover.

---

## 2. FastAPI + OIDC (NES and NGM)

### 2.1 Library decision

| Dimension | **PyJWT** | Authlib | python-jose |
|---|---|---|---|
| Maintenance | Active, frequent releases | Active | Marginal (long release gaps) |
| Known CVEs | None notable | None notable | CVE-2024-33663 (alg confusion), CVE-2024-33664 (JWE DoS) fixed in 3.4.0; **unfixed** transitive `ecdsa` CVE-2024-23342 (Minerva timing) |
| JWKS fetch + cache | **Built-in `PyJWKClient`** (caches, auto-refresh on unknown `kid`) | Parses JWKS but **no remote fetch/cache** (DIY) | JWK parsing, no managed remote client |
| Fit for "fetch JWKS → verify iss/aud/exp" | **Best** | Heavy unless you need the full framework | Avoid |

**Decision: PyJWT + `PyJWKClient`**, same as Django — one validation model across all three services.

**Security scheme:** `fastapi.security.HTTPBearer` (cleanest for a pure resource server where bearer JWT is the only auth; `OAuth2*` schemes are for apps that issue tokens or want Swagger's IdP-login UX).

### 2.2 What to validate (RFC 9068 order)

1. Signature via JWKS (`jwks_uri` = `${ISSUER}/oauth/v2/keys`); **pin `algorithms=["RS256"]`**, reject `alg=none`.
2. `iss` exactly matches the configured issuer.
3. `aud` contains your resource identifier — for Zitadel, the **project ID** (most stable; Zitadel puts all client IDs of the project *plus* the project ID into `aud`).
4. `exp`/`nbf` with small `leeway`.
5. Optionally check the header `typ` is `at+jwt` to distinguish access tokens from ID tokens.

### 2.3 Code sketch (drop-in for the NGM stub; greenfield for NES)

This directly completes the existing `_verify_token` TODO in `/damodaha-volunteer/wt-ngm-v2/ngm/api/auth.py`:

```python
from typing import Annotated, Any
import jwt
from jwt import PyJWKClient
from jwt.exceptions import InvalidTokenError
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel

ISSUER     = settings.OIDC_ISSUER                 # e.g. https://<instance>.zitadel.cloud
JWKS_URI   = f"{ISSUER}/oauth/v2/keys"
AUDIENCE   = settings.OIDC_AUDIENCE               # the project ID
ROLES_CLAIM = "urn:zitadel:iam:org:project:roles"

bearer = HTTPBearer()                              # the only auth scheme
jwks_client = PyJWKClient(JWKS_URI)                # caches + auto-refresh on rotation


class Principal(BaseModel):
    sub: str
    claims: dict[str, Any]

    @property
    def roles(self) -> dict[str, dict[str, str]]:
        raw = self.claims.get(ROLES_CLAIM)
        if not raw:
            return {}
        if isinstance(raw, list):                  # normalize docs' array-wrapped form
            raw = raw[0] if raw else {}
        return raw or {}


def require_principal(
    creds: Annotated[HTTPAuthorizationCredentials, Depends(bearer)],
) -> Principal:
    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        signing_key = jwks_client.get_signing_key_from_jwt(creds.credentials)
        payload = jwt.decode(
            creds.credentials,
            signing_key.key,
            algorithms=["RS256"],
            audience=AUDIENCE,
            issuer=ISSUER,
        )
    except InvalidTokenError:
        raise unauthorized
    sub = payload.get("sub")
    if not sub:
        raise unauthorized
    return Principal(sub=sub, claims=payload)


def require_role(role: str):
    """Dependency factory enforcing a Zitadel project role."""
    def checker(principal: Annotated[Principal, Depends(require_principal)]) -> Principal:
        if role not in principal.roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Not enough permissions")
        return principal
    return checker
```

The existing NGM scope dependencies (`require_scope("ngm.query")`, `require_scope("ngm.ingest")` in `routes/query.py`, `routes/ingestion.py`) can either (a) keep using OAuth scopes if you choose to model NGM access as **scopes**, or (b) switch to `require_role(...)` if you model them as **project roles** (recommended — see §5). NES gets the same `require_principal` dependency added to its routers (it has none today).

> **401 vs 403:** use **401** for missing/invalid token, **403** for authenticated-but-unauthorized. Note FastAPI ≥0.122.0 changed built-in security failures from 403→401; pin and confirm your version.

---

## 3. Service-to-service (machine-to-machine)

### 3.1 Method decision: JWT Profile (Private Key JWT)

Zitadel states verbatim that *"private key JWT authentication is the recommended choice due to its benefits in security, performance, and control."* The decisive reason here: **the private key never leaves the calling service** (Zitadel stores only the public key), so there is no shared secret to leak — exactly the failure mode of the removed `chat-jawafdehi-org` DRF token.

- **Recommended:** JWT Profile (`grant_type=urn:ietf:params:oauth:grant-type:jwt-bearer`).
- **Acceptable fallback:** client credentials (`client_id`+`client_secret`) — "where simplicity and trust between servers are priorities," but a shared secret.
- **Avoid:** Personal Access Tokens (long-lived static bearer tokens — same anti-pattern you're removing).

### 3.2 Creating the service account

Console → **Service Accounts → New** (username + display name). Then **Keys → New → download JSON** (cannot be retrieved again). The JSON contains:

```
type   = "serviceaccount"
keyId  -> JWT header "kid"
key    -> PEM RSA private key (sign RS256)
userId -> JWT "iss" and "sub"
```

> Doc URLs moved: the current guides live under `/docs/guides/integrate/service-accounts/` (old `/service-users/` paths now 404).

### 3.3 Exact token requests

Both POST to `${ISSUER}/oauth/v2/token`, `Content-Type: application/x-www-form-urlencoded`.

**JWT Profile (recommended).** Build & sign an assertion (RS256, `kid`=keyId):

```
claims: { "iss": <userId>, "sub": <userId>,
          "aud": "https://${ISSUER}",
          "iat": <now>, "exp": <now+3600> }    # iat must not be older than 1h
```

```bash
curl -X POST https://${ISSUER}/oauth/v2/token \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  -d grant_type=urn:ietf:params:oauth:grant-type:jwt-bearer \
  -d assertion=<signed-JWT> \
  -d 'scope=openid urn:zitadel:iam:org:project:id:<RECEIVER_PROJECT_ID>:aud'
```

(`zitadel-tools key2jwt --audience=https://${ISSUER} --key=key.json` builds the assertion.)

**Client credentials (fallback):**

```bash
curl -X POST https://${ISSUER}/oauth/v2/token \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  -d grant_type=client_credentials \
  -d client_id=${CLIENT_ID} -d client_secret=${CLIENT_SECRET} \
  -d 'scope=openid urn:zitadel:iam:org:project:id:<RECEIVER_PROJECT_ID>:aud'
```

> **The audience scope is load-bearing and mandatory.** `urn:zitadel:iam:org:project:id:{projectId}:aud` (use the **receiver's** numeric project ID) puts that project into the token's `aud`. Without it, the receiver's `aud` check and Zitadel introspection will fail. To also carry roles, add the scope `urn:zitadel:iam:org:projects:roles` (note **plural `projects`** — the *scope* — which yields the singular-`project` *claim*).
>
> Response: `access_token`, `token_type: Bearer`, `expires_in` (default ~12h), `scope`. **No `refresh_token` for M2M.** So the caller caches the token and re-requests near expiry.

### 3.4 How the receiver validates inbound service tokens

**Validation is identical for machine and user tokens** — same `require_principal` / `ZitadelJWTAuthentication` path (JWT + JWKS), checking `aud` contains the receiver's project ID.

- **There is no claim that flags machine vs. human.** A service account is a user object; `sub` is just its user ID. To recognize "this is the chat service," match on the known `sub` and/or on a granted role — **out-of-band**, not via a `user_type` claim (none exists).
- Roles are carried for service accounts exactly as for humans (granted via Role Assignment), so the cleanest pattern is: grant the service account a dedicated role (e.g. `contributor` or a `service_chat` role) and authorize on that role.
- If instant revocation matters, use **introspection** (`${ISSUER}/oauth/v2/introspect`, returns `active`) instead of local validation, authenticating the receiver with its own Private Key JWT (`client_assertion` + `client_assertion_type=urn:ietf:params:oauth:client-assertion-type:jwt-bearer` — note this URN differs from the grant-type URN above).

**Replacing `chat-jawafdehi-org`:** model it as a Zitadel service account granted the `contributor` role. The MCP/chat caller fetches an M2M JWT-profile token and presents it as `Authorization: Bearer`. The existing **end-user impersonation** (`X-Jawafdehi-User-Id` header → `ChatUserIdentity` → real user, with `JWTAuditlogMiddleware` lazy actor) stays as an application-layer concern *layered on top of* OIDC transport auth — the new `ZitadelJWTAuthentication` authenticates the service account, then the impersonation logic resolves the acting end user for audit attribution. (Implementation note: that header→identity resolution can move into a small DRF auth subclass or middleware that runs after `ZitadelJWTAuthentication`.)

---

## 4. Zitadel specifics

### 4.1 Endpoints (relative to ISSUER; read from discovery, don't hardcode)

| Purpose | Path |
|---|---|
| Discovery | `/.well-known/openid-configuration` |
| **JWKS (`jwks_uri`)** | `/oauth/v2/keys` |
| Token | `/oauth/v2/token` |
| Userinfo | `/oidc/v1/userinfo` |
| Introspection | `/oauth/v2/introspect` |
| Revocation | `/oauth/v2/revoke` |

Self-hosted ISSUER is derived from `ExternalSecure` + `ExternalDomain` + `ExternalPort` (e.g. `https://zitadel.my.domain`).

### 4.2 Project roles & claims

- A **project role** has **Key** (used in code/tokens), **Display Name**, and optional **Group** (for bulk assignment).
- **Role Assignment** (formerly "Authorization"/"User Grant") binds a user *or service account* to roles. APIs still use old names (`CreateAuthorization`, v1 `AddUserGrant`).
- Roles claim: `urn:zitadel:iam:org:project:roles` = map `roleKey → {orgId: orgPrimaryDomain}`. Prefer the per-project form `urn:zitadel:iam:org:project:{projectId}:roles`.
- **Getting roles into the access token requires BOTH:** (1) Token Type = **JWT**, and (2) roles **requested** (scope `urn:zitadel:iam:org:projects:roles` or a single-role scope `urn:zitadel:iam:org:project:role:{rolekey}`) **or configured** via the project's **"Assert Roles on Authentication"** toggle. There is **no dedicated "assert roles on access token" toggle** and **no bare `roles` scope.**

**Reserved strings (singular/plural matters):**

| Purpose | Exact string | Kind |
|---|---|---|
| Audience = target project | `urn:zitadel:iam:org:project:id:{projectId}:aud` | scope |
| All granted project roles | `urn:zitadel:iam:org:projects:roles` (**plural** `projects`) | scope |
| Single role | `urn:zitadel:iam:org:project:role:{rolekey}` | scope |
| Roles land here | `urn:zitadel:iam:org:project:roles` (**singular** `project`) | claim |

### 4.3 Local JWT validation vs introspection

- **Opaque (Bearer) is Zitadel's default.** Set **Token Type = JWT** per application to enable local JWKS validation.
- **JWT + JWKS:** fast, scalable, no per-request IdP call; **no revocation detection** — cache JWKS, accept short token lifetimes. **Recommended default for these microservices.**
- **Opaque + introspection:** centralized control + revocation, at the cost of a per-request IdP call. Use only where instant revocation is required.

### 4.4 Seeding for local dev (docker-compose)

Lifecycle: `zitadel init` → `zitadel setup` (runs **FirstInstance** seeding) → `zitadel start`; compose uses combined **`start-from-init`**.

- **Masterkey:** exactly **32 chars**, immutable, via `--masterkey` / `ZITADEL_MASTERKEY`.
- **Initial setup overrides** go through `--steps` carrying a **`FirstInstance`** block. **Correction:** `FirstInstance:` lives in `cmd/setup/steps.yaml`, *not* `cmd/defaults.yaml` (which uses `DefaultInstance:`). Every field also maps to a `ZITADEL_FIRSTINSTANCE_*` env var.

Example `steps.yaml`:

```yaml
FirstInstance:
  InstanceName: ZITADEL
  DefaultLanguage: en
  MachineKeyPath:                     # JSON machine key written here on first setup
  Org:
    Name: ZITADEL
    Human:                            # admin (gets IAM_OWNER)
      UserName: zitadel-admin
      Password: Password1!
      PasswordChangeRequired: true
      Email: { Address: admin@example.com, Verified: true }
    Machine:                          # optional bootstrap service account
      Machine: { Username: bootstrap, Name: bootstrap }
      MachineKey:
        ExpirationDate: 2099-01-01T00:00:00Z
        Type: 1                       # 1 = JSON (only supported value)
```

Default admin after bootstrap: `zitadel-admin` / `Password1!`.

**Reproducible provisioning (recommended for local dev):** drop a bootstrap **machine key** via `FirstInstance.Machine.MachineKey` + `MachineKeyPath`, then feed that JSON into the **Terraform provider `zitadel/zitadel`** (`jwt_profile_file`) to create projects, OIDC apps (`zitadel_application_oidc`, with `auth_method_type = ..._PRIVATE_KEY_JWT`), `zitadel_project_role`s, `zitadel_machine_user`s, `zitadel_machine_key`s, and role assignments. `zitadel-tools key2jwt` converts a machine `key.json` to a JWT-profile assertion for scripting.

Quickstart files:
```bash
curl -fsSLO https://raw.githubusercontent.com/zitadel/zitadel/main/deploy/compose/docker-compose.yml
curl -fsSLO https://raw.githubusercontent.com/zitadel/zitadel/main/deploy/compose/.env.example
```
(Current compose ships a separate v2 login UI container at `/ui/v2/login` + Traefik — newer than older single-container blog examples.)

---

## 5. Migration mapping

### 5.1 Jawafdehi DRF roles/groups → Zitadel project roles

Today's groups (`/damodaha-volunteer/backend/cases/rules/predicates.py`, `review/permissions.py`): **Admin, Moderator, Contributor, ReadOnly, ReviewAssistant** (group-keyed predicates `is_admin`, `is_moderator`, `is_contributor`, `is_readonly`, `has_role`, `is_admin_or_moderator`).

| Existing Django Group | Zitadel project role key | Notes |
|---|---|---|
| Admin | `admin` | (superuser handled in-app) |
| Moderator | `moderator` | |
| Contributor | `contributor` | min role for mutations (`has_role`) |
| ReadOnly | `readonly` | grants `view_*`; deliberately does **not** imply `has_role` |
| ReviewAssistant | `review_assistant` | review-system role |

The `ROLE_TO_GROUP` dict in §1.3 syncs these on each request, so **predicates are unchanged**.

### 5.2 NGM Silver/Gold/Platinum → Zitadel roles

**Finding:** the NGM tiers are **rate-limit groups only** — no scope/permission semantics. Defined in `/damodaha-volunteer/backend/ngm/migrations/0001_create_rate_tier_groups.py` (Groups `NGM_SilverTier/GoldTier/PlatinumTier`) and consumed by `NGMQueryRateThrottle` in `/damodaha-volunteer/backend/ngm/api_views.py`:

```
NGM_SilverTier   -> 60/hour
NGM_GoldTier     -> 200/hour
NGM_PlatinumTier -> 500/hour   (Admin/Moderator also 500/hour)
```

**Migration:**

| Existing tier group | Zitadel project role | Rate limit (unchanged) |
|---|---|---|
| NGM_SilverTier | `ngm_silver` | 60/hour |
| NGM_GoldTier | `ngm_gold` | 200/hour |
| NGM_PlatinumTier | `ngm_platinum` | 500/hour |

- **Django NGM views:** keep `RoleBasedRateThrottle` but rekey `TIER_LIMITS`/`GROUP_PRIORITY` off the synced groups (the §1.3 mapping already creates `NGM_*Tier` groups from the `ngm_*` roles), and change the throttle bucket key from the DRF token key to the JWT `sub` (the token no longer exists). `CourtCaseDetailView` (currently `authentication_classes = []`, unauthenticated but throttled) can stay public or require a base role — product decision.
- **NGM FastAPI (`wt-ngm-v2`):** the stub's TODO ("map Zitadel roles/scopes → SQL tier") resolves to: read `ngm_silver/ngm_gold/ngm_platinum` from the roles claim, pick the highest, apply the same per-hour quota. The existing `require_scope("ngm.query"/"ngm.ingest")` dependencies can become `require_role(...)` (model NGM access as **roles**, consistent with everything else) — recommended over keeping a parallel scope vocabulary.

### 5.3 `chat-jawafdehi-org` service account → Zitadel service account

- Replace the management command `setup_chat_service_account.py` (creates a Django user + DRF token, adds to `Contributor` group) with a **Zitadel service account** granted the `contributor` role (Terraform `zitadel_machine_user` + `zitadel_machine_key` + role assignment).
- The chat/MCP caller uses **JWT Profile** to obtain an M2M token.
- End-user impersonation (`X-Jawafdehi-User-Id` → `ChatUserIdentity`) and the auditlog lazy actor are preserved as an app-layer step after OIDC auth (§3.4).

### 5.4 NES FastAPI

Greenfield — no auth today. Add the `require_principal` dependency (§2.3) to the `entities`/`relationships`/`schemas` routers; tighten the permissive CORS (`allow_origins=["*"]`) to known origins.

---

## 6. Open items / things to verify against a live instance

1. **Roles-claim shape** (plain map vs array-wrapped) — confirm against a real Zitadel JWT; the code normalizes both.
2. **`aud` contents** — confirm your API's project ID is present; ensure callers request the audience scope.
3. **Token Type = JWT** — confirm the setting is exposed for your app types and enabled; otherwise local validation won't work (use introspection).
4. **"Assert Roles on Authentication"** wording — confirm the exact Console label.
5. **Per-request `user.groups.set()` cost** in Django option (B) — measure; cache or fall back to lightweight user if hot.
6. **PyJWT version pinning** — `get_signing_key_from_jwt` returns a `PyJWK` (use `.key`) on current versions; pin and confirm.
7. **No machine-vs-human claim** — service identification is out-of-band on `sub`/role.

---

## Cited sources

**Zitadel**
- Endpoints (`/oauth/v2/keys`, token, introspect): https://zitadel.com/docs/apis/openidoauth/endpoints
- Claims (`urn:zitadel:iam:org:project:roles`, `aud`, `iss`): https://zitadel.com/docs/apis/openidoauth/claims
- Scopes (audience + role scopes; plural `projects`): https://zitadel.com/docs/apis/openidoauth/scopes
- Opaque vs JWT tokens: https://zitadel.com/docs/concepts/knowledge/opaque-tokens
- Token introspection (+ Private Key JWT): https://zitadel.com/docs/guides/integrate/token-introspection
- Retrieve user roles / assert-roles toggles: https://zitadel.com/docs/guides/integrate/retrieve-user-roles
- Service accounts — authenticate / private-key-jwt / client-credentials: https://zitadel.com/docs/guides/integrate/service-accounts/authenticate-service-accounts , .../private-key-jwt , .../client-credentials
- Roles / projects / users (Console): https://zitadel.com/docs/guides/manage/console/roles , /projects , /users
- Actions (Complement Token): https://zitadel.com/docs/apis/actions/complement-token
- Self-hosting configure / compose: https://zitadel.com/docs/self-hosting/manage/configure/configure , /self-hosting/deploy/compose
- `cmd/setup/steps.yaml` (FirstInstance): https://github.com/zitadel/zitadel/blob/main/cmd/setup/steps.yaml
- Terraform provider: https://github.com/zitadel/terraform-provider-zitadel ; zitadel-tools: https://github.com/zitadel/zitadel-tools

**PyJWT / DRF / FastAPI / libraries**
- PyJWT usage + API (`PyJWKClient`, `decode`): https://pyjwt.readthedocs.io/en/stable/usage.html , /api.html
- DRF authentication / permissions: https://www.django-rest-framework.org/api-guide/authentication/ , /api-guide/permissions/
- mozilla-django-oidc DRF integration: https://mozilla-django-oidc.readthedocs.io/en/stable/drf.html
- django-oauth-toolkit resource server: https://django-oauth-toolkit.readthedocs.io/en/latest/resource_server.html
- FastAPI security (`HTTPBearer`, current-user, OAuth2 scopes): https://fastapi.tiangolo.com/tutorial/security/ , /reference/security/ , /tutorial/security/get-current-user/ , /advanced/security/oauth2-scopes/
- python-jose CVEs: https://nvd.nist.gov/vuln/detail/CVE-2024-33663 , https://github.com/advisories/GHSA-wj6h-64fc-37mp (ecdsa)

**Specs**
- RFC 9068 (JWT access tokens): https://datatracker.ietf.org/doc/html/rfc9068
- RFC 7517 (JWK): https://datatracker.ietf.org/doc/html/rfc7517 ; RFC 8414 (discovery): https://datatracker.ietf.org/doc/html/rfc8414
