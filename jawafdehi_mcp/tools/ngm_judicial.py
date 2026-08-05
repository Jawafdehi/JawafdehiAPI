"""NGM judicial data query tool."""

import json
import os
from typing import Any

import httpx
import structlog
from courts.query_guard import validate_query
from mcp.types import TextContent

from ..api_transport import embedded_api_client_kwargs
from .base import BaseTool, ToolExecutionResult
from .ngm_proxy import NGMProxyError, execute_ngm_proxy_query, get_jawafdehi_api_config

logger = structlog.get_logger()


def _json_response(payload: dict[str, Any]) -> list[TextContent]:
    return [
        TextContent(
            type="text",
            text=json.dumps(payload, ensure_ascii=False),
        )
    ]


def _error_response(message: str) -> ToolExecutionResult:
    return ToolExecutionResult(
        _json_response(
            {
                "success": False,
                "data": None,
                "error": message,
                "query_time_ms": 0,
            }
        ),
        is_error=True,
    )


def _get_max_query_timeout() -> float:
    """Return max allowed query timeout in seconds (env MCP_QUERY_TIMEOUT, default 15)."""
    raw = os.getenv("MCP_QUERY_TIMEOUT", "15")
    try:
        return float(raw)
    except (TypeError, ValueError):
        logger.warning("invalid_mcp_query_timeout", value=raw)
        return 15.0


class NGMJudicialTool(BaseTool):
    """Tool for querying Nepal's judicial data from NGM database."""

    @property
    def name(self) -> str:
        return "ngm_query_judicial"

    @property
    def description(self) -> str:
        return """Search judicial cases from Nepal's court system. Execute SELECT queries against NGM court and court case tables. By default, we should fetch only a few results (e.g. 5) to avoid eating up the context window.

Public table schemas (scope-only callers receive these fixed projections):
- courts: identifier (PK), court_type, full_name_nepali, full_name_english
- court_cases: case_number (PK), court_identifier (PK), registration_date_bs, registration_date_ad, case_type, case_status, plaintiff, defendant, nes_id, document_sources. Soft-deleted rows are excluded.
- court_case_hearings: id (PK), case_number, court_identifier, hearing_date_bs, hearing_date_ad, bench, bench_type, judge_names, lawyer_names, serial_no, case_status, decision_type, remarks, extra_data
- court_case_entities: id (PK), case_number, court_identifier, side, name, address, nes_id

Court IDs (court_identifier):
- Supreme & Special: supreme, special
- High Courts: biratnagarhc, illamhc, dhankutahc, okhaldhungahc, janakpurhc, rajbirajhc, birganjhc, patanhc, hetaudahc, pokharahc, baglunghc, tulsipurhc, butwalhc, nepalgunjhc, surkhethc, jumlahc, dipayalhc, mahendranagarhc
- District Courts: achhamdc, argakhanchidc, etc."""

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "SQL SELECT query to execute. Must be read-only. By default, we should fetch only a few results (e.g. 5) to avoid eating up the context window.",
                },
                "timeout": {
                    "type": "number",
                    "description": "Query timeout in seconds (default: 15)",
                    "default": 15,
                },
            },
            "required": ["query"],
        }

    def _validate_environment(self) -> tuple[str, str | None]:
        """
        Validate required environment variables.

        Returns:
            Tuple of (base_url, optional token)

        Raises:
            ValueError: If required API configuration is missing or invalid
        """
        return get_jawafdehi_api_config()

    def _validate_query(self, query: str) -> tuple[bool, str | None]:
        """Apply the same SQL policy enforced by the API execution boundary."""
        return validate_query(query)

    async def execute(self, arguments: dict[str, Any]) -> list[TextContent]:
        """Execute the NGM judicial query tool."""
        # Extract arguments
        query = arguments.get("query")
        raw_timeout = arguments.get("timeout", 15)

        # Validate and clamp timeout
        try:
            timeout = float(raw_timeout)
        except (TypeError, ValueError):
            return _error_response("timeout must be a number")
        if timeout <= 0:
            return _error_response("timeout must be greater than 0")
        timeout = min(timeout, _get_max_query_timeout())

        if not query:
            return _error_response("Query parameter is required")

        # Validate query
        is_valid, error_msg = self._validate_query(query)
        if not is_valid:
            return _error_response(error_msg or "Query is not allowed")

        # Execute query
        try:
            base_url, token = self._validate_environment()

            async with httpx.AsyncClient(**embedded_api_client_kwargs()) as client:
                payload = await execute_ngm_proxy_query(
                    client,
                    base_url,
                    token,
                    query,
                    timeout=timeout,
                )

            # Wrap in stable tool-owned envelope
            proxy_data = payload.get("data") or {}
            response = {
                "success": True,
                "data": {
                    "columns": proxy_data.get("columns", []),
                    "rows": proxy_data.get("rows", []),
                    "row_count": proxy_data.get(
                        "row_count", len(proxy_data.get("rows", []))
                    ),
                },
                "error": None,
                "query_time_ms": payload.get("query_time_ms", 0),
            }
            return _json_response(response)
        except NGMProxyError as e:
            # A 4xx means the caller's SQL was rejected by the gated query plane
            # (subquery/CTE/non-allowlisted table/bad column) — an expected input
            # error, not a server fault, so log at warning: diagnosable in logs but
            # below Sentry's ERROR capture threshold so it does not page. A 5xx (or
            # a malformed 2xx) is a real upstream problem and stays at error.
            log = logger.warning if 400 <= e.status_code < 500 else logger.error
            log(
                "ngm_query_failed",
                error=str(e),
                status_code=e.status_code,
                category="proxy_api",
            )
            return _error_response(f"Proxy API error: {e}")
        except RuntimeError as e:
            logger.error("ngm_query_failed", error=str(e), category="proxy_api")
            return _error_response(f"Proxy API error: {e}")
        except httpx.HTTPError as e:
            logger.error("ngm_query_http_error", error=str(e), category="http")
            return _error_response(f"HTTP error: {e}")
        except Exception as e:
            logger.exception("ngm_query_unexpected_error", error=str(e))
            return _error_response(f"Unexpected error: {e}")
