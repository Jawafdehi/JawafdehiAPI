from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from django.http import StreamingHttpResponse
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from caseworker.models import PublicChatConfig

from .agent import PublicChatAgentError, run_public_chat_agent
from .mcp_client import PublicChatMCPError
from .quota import check_and_increment_quota
from .serializers import PublicChatRequestSerializer, PublicChatResponseSerializer

logger = logging.getLogger(__name__)


class PublicChatView(APIView):
    permission_classes = [AllowAny]
    authentication_classes: list = []

    def post(self, request):
        config = self._get_active_config()
        if config is None or not config.enabled:
            return Response(
                {"detail": "Public chat is not available right now."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        serializer = PublicChatRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        payload = serializer.validated_data

        session_id = payload.get("session_id") or uuid.uuid4().hex
        question = payload["question"]
        if len(question) > config.max_question_chars:
            return Response(
                {
                    "detail": (
                        f"Question is too long. Maximum length is "
                        f"{config.max_question_chars} characters."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        quota = check_and_increment_quota(config, request, session_id)
        if not quota["allowed"]:
            return Response(
                {
                    "detail": "Public chat query limit reached.",
                    "error": "quota_exceeded",
                    "limit": quota["limit"],
                    "window_seconds": quota["window_seconds"],
                },
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        history = self._bound_history(payload.get("history", []), config)
        language = payload.get("language") or "auto"
        try:
            response_data = run_public_chat_agent(
                config=config,
                question=question,
                history=history,
                language=language,
                session_id=session_id,
            )
        except PublicChatMCPError as exc:
            logger.warning(
                "public_chat_tool_setup_failed error_type=%s",
                type(exc).__name__,
                exc_info=True,
                extra={"error_type": type(exc).__name__},
            )
            return Response(
                {
                    "detail": "Public chat tools are not available right now.",
                    "error": "tool_setup_failed",
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except PublicChatAgentError as exc:
            logger.warning(
                "public_chat_agent_failed error_type=%s",
                type(exc).__name__,
                exc_info=True,
                extra={"error_type": type(exc).__name__},
            )
            return Response(
                {
                    "detail": "Public chat answer generation failed.",
                    "error": "answer_generation_failed",
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        PublicChatResponseSerializer(data=response_data).is_valid(raise_exception=True)
        return Response(response_data)

    def _get_active_config(self):
        return (
            PublicChatConfig.objects.select_related("prompt", "llm_provider")
            .prefetch_related("prompt__skills")
            .filter(is_active=True)
            .first()
        )

    def _bound_history(
        self, history: list[dict[str, str]], config
    ) -> list[dict[str, str]]:
        bounded = history[-config.max_history_turns * 2 :]
        total = 0
        result = []
        for item in reversed(bounded):
            content = item.get("content", "")
            total += len(content)
            if total > config.max_history_chars:
                break
            result.append(item)
        return list(reversed(result))


class PublicChatStreamView(APIView):
    permission_classes = [AllowAny]
    authentication_classes: list = []

    def post(self, request):
        def events():
            yield _sse_event("status", {"stage": "accepted"})
            response = PublicChatView().post(request)
            payload = {
                "status": getattr(response, "status_code", 200),
                "data": getattr(response, "data", {}),
            }
            yield _sse_event("final", payload)

        response = StreamingHttpResponse(events(), content_type="text/event-stream")
        response["Cache-Control"] = "no-cache"
        response["X-Accel-Buffering"] = "no"
        return response


def _sse_event(event: str, payload: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
