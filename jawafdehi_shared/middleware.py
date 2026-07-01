import uuid

import structlog

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
