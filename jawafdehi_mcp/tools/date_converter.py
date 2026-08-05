import datetime
from typing import Any

import nepali_datetime
import structlog
from mcp.types import TextContent

from .base import BaseTool, error_text

logger = structlog.get_logger()


class DateConverterTool(BaseTool):
    """Tool for converting dates between AD (Gregorian) and BS (Bikram Sambat).

    This tool converts dates between AD and BS, working in the conversion in
    Asia/Kathmandu time zone. The conversion requires precise calculations
    which the tool will provide securely.
    """

    @property
    def name(self) -> str:
        return "convert_date"

    @property
    def description(self) -> str:
        return (
            "Convert dates between AD (Gregorian) and BS (Bikram Sambat). "
            "Works in the Asia/Kathmandu time zone. Often times, LLMs convert date wrongly and this tool will "
            "provide the correct conversion."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "dates": {
                    "type": "array",
                    "items": {
                        "type": "string",
                    },
                    "minItems": 1,
                    "maxItems": 100,
                    "description": "A list of dates to convert in YYYY-MM-DD format (e.g. ['2023-01-15', '2079-10-01']).",
                },
                "mode": {
                    "type": "string",
                    "enum": ["ad_to_bs", "bs_to_ad"],
                    "description": "The direction of the conversion ('ad_to_bs' or 'bs_to_ad').",
                },
            },
            "required": ["dates", "mode"],
        }

    async def execute(self, arguments: dict[str, Any]) -> list[TextContent]:
        dates = arguments.get("dates")
        mode = arguments.get("mode")

        if not dates or not isinstance(dates, list) or not mode:
            return error_text(
                "Error: 'dates' (non-empty list) and 'mode' are required parameters."
            )

        if len(dates) > 100:
            return error_text(
                "Error: at most 100 dates may be converted per call."
            )

        if mode not in ["ad_to_bs", "bs_to_ad"]:
            return error_text(
                "Error: mode must be 'ad_to_bs' or 'bs_to_ad'."
            )

        results = []
        success_count = 0
        for date_str in dates:
            try:
                year, month, day = map(int, date_str.split("-"))
            except ValueError:
                results.append(
                    f"{date_str}: Error - date must be in YYYY-MM-DD format."
                )
                continue

            try:
                if mode == "ad_to_bs":
                    ad_date = datetime.date(year, month, day)
                    bs_date = nepali_datetime.date.from_datetime_date(ad_date)
                    result = bs_date.strftime("%Y-%m-%d")
                    results.append(
                        f"Converted AD {date_str} to BS: {result} (Asia/Kathmandu Timezone Context)"
                    )
                    success_count += 1

                elif mode == "bs_to_ad":
                    bs_date = nepali_datetime.date(year, month, day)
                    ad_date = bs_date.to_datetime_date()
                    result = ad_date.strftime("%Y-%m-%d")
                    results.append(
                        f"Converted BS {date_str} to AD: {result} (Asia/Kathmandu Timezone Context)"
                    )
                    success_count += 1
            except Exception as e:  # noqa: BLE001 — one bad date is reported, the batch continues
                logger.error(
                    "date_conversion_failed",
                    date_str=date_str,
                    mode=mode,
                    error=str(e),
                )
                results.append(f"{date_str}: Error performing conversion: {str(e)}")

        text = "\n".join(results)
        if success_count == 0:
            return error_text(text)
        return [TextContent(type="text", text=text)]
