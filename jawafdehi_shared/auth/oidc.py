"""OIDC (Zitadel) JWT authentication — shared across all Jawafdehi-platform DRF
services (nes, ngm, jawafdehi).

This is a pure *resource-server* authenticator: it validates a Zitadel-issued
JWT **access token** on every request using local JWKS validation (PyJWT +
``PyJWKClient``). It does not run a browser login / SSO flow.

Each service adds ``jawafdehi_shared.auth.oidc.OIDCAuthentication`` to its
``REST_FRAMEWORK['DEFAULT_AUTHENTICATION_CLASSES']`` and configures the OIDC_*
settings. This single implementation replaces the per-service FastAPI OIDC code
that NES and NGM each carried before the Django consolidation.

Zitadel is the source of truth for roles. Roles arrive in the access token
under the claim ``urn:zitadel:iam:org:project:roles`` (a map keyed by role
name). On each request we get-or-create a Django ``User`` keyed on the OIDC
``sub`` claim and SYNC the user's Django Groups from the role claim, so the
existing django-rules predicates (which key on ``user.groups``) keep working
untouched.

See ``/damodaha-volunteer/think-big/shared/research/oidc-zitadel-integration.md``
for the platform decision and the Zitadel specifics.

Migration note (DRF token auth removal / chat service account):
    ``jawafdehi_shared.identity.ChatServiceAccountAuthentication`` (the ``chat-jawafdehi-org``
    DRF token) is being retired in favour of a Zitadel **service account** — a
    normal OIDC principal (machine user) granted the ``contributor`` role,
    authenticated by exactly the same JWT+JWKS path below. A service account is
    indistinguishable from a human at the transport layer: ``sub`` is just its
    user id and there is no machine-vs-human claim. The existing end-user
    impersonation (``X-Jawafdehi-User-Id`` header -> ``ChatUserIdentity`` -> real
    user) is a separate application-layer concern that layers *on top of* this
    authenticator; it can move into a thin DRF auth subclass or middleware that
    runs after ``OIDCAuthentication``. ``OIDC_SERVICE_ACCOUNT_SUBJECTS`` /
    ``OIDC_SERVICE_ACCOUNT_ROLE`` (see settings) are provided so that layer can
    recognise the service account out-of-band on ``sub`` and/or role.
"""

from __future__ import annotations

import re
import threading

import jwt
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from jwt import PyJWKClient
from rest_framework import authentication, exceptions

User = get_user_model()

# The Zitadel project-role key that, when present in a token, additionally
# promotes the Django user to ``is_superuser`` (see _sync_user). Kept separate
# from the role->group map so the superuser semantics are explicit and the group
# sync stays purely data-driven. Overridable via settings.OIDC_SUPERUSER_ROLE.
DEFAULT_SUPERUSER_ROLE = "admin"

# Zitadel emits granted project roles in this claim (singular "project").
# It is a map: roleKey -> {orgId: orgPrimaryDomain}. The claim read by default
# is configurable via settings.OIDC_ROLES_CLAIM.
DEFAULT_ROLES_CLAIM = "urn:zitadel:iam:org:project:roles"

# Machine users (client-credentials grant — e.g. the jobs consumer service
# account) never receive the generic claim above: with the
# ``urn:zitadel:iam:org:projects:roles`` scope Zitadel emits ONLY the
# per-project variant ``urn:zitadel:iam:org:project:{projectId}:roles``, even
# with projectRoleAssertion enabled on the project. Human (auth-code) tokens
# carry the generic claim. Roles are therefore merged from the configured claim
# AND the per-project variant — but ONLY for project ids this API already
# trusts as itself (settings.OIDC_AUDIENCE). A token can pass the audience
# check while ALSO carrying role claims from sibling Zitadel projects in the
# same org (PyJWT accepts extra audiences, and the projects:roles scope asserts
# every project the principal holds grants in); honoring those would let a
# colliding roleKey like ``admin`` granted in an unrelated project escalate to
# superuser here.
_PER_PROJECT_ROLES_CLAIM_RE = re.compile(
    r"^urn:zitadel:iam:org:project:(\d+):roles$"
)


def _trusted_project_ids() -> set[str]:
    """Project ids whose per-project role claims this API honors (from aud)."""
    aud = getattr(settings, "OIDC_AUDIENCE", None)
    if not aud:
        return set()
    if isinstance(aud, str):
        aud = [aud]
    return {str(a) for a in aud}

# Default Zitadel project-role key -> existing Django Group name. Overridable via
# settings.OIDC_ROLE_TO_GROUP. The Group names mirror those the predicates and
# create_groups.py use (Admin, Moderator, Caseworker, ReadOnly, Public,
# ReviewAssistant) plus the NGM rate-limit tier groups.
#
# Role model (v2):
#   admin     -> Admin group  (AND user.is_superuser=True, set in _sync_user)
#   moderator -> Moderator
#   caseworker-> Caseworker   (renamed from the old "contributor" role)
#   readonly  -> ReadOnly     (system-wide read INCLUDING casework view)
#   public    -> Public       (public read EXCLUDING casework view)
DEFAULT_ROLE_TO_GROUP = {
    "admin": "Admin",
    "moderator": "Moderator",
    "caseworker": "Caseworker",
    "readonly": "ReadOnly",
    "public": "Public",
    "review_assistant": "ReviewAssistant",
    "ngm_silver": "NGM_SilverTier",
    "ngm_gold": "NGM_GoldTier",
    "ngm_platinum": "NGM_PlatinumTier",
}


# The JWKS client caches keys (default ~300s lifespan) and auto-refreshes when an
# unknown `kid` is seen, which handles Zitadel key rotation. Build it lazily and
# once, guarded by a lock, so settings can be missing in non-API contexts (e.g.
# management commands) without import-time failures.
_jwks_client: PyJWKClient | None = None
_jwks_lock = threading.Lock()


def _get_jwks_client() -> PyJWKClient:
    global _jwks_client
    if _jwks_client is None:
        with _jwks_lock:
            if _jwks_client is None:
                jwks_uri = getattr(settings, "OIDC_JWKS_URI", None)
                if not jwks_uri:
                    raise exceptions.AuthenticationFailed(
                        "OIDC authentication is not configured (OIDC_JWKS_URI)."
                    )
                # Send a browser-like User-Agent. The OIDC provider
                # (auth.jawafdehi.org) sits behind Cloudflare, whose bot
                # protection 403s the default `Python-urllib/x.y` UA — which
                # fails JWKS retrieval and surfaces as "Invalid token: Fail to
                # fetch data from the url, err: HTTP Error 403: Forbidden" on
                # every authenticated request. Overridable via settings.
                _jwks_client = PyJWKClient(
                    jwks_uri,
                    cache_keys=True,
                    lifespan=getattr(settings, "OIDC_JWKS_CACHE_SECONDS", 300),
                    headers={
                        "User-Agent": getattr(
                            settings,
                            "OIDC_JWKS_USER_AGENT",
                            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                        ),
                    },
                )
    return _jwks_client


def reset_jwks_client() -> None:
    """Reset the module-level JWKS client.

    Test hook: lets tests inject a fresh client / re-read settings between
    cases. Not used in production.
    """
    global _jwks_client
    with _jwks_lock:
        _jwks_client = None


def extract_role_keys(claims: dict) -> set[str]:
    """Return the set of Zitadel project-role keys present in ``claims``.

    Merges the configured generic claim (settings.OIDC_ROLES_CLAIM) with the
    per-project ``urn:zitadel:iam:org:project:{id}:roles`` variant — machine
    users' client-credentials tokens carry ONLY the latter. Per-project claims
    are honored ONLY for this API's own project ids (OIDC_AUDIENCE): role keys
    asserted by sibling projects in the same org are ignored, so a colliding
    key (e.g. ``admin``) granted elsewhere cannot map to privileges here.
    Normalizes both the plain-map shape returned by real tokens and the
    array-wrapped shape Zitadel's docs sometimes render.
    """
    claim_name = getattr(settings, "OIDC_ROLES_CLAIM", DEFAULT_ROLES_CLAIM)
    trusted_projects = _trusted_project_ids()
    keys: set[str] = set()
    for name, raw in (claims or {}).items():
        if name != claim_name:
            m = _PER_PROJECT_ROLES_CLAIM_RE.match(name)
            if m is None or m.group(1) not in trusted_projects:
                continue
        if isinstance(raw, list):  # normalize docs' array-wrapped form
            merged: dict = {}
            for item in raw:
                if isinstance(item, dict):
                    merged.update(item)
            raw = merged
        if isinstance(raw, dict):
            keys |= set(raw.keys())
    return keys


class OIDCAuthentication(authentication.BaseAuthentication):
    """Validate a Zitadel JWT access token and sync roles -> Django Groups.

    On success ``request.user`` is a real Django ``User`` (keyed on the OIDC
    ``sub``) whose group membership mirrors the token's project roles, and
    ``request.auth`` is the decoded claims dict.
    """

    keyword = "Bearer"

    def authenticate(self, request):
        header = authentication.get_authorization_header(request).split()
        if not header or header[0].lower() != self.keyword.lower().encode():
            # Not a Bearer attempt -> let other authenticators (if any) run and
            # ultimately produce a 401 via authenticate_header().
            return None
        if len(header) != 2:
            raise exceptions.AuthenticationFailed(
                "Invalid Authorization header. No credentials or token contains spaces."
            )

        token = header[1].decode()
        claims = self._decode(token)
        user = self._sync_user(claims)
        return (user, claims)

    def _decode(self, token: str) -> dict:
        try:
            signing_key = _get_jwks_client().get_signing_key_from_jwt(token)
            return jwt.decode(
                token,
                signing_key.key,
                # Pin the algorithm; never trust the alg in the token header
                # (defends against alg-confusion / alg=none).
                algorithms=getattr(settings, "OIDC_ALGORITHMS", ["RS256"]),
                audience=settings.OIDC_AUDIENCE,
                issuer=settings.OIDC_ISSUER,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
                leeway=getattr(settings, "OIDC_LEEWAY", 30),
            )
        except jwt.ExpiredSignatureError:
            raise exceptions.AuthenticationFailed("Token has expired.")
        except jwt.InvalidAudienceError:
            raise exceptions.AuthenticationFailed("Invalid token audience.")
        except jwt.InvalidIssuerError:
            raise exceptions.AuthenticationFailed("Invalid token issuer.")
        except jwt.PyJWTError as exc:
            raise exceptions.AuthenticationFailed(f"Invalid token: {exc}")

    def _sync_user(self, claims: dict):
        """Get-or-create the Django user for ``sub`` and sync its groups.

        Zitadel is the source of truth for roles, so the user's groups are
        OVERWRITTEN from the token's role claim on every request. Group rows are
        not created here — only existing Groups whose names are in the mapping
        are attached, so the role->group mapping is authoritative but the Group
        catalogue stays under create_groups.py's control.

        Superuser sync (role model v2): the ``admin`` role means "Admin group AND
        Django superuser". Zitadel remains authoritative, so ``is_superuser`` is
        driven directly off the role claim on every request — set True when the
        admin role is present and cleared to False when it is absent (so a
        revoked admin role immediately drops superuser, not just the group).
        """
        sub = claims["sub"]
        user, _ = User.objects.get_or_create(
            username=sub,
            defaults={
                "email": claims.get("email", "") or "",
                "is_active": True,
            },
        )

        role_to_group = getattr(settings, "OIDC_ROLE_TO_GROUP", DEFAULT_ROLE_TO_GROUP)
        role_keys = extract_role_keys(claims)
        group_names = {role_to_group[r] for r in role_keys if r in role_to_group}
        # Compare against current membership and only write when it changed, to
        # avoid a needless m2m write on every request for the common steady
        # state.
        current = set(user.groups.values_list("name", flat=True))
        if current != group_names:
            groups = Group.objects.filter(name__in=group_names)
            user.groups.set(groups)

        # admin role -> Django superuser (and unset when the role is absent).
        superuser_role = getattr(
            settings, "OIDC_SUPERUSER_ROLE", DEFAULT_SUPERUSER_ROLE
        )
        should_be_superuser = superuser_role in role_keys
        if user.is_superuser != should_be_superuser:
            user.is_superuser = should_be_superuser
            user.save(update_fields=["is_superuser"])
        return user

    def authenticate_header(self, request):
        # Returning a value makes DRF respond 401 (not 403) when no/invalid
        # credentials are supplied.
        return f'{self.keyword} realm="api"'
