"""NGM case data extraction query tool."""

import json
import os
from typing import Any

import httpx
import structlog
from mcp.types import TextContent

from ..api_transport import embedded_api_client_kwargs
from ..request_context import is_local_stdio_transport
from .base import BaseTool, ToolExecutionResult
from .ngm_proxy import (
    execute_ngm_proxy_query,
    get_jawafdehi_api_config,
    rows_to_dicts,
    sql_quote,
)

logger = structlog.get_logger()


def _error_response(message: str) -> ToolExecutionResult:
    return ToolExecutionResult(
        [
            TextContent(
                type="text",
                text=json.dumps(
                    {"success": False, "error": message},
                    ensure_ascii=False,
                ),
            )
        ],
        is_error=True,
    )


class NGMExtractCaseDataTool(BaseTool):
    """Tool for extracting complete NGM judicial case data directly to a markdown file."""

    @property
    def name(self) -> str:
        return "ngm_extract_case_data"

    @property
    def description(self) -> str:
        return """Extract complete judicial case information from Nepal's court system (NGM database) into a Markdown file. This tool is only available through a local stdio MCP server because it writes to the local filesystem.
This includes case metadata, hearings, and entities (plaintiffs/defendants).

Court IDs (court_identifier):
- Supreme & Special: supreme, special
- High Courts: biratnagarhc, illamhc, dhankutahc, okhaldhungahc, janakpurhc, rajbirajhc, birganjhc, patanhc, hetaudahc, pokharahc, baglunghc, tulsipurhc, butwalhc, nepalgunjhc, surkhethc, jumlahc, dipayalhc, mahendranagarhc
- District Courts: achhamdc, argakhanchidc, etc."""

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "court_identifier": {
                    "type": "string",
                    "description": "Identifier for the court (e.g. supreme, patanhc, special)",
                },
                "case_number": {
                    "type": "string",
                    "description": "The exact case number to extract data for",
                },
                "file_path": {
                    "type": "string",
                    "description": (
                        "Absolute path to save the generated Markdown document. "
                        "Only supported by local stdio servers."
                    ),
                },
            },
            "required": ["court_identifier", "case_number", "file_path"],
        }

    def _validate_environment(self) -> tuple[str, str]:
        return get_jawafdehi_api_config()

    @staticmethod
    def _sql_quote(value: str) -> str:
        return sql_quote(value)

    @staticmethod
    def _rows_to_dicts(payload: dict[str, Any]) -> list[dict[str, Any]]:
        return rows_to_dicts(payload)

    async def _execute_proxy_query(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        token: str,
        query: str,
    ) -> dict[str, Any]:
        return await execute_ngm_proxy_query(
            client,
            base_url,
            token,
            query,
            timeout=15,
        )

    def _format_markdown(
        self, court_info: dict, case_info: dict, hearings: list, entities: list
    ) -> str:
        """Format the extracted data into a Markdown string."""
        md = []

        # Header
        court_name_en = (
            court_info.get("full_name_english", "Unknown Court")
            if court_info
            else "Unknown Court"
        )
        court_name_np = court_info.get("full_name_nepali", "") if court_info else ""
        case_no = (
            case_info.get("case_number", "Unknown Case")
            if case_info
            else "Unknown Case"
        )

        md.append(f"# Case Extract: {case_no}")
        md.append(f"**Court:** {court_name_en} ({court_name_np})")

        if not case_info:
            md.append(
                "\n*Could not find metadata for this exact case number and court.*"
            )
            return "\n".join(md)

        md.append("\n## Case Information")
        # Format case metadata as a table
        md.append("| Property | Value |")
        md.append("|---|---|")

        # Mapping to readable labels
        props = [
            ("Case Type", "case_type"),
            ("Status", "case_status"),
            ("Registration Date (AD)", "registration_date_ad"),
            ("Registration Date (BS)", "registration_date_bs"),
            ("Division", "division"),
            ("Category", "category"),
            ("Section", "section"),
            ("Priority", "priority"),
            ("Original Case Number", "original_case_number"),
            ("Verdict Date (AD)", "verdict_date_ad"),
            ("Verdict Date (BS)", "verdict_date_bs"),
            ("Verdict Judge", "verdict_judge"),
        ]

        def default_serializer(obj: Any) -> str:
            return str(obj)

        for label, key in props:
            val = case_info.get(key)
            if val is not None and val != "":
                md.append(f"| **{label}** | {val} |")

        # Entities
        if entities:
            md.append("\n## Entities Involved")

            # Group entities by side side
            plaintiffs = [
                e for e in entities if str(e.get("side")).lower() == "plaintiff"
            ]
            defendants = [
                e for e in entities if str(e.get("side")).lower() == "defendant"
            ]
            others = [
                e
                for e in entities
                if str(e.get("side")).lower() not in ["plaintiff", "defendant"]
            ]

            if plaintiffs:
                md.append("\n### Plaintiffs")
                for e in plaintiffs:
                    nes = f" (NES ID: {e.get('nes_id')})" if e.get("nes_id") else ""
                    addr = f" - {e.get('address')}" if e.get("address") else ""
                    md.append(f"- **{e.get('name', 'Unknown')}**{addr}{nes}")

            if defendants:
                md.append("\n### Defendants")
                for e in defendants:
                    nes = f" (NES ID: {e.get('nes_id')})" if e.get("nes_id") else ""
                    addr = f" - {e.get('address')}" if e.get("address") else ""
                    md.append(f"- **{e.get('name', 'Unknown')}**{addr}{nes}")

            if others:
                md.append("\n### Other Entities")
                for e in others:
                    nes = f" (NES ID: {e.get('nes_id')})" if e.get("nes_id") else ""
                    side = f"[{e.get('side')}] " if e.get("side") else ""
                    addr = f" - {e.get('address')}" if e.get("address") else ""
                    md.append(f"- {side}**{e.get('name', 'Unknown')}**{addr}{nes}")

        # Hearings
        if hearings:
            md.append("\n## Hearing History")
            for h in sorted(hearings, key=lambda x: str(x.get("hearing_date_ad", ""))):
                date_ad = h.get("hearing_date_ad")
                date_bs = h.get("hearing_date_bs", "Unknown Date")
                date_str = f"{date_bs} ({date_ad})" if date_ad else f"{date_bs}"

                md.append(f"\n### {date_str} - {h.get('decision_type', 'Hearing')}")

                judge = h.get("judge_names", h.get("bench", "Unknown Bench"))
                md.append(f"- **Judges / Bench:** {judge}")

                if h.get("bench_type"):
                    md.append(f"- **Bench Type:** {h.get('bench_type')}")

                if h.get("case_status"):
                    md.append(f"- **Case Status:** {h.get('case_status')}")

                if h.get("lawyer_names"):
                    md.append(f"- **Lawyers:** {h.get('lawyer_names')}")

                if h.get("remarks"):
                    md.append(f"\n> **Remarks:** {h.get('remarks')}")

        # Appendix: raw data for integrity
        md.append("\n---")
        md.append("\n## Appendix: Raw Data")
        md.append(
            "*This section contains unformatted raw database records for reference and data integrity.*"
        )

        if case_info:
            md.append("\n### Full Case Record")
            md.append("```json")
            md.append(
                json.dumps(
                    case_info,
                    indent=2,
                    ensure_ascii=False,
                    default=default_serializer,
                )
            )
            md.append("```")

        if hearings:
            md.append("\n### Hearing Records")
            sorted_hearings = sorted(
                hearings, key=lambda x: str(x.get("hearing_date_ad", ""))
            )
            for i, h in enumerate(sorted_hearings, start=1):
                date_ad = h.get("hearing_date_ad")
                date_bs = h.get("hearing_date_bs", "Unknown Date")
                date_str = f"{date_bs} ({date_ad})" if date_ad else f"{date_bs}"
                decision = h.get("decision_type", "Hearing")
                md.append(f"\n#### Hearing {i}: {date_str} \u2014 {decision}")
                md.append("```json")
                md.append(
                    json.dumps(
                        h,
                        indent=2,
                        ensure_ascii=False,
                        default=default_serializer,
                    )
                )
                md.append("```")

        return "\n".join(md)

    async def execute(self, arguments: dict[str, Any]) -> list[TextContent]:
        if not is_local_stdio_transport():
            return _error_response(
                "file_path is only supported by local stdio MCP servers; "
                "HTTP-hosted MCP servers cannot write server filesystem paths"
            )

        court_identifier = arguments.get("court_identifier")
        case_number = arguments.get("case_number")
        file_path = arguments.get("file_path")

        if not all([court_identifier, case_number, file_path]):
            return _error_response(
                "court_identifier, case_number, and file_path parameters are required"
            )

        # Ensure path is absolute
        if not os.path.isabs(file_path):
            file_path = os.path.abspath(file_path)

        try:
            base_url, token = self._validate_environment()
            court_id_sql = self._sql_quote(court_identifier)
            case_no_sql = self._sql_quote(case_number)

            async with httpx.AsyncClient(**embedded_api_client_kwargs()) as client:
                court_payload = await self._execute_proxy_query(
                    client,
                    base_url,
                    token,
                    (
                        "SELECT * FROM courts "
                        f"WHERE identifier = '{court_id_sql}' "
                        "LIMIT 1"
                    ),
                )
                court_rows = self._rows_to_dicts(court_payload)
                court_info = court_rows[0] if court_rows else {}

                case_payload = await self._execute_proxy_query(
                    client,
                    base_url,
                    token,
                    (
                        "SELECT * FROM court_cases "
                        f"WHERE court_identifier = '{court_id_sql}' "
                        f"AND case_number = '{case_no_sql}' "
                        "LIMIT 1"
                    ),
                )
                case_rows = self._rows_to_dicts(case_payload)
                case_info = case_rows[0] if case_rows else {}

                entities_payload = await self._execute_proxy_query(
                    client,
                    base_url,
                    token,
                    (
                        "SELECT * FROM court_case_entities "
                        f"WHERE court_identifier = '{court_id_sql}' "
                        f"AND case_number = '{case_no_sql}'"
                    ),
                )
                entities = self._rows_to_dicts(entities_payload)

                hearings_payload = await self._execute_proxy_query(
                    client,
                    base_url,
                    token,
                    (
                        "SELECT * FROM court_case_hearings "
                        f"WHERE court_identifier = '{court_id_sql}' "
                        f"AND case_number = '{case_no_sql}'"
                    ),
                )
                hearings = self._rows_to_dicts(hearings_payload)

            # 5. Format to Markdown
            markdown_content = self._format_markdown(
                court_info, case_info, hearings, entities
            )

            # 6. Write to File

            # Ensure directory exists
            os.makedirs(os.path.dirname(file_path), exist_ok=True)

            with open(file_path, "w", encoding="utf-8") as f:
                f.write(markdown_content)

            response = {
                "success": True,
                "message": f"Case data successfully extracted to {file_path}",
                "file_path": file_path,
                "stats": {
                    "case_found": bool(case_info),
                    "hearings_count": len(hearings),
                    "entities_count": len(entities),
                },
            }

            return [TextContent(type="text", text=json.dumps(response))]

        except httpx.HTTPError as e:
            logger.error("ngm_extract_http_error", error=str(e), category="http")
            return _error_response(f"HTTP error: {e}")
        except RuntimeError as e:
            logger.error("ngm_extract_proxy_error", error=str(e), category="proxy")
            return _error_response(f"Proxy error: {e}")
        except Exception as e:
            logger.exception("ngm_extract_unexpected_error", error=str(e))
            return _error_response(f"Unexpected error: {e}")
