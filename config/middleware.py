import uuid

import structlog
from auditlog.middleware import AuditlogMiddleware as _BaseAuditlogMiddleware
from django.conf import settings
from django.utils.cache import patch_cache_control
from django.utils.functional import SimpleLazyObject

from config.db_router import force_primary_reads

_logger = structlog.get_logger(__name__)

REQUEST_ID_HEADER = "X-Request-Id"

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})


class ForcePrimaryReadsMiddleware:
    """Pin ORM reads to the primary where stale replica reads would be wrong.

    Reads are served from the ``pg-r`` replica by default, but the async
    standby can lag. Two request classes need read-your-writes consistency and
    are routed to the primary instead:

    * Any unsafe-method request (POST/PUT/PATCH/DELETE) — so reads issued after
      its own write (including the object echoed back in the response) are
      current.
    * Every Django admin request — the admin's save→redirect→list pattern reads
      across requests, and admin traffic is low-volume, so correctness wins.

    Plain GET API traffic keeps reading from the replica, which is the point of
    the split.
    """

    def __init__(self, get_response):
        self.get_response = get_response
        self.admin_prefix = getattr(settings, "ADMIN_URL_PREFIX", "/admin/")

    def __call__(self, request):
        if request.method not in _SAFE_METHODS or request.path.startswith(
            self.admin_prefix
        ):
            with force_primary_reads():
                return self.get_response(request)
        return self.get_response(request)


class RequestIdMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request_id = request.META.get(
            f"HTTP_{REQUEST_ID_HEADER.upper().replace('-', '_')}",
            str(uuid.uuid4()),
        )
        request.request_id = request_id
        structlog.contextvars.bind_contextvars(request_id=request_id)

        response = self.get_response(request)

        response[REQUEST_ID_HEADER] = request_id
        structlog.contextvars.unbind_contextvars("request_id")

        return response


def _strip_vary_cookie(response):
    """Remove the ``Cookie`` token from a response's ``Vary`` header.

    DRF ``SessionAuthentication`` makes Django add ``Vary: Cookie``, which marks
    the response uncacheable by shared caches. We drop only ``Cookie`` and keep
    any other tokens (``Origin``, ``Accept-Encoding``, …); there is no stdlib
    helper that removes a Vary token, only one that adds.
    """
    vary = response.headers.get("Vary")
    if not vary:
        return
    kept = [
        tok.strip()
        for tok in vary.split(",")
        if tok.strip() and tok.strip().lower() != "cookie"
    ]
    if kept:
        response.headers["Vary"] = ", ".join(kept)
    else:
        del response.headers["Vary"]


class PublicCacheHeadersMiddleware:
    """Make anonymous public GET responses edge-cacheable.

    For an anonymous (no ``Authorization`` header, no session cookie) GET/HEAD
    request on an allowlisted path, emit ``Cache-Control: public, s-maxage=…``
    and strip ``Cookie`` from ``Vary`` so Cloudflare can cache the response at
    the edge.

    The anonymous gate is the safety boundary: anonymous and staff callers see
    different data from the same URL (e.g. only staff see DRAFT cases — see
    ``CaseViewSet.get_queryset``), so authenticated responses must never be made
    cacheable. Responses that already carry a ``Set-Cookie`` are left alone since
    a shared cache cannot cache them anyway.
    """

    def __init__(self, get_response):
        self.get_response = get_response
        self.enabled = getattr(settings, "PUBLIC_CACHE_ENABLED", True)
        self.smaxage = getattr(settings, "PUBLIC_CACHE_SMAXAGE", 300)
        self.maxage = getattr(settings, "PUBLIC_CACHE_MAXAGE", 300)
        self.paths = tuple(getattr(settings, "PUBLIC_CACHE_PATHS", ()))

    def _is_anonymous_public_get(self, request):
        if request.method not in ("GET", "HEAD"):
            return False
        # DRF auth runs inside the view (before this post-processing) and writes
        # request.user through to the HttpRequest, so by now an authenticated
        # principal is visible here regardless of *how* it authenticated. This is
        # the backstop that keeps any non-anonymous response out of the cache.
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            return False
        if request.META.get("HTTP_AUTHORIZATION"):
            return False
        if settings.SESSION_COOKIE_NAME in request.COOKIES:
            return False
        return request.path.startswith(self.paths)

    def __call__(self, request):
        response = self.get_response(request)
        if not self.enabled or not self.paths:
            return response
        # Django keeps cookies in response.cookies until render, so a Set-Cookie
        # header may not exist yet — check both. A shared cache can't cache a
        # response that carries a cookie.
        if response.status_code != 200:
            return response
        if response.cookies or response.has_header("Set-Cookie"):
            return response
        if not self._is_anonymous_public_get(request):
            return response
        patch_cache_control(
            response, public=True, s_maxage=self.smaxage, max_age=self.maxage
        )
        _strip_vary_cookie(response)
        return response


class JWTAuditlogMiddleware(_BaseAuditlogMiddleware):
    """Auditlog middleware that binds a *lazy* actor resolved at write time.

    The stock ``AuditlogMiddleware`` reads ``request.user`` once, up front at
    the WSGI middleware layer — but our API authenticates inside DRF (OIDC JWT
    bearer or session), which runs *after* this middleware. So at middleware
    entry ``request.user`` is still ``AnonymousUser`` and every API-driven audit
    entry recorded ``actor=None``.

    Instead of re-running authentication here (which would double every JWT
    decode), we bind a ``SimpleLazyObject`` over ``request.user``. auditlog
    evaluates the actor lazily when it builds each ``LogEntry`` — by then the
    view's DRF authentication has populated ``request.user`` (DRF's
    ``request.user`` setter writes through to the underlying ``HttpRequest``, so
    it is visible here). The audit actor and the permission-checked principal are
    therefore the same user resolved from the bearer token. A lazy
    ``AnonymousUser`` / ``None`` fails auditlog's ``isinstance(user, User)``
    check and records no actor, exactly as before.
    """

    @staticmethod
    def _get_actor(request):
        return SimpleLazyObject(lambda: getattr(request, "user", None))
