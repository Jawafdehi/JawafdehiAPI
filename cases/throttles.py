"""Role-aware rate throttles shared across the API.

A single base, :class:`RoleBasedRateThrottle`, owns all the throttling
machinery: choosing a per-request rate from the caller's role, parsing it, and
bucketing requests in the cache. Subclasses only *declare* their tiers and tune
three small hooks:

- ``resolve_rate`` — turn a ``TIER_LIMITS`` value into a concrete ``N/period``
  rate (literal passthrough by default; the global throttle resolves keys
  against ``DEFAULT_THROTTLE_RATES`` so rates stay tunable from settings).
- ``get_role_names`` — the set of role names to match against
  ``GROUP_PRIORITY`` (Django group names by default).
- ``get_authenticated_ident`` — the cache identity for an authenticated caller
  (the user's pk by default; NGM overrides this to bucket per API token).
"""

from rest_framework.settings import api_settings
from rest_framework.throttling import AnonRateThrottle as _AnonRateThrottle
from rest_framework.throttling import SimpleRateThrottle


def get_client_ident(request):
    """Real client IP for throttling, behind Cloudflare + Traefik.

    The edge is Cloudflare and the cluster ingress (klipper-svclb, Cluster
    externalTrafficPolicy) SNATs the L3 source, so ``REMOTE_ADDR`` is a useless
    in-cluster proxy IP and every caller would otherwise share one throttle
    bucket. Cloudflare sets ``CF-Connecting-IP`` to the true client and
    overwrites any client-supplied value, so it is both correct and unspoofable
    through the proxy; prefer it. Fall back to the first non-empty
    ``X-Forwarded-For`` hop, then the peer address.

    Every candidate is stripped and skipped if blank: a whitespace-only header
    or a leading comma (``", 1.2.3.4"``) must not yield an empty ident, which
    would collapse those callers back into one shared bucket.
    """
    cf_ip = (request.META.get("HTTP_CF_CONNECTING_IP") or "").strip()
    if cf_ip:
        return cf_ip
    for hop in (request.META.get("HTTP_X_FORWARDED_FOR") or "").split(","):
        hop = hop.strip()
        if hop:
            return hop
    return request.META.get("REMOTE_ADDR")


class AnonRateThrottle(_AnonRateThrottle):
    """``AnonRateThrottle`` that buckets by the real client IP (see
    :func:`get_client_ident`) instead of DRF's proxy-naive ident."""

    def get_ident(self, request):
        return get_client_ident(request)


class RoleBasedRateThrottle(SimpleRateThrottle):
    """Base throttle whose rate is chosen by the requesting user's role.

    Subclasses declare a ``scope``, a ``DEFAULT_RATE`` (for anonymous and
    role-less users), and a ``GROUP_PRIORITY`` tuple whose entries key into
    ``TIER_LIMITS``. The first role in ``GROUP_PRIORITY`` that the user holds
    wins, so list roles highest-tier first.

    Authenticated requests are bucketed by user identity (so a user's quota
    follows them across addresses); anonymous requests fall back to client IP.
    """

    scope = None
    rate = None
    TIER_LIMITS: dict[str, str] = {}
    DEFAULT_RATE: str | None = None
    GROUP_PRIORITY: tuple[str, ...] = ()

    # --- rate selection -----------------------------------------------------

    def resolve_rate(self, value):
        """Turn a ``TIER_LIMITS``/``DEFAULT_RATE`` entry into a concrete rate.

        Default is passthrough (entries are literal ``N/period`` strings).
        """
        return value

    def get_rate(self):
        # SimpleRateThrottle.__init__ calls this once to seed num_requests /
        # duration; allow_request re-parses the real per-user rate per request.
        return self.resolve_rate(self.DEFAULT_RATE)

    def get_role_names(self, user):
        """Role names held by ``user``, matched against ``GROUP_PRIORITY``."""
        return set(user.groups.values_list("name", flat=True))

    def get_user_rate(self, user):
        default_rate = self.get_rate()
        if not user or not user.is_authenticated:
            return default_rate

        role_names = self.get_role_names(user)
        for role in self.GROUP_PRIORITY:
            if role in role_names:
                # .get() (not []) so a role listed in GROUP_PRIORITY but missing
                # from TIER_LIMITS degrades to the default rate instead of
                # raising KeyError. Also covers an unset tier rate.
                return self.resolve_rate(self.TIER_LIMITS.get(role)) or default_rate

        return default_rate

    def allow_request(self, request, view):
        self.rate = self.get_user_rate(getattr(request, "user", None))
        self.num_requests, self.duration = self.parse_rate(self.rate)
        return super().allow_request(request, view)

    # --- cache bucketing ----------------------------------------------------

    def get_authenticated_ident(self, request):
        """Cache identity for an authenticated caller. Defaults to user pk."""
        return request.user.pk

    def get_ident(self, request):
        return get_client_ident(request)

    def get_cache_key(self, request, view):
        user = getattr(request, "user", None)
        if user and user.is_authenticated:
            ident = self.get_authenticated_ident(request)
        else:
            # Anonymous: bucket by IP so the limit still applies.
            ident = self.get_ident(request)
        return self.cache_format % {"scope": self.scope, "ident": ident}


class RoleBasedUserRateThrottle(RoleBasedRateThrottle):
    """Global default throttle that raises limits for staff and contributors.

    Tiers map to keys in ``DEFAULT_THROTTLE_RATES`` so rates stay tunable from
    settings:

    - ``staff``        -> Admin/Moderator group members, superusers, or any user
                          with Django's ``is_staff`` flag set.
    - ``contributor``  -> Contributor group members.
    - ``user``         -> every other authenticated user (and the fallback when a
                          tier rate is unset).

    Authenticated callers are bucketed by user pk (inherited default), matching
    DRF's stock ``UserRateThrottle`` so users sharing a NAT/proxy IP keep
    independent quotas.
    """

    scope = "user"
    DEFAULT_RATE = "user"
    # Map roles to DEFAULT_THROTTLE_RATES keys; staff is the top tier.
    TIER_LIMITS = {
        "Admin": "staff",
        "Moderator": "staff",
        "Contributor": "contributor",
    }
    GROUP_PRIORITY = ("Admin", "Moderator", "Contributor")

    def resolve_rate(self, key):
        # TIER_LIMITS/DEFAULT_RATE hold settings keys; look them up live so the
        # rates can be tuned without code changes.
        if key is None:
            return None
        return api_settings.DEFAULT_THROTTLE_RATES.get(key)

    def get_role_names(self, user):
        # is_staff is unused in this codebase today, but honoring it (and
        # superuser) keeps the built-in flags meaningful and future-proof.
        # Both map onto the top "Admin" tier, so short-circuit and skip the
        # groups query on this per-request hot path.
        if user.is_superuser or user.is_staff:
            return {"Admin"}
        return super().get_role_names(user)
