import uuid

import structlog
from auditlog.middleware import AuditlogMiddleware as _BaseAuditlogMiddleware
from django.conf import settings
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


class JWTAuditlogMiddleware(_BaseAuditlogMiddleware):
    """Auditlog middleware that binds a *lazy* actor resolved at write time.

    The stock ``AuditlogMiddleware`` reads ``request.user`` once, up front at
    the WSGI middleware layer — but our API authenticates inside DRF (JWT, the
    impersonation-aware ``ChatServiceAccountAuthentication`` token auth, or
    session), which runs *after* this middleware. So at middleware entry
    ``request.user`` is still ``AnonymousUser`` and every API-driven audit
    entry recorded ``actor=None``.

    Instead of re-running authentication here (which would double every token
    lookup / JWT decode and re-derive impersonation), we bind a
    ``SimpleLazyObject`` over ``request.user``. auditlog evaluates the actor
    lazily when it builds each ``LogEntry`` — by then the view's DRF
    authentication has populated ``request.user`` (DRF's ``request.user``
    setter writes through to the underlying ``HttpRequest``, so it is visible
    here). Because ``ChatServiceAccountAuthentication`` resolves the
    impersonated end user, the audit actor and the permission-checked principal
    are guaranteed to match. A lazy ``AnonymousUser`` / ``None`` fails
    auditlog's ``isinstance(user, User)`` check and records no actor, exactly as
    before.
    """

    @staticmethod
    def _get_actor(request):
        return SimpleLazyObject(lambda: getattr(request, "user", None))
