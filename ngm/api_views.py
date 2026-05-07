import logging

from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.authentication import TokenAuthentication
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import SimpleRateThrottle
from rest_framework.views import APIView

from ngm.serializers import CourtCaseDetailSerializer, NGMQuerySerializer
from ngm.services import (
    execute_select_query,
    get_court_case_details,
    normalize_case_number,
    validate_query,
)

logger = logging.getLogger(__name__)


class NGMQueryRateThrottle(SimpleRateThrottle):
    scope = "ngm_token"
    rate = None
    TIER_LIMITS = {
        "Admin": "500/hour",
        "Moderator": "500/hour",
        "NGM_PlatinumTier": "500/hour",
        "NGM_GoldTier": "200/hour",
        "NGM_SilverTier": "60/hour",
    }
    DEFAULT_RATE = "60/hour"
    GROUP_PRIORITY = (
        "Admin",
        "Moderator",
        "NGM_PlatinumTier",
        "NGM_GoldTier",
        "NGM_SilverTier",
    )

    def get_rate(self):
        return self.DEFAULT_RATE

    def get_user_rate(self, user):
        if not user or not user.is_authenticated:
            return self.DEFAULT_RATE

        group_names = set(user.groups.values_list("name", flat=True))
        for group_name in self.GROUP_PRIORITY:
            if group_name in group_names:
                return self.TIER_LIMITS[group_name]

        return self.DEFAULT_RATE

    def allow_request(self, request, view):
        self.rate = self.get_user_rate(getattr(request, "user", None))
        self.num_requests, self.duration = self.parse_rate(self.rate)
        return super().allow_request(request, view)

    def get_cache_key(self, request, view):
        token = getattr(request, "auth", None)
        token_key = getattr(token, "key", None)
        if not token_key:
            return None

        return self.cache_format % {"scope": self.scope, "ident": token_key}


@extend_schema(
    summary="Run read-only query against NGM judicial database",
    description="""
    Executes validated SELECT queries against NGM judicial tables.

    Parameters:
    - query (string): SELECT query to execute
    - timeout (float, optional): Statement timeout in seconds (1-15, default: 15)

    Security controls:
    - Requires DRF token authentication
    - Rate limited per token based on NGM tier or staff role
    - Only SELECT queries are allowed
    - Only allowlisted judicial tables may be referenced
    - Server enforces statement timeout (max 15 seconds) and row cap
    """,
    request=NGMQuerySerializer,
)
class NGMJudicialQueryView(APIView):
    authentication_classes = [TokenAuthentication]
    permission_classes = [IsAuthenticated]
    throttle_classes = [NGMQueryRateThrottle]

    def post(self, request):
        serializer = NGMQuerySerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                {
                    "success": False,
                    "data": None,
                    "error": serializer.errors,
                    "query_time_ms": 0,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        query = serializer.validated_data["query"]
        timeout = serializer.validated_data["timeout"]

        is_valid, error_msg = validate_query(query)
        if not is_valid:
            return Response(
                {
                    "success": False,
                    "data": None,
                    "error": error_msg,
                    "query_time_ms": 0,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            result = execute_select_query(query, timeout)
        except ValueError as exc:
            # Map ValueError to appropriate status code based on message
            message = str(exc)
            message_lower = message.lower()

            if "not configured" in message_lower:
                status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            elif "database" in message_lower or "query failed" in message_lower:
                status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
            else:
                status_code = status.HTTP_400_BAD_REQUEST

            return Response(
                {
                    "success": False,
                    "data": None,
                    "error": message,
                    "query_time_ms": 0,
                },
                status=status_code,
            )
        except Exception:
            # Unexpected errors - log full details but return generic message
            logger.exception("Unexpected error executing NGM query")
            return Response(
                {
                    "success": False,
                    "data": None,
                    "error": "Internal server error",
                    "query_time_ms": 0,
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        return Response(
            {
                "success": True,
                "data": {
                    "columns": result["columns"],
                    "rows": result["rows"],
                    "row_count": result["row_count"],
                    "max_rows": result["max_rows"],
                },
                "error": None,
                "query_time_ms": result["query_time_ms"],
            },
            status=status.HTTP_200_OK,
        )


@extend_schema(
    summary="Get court case details by court and case number",
    description="""
    Retrieves complete case details including hearings and entities.
    
    URL format: /api/ngm/court_case/{case_id}
    where {case_id} = {court_identifier}:{case_number}
    
    Example: /api/ngm/court_case/supreme:081-CR-0081
    
    Returns:
    - Case details (registration, verdict, parties, etc.)
    - All hearings for the case (ordered by date, newest first)
    - All entities involved (plaintiffs, defendants, etc.)
    
    Returns 404 if case does not exist.
    """,
    responses={
        200: CourtCaseDetailSerializer,
        404: {"description": "Case not found"},
    },
)
class CourtCaseDetailView(APIView):
    authentication_classes = [TokenAuthentication]
    permission_classes = [IsAuthenticated]
    throttle_classes = [NGMQueryRateThrottle]

    def get(self, request, case_id):
        # Parse case_id format: court_identifier:case_number
        if ":" not in case_id:
            return Response(
                {
                    "error": "Invalid case_id format. Expected format: {court}:{case_number}"
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        parts = case_id.split(":", 1)
        court_identifier = parts[0].strip()
        case_number_raw = parts[1].strip()

        # Validate that both parts are non-empty
        if not court_identifier or not case_number_raw:
            return Response(
                {
                    "error": "Invalid case_id format. Both court identifier and case number are required"
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Normalize case number to standard format
        try:
            case_number = normalize_case_number(case_number_raw)
        except ValueError as exc:
            return Response(
                {"error": str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            result = get_court_case_details(court_identifier, case_number)
        except ValueError as exc:
            # Map ValueError to appropriate status code based on message
            message = str(exc)
            message_lower = message.lower()

            if "not configured" in message_lower:
                status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            elif "database" in message_lower or "query failed" in message_lower:
                status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
            else:
                status_code = status.HTTP_400_BAD_REQUEST

            return Response(
                {"error": message},
                status=status_code,
            )
        except Exception:
            # Unexpected errors - log full details but return generic message
            logger.exception("Unexpected error fetching court case details")
            return Response(
                {"error": "Internal server error"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        if result is None:
            return Response(
                {"error": f"Case not found: {case_id}"},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Combine case data with hearings and entities
        response_data = {
            **result["case"],
            "hearings": result["hearings"],
            "entities": result["entities"],
        }

        serializer = CourtCaseDetailSerializer(response_data)

        return Response(
            serializer.data,
            status=status.HTTP_200_OK,
        )
