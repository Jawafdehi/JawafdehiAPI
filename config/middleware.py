import uuid

import structlog
from auditlog.middleware import AuditlogMiddleware as _BaseAuditlogMiddleware
from django.utils.functional import SimpleLazyObject

_logger = structlog.get_logger(__name__)

REQUEST_ID_HEADER = "X-Request-Id"


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
