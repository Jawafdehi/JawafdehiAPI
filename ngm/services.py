import logging
import re
import time

from django.conf import settings
from django.db import connections
from django.db.utils import DatabaseError

logger = logging.getLogger(__name__)

# Devanagari digit to ASCII digit mapping
DEVANAGARI_TO_ASCII = {
    "०": "0",
    "१": "1",
    "२": "2",
    "३": "3",
    "४": "4",
    "५": "5",
    "६": "6",
    "७": "7",
    "८": "8",
    "९": "9",
}

ALLOWED_TABLES = {
    "courts",
    "court_cases",
    "court_case_hearings",
    "court_case_entities",
}

FORBIDDEN_KEYWORDS = [
    "insert",
    "update",
    "delete",
    "drop",
    "create",
    "alter",
    "truncate",
    "grant",
    "revoke",
]


def normalize_case_number(case_number: str) -> str:
    """
    Normalize a court case number to standard format: XXX-YY-XXXX

    Accepts various formats:
    - 081-CR-0081 (already normalized)
    - 081-cr-0081 (lowercase)
    - 81-cr-0081 (missing leading zero)
    - ०८१-CR-००८१ (Devanagari numerals)
    - 81-CR-81 (missing leading zeros)

    Returns normalized format: 081-CR-0081 (uppercase, zero-padded)

    Args:
        case_number: The case number to normalize

    Returns:
        Normalized case number in format XXX-YY-XXXX

    Raises:
        ValueError: If case number format is invalid
    """
    if not case_number:
        raise ValueError("Case number cannot be empty")

    # Convert Devanagari digits to ASCII
    normalized = case_number
    for devanagari, ascii_digit in DEVANAGARI_TO_ASCII.items():
        normalized = normalized.replace(devanagari, ascii_digit)

    # Convert to uppercase
    normalized = normalized.upper()

    # Match pattern: digits-letters-digits
    # Allow flexible digit counts (will be padded later)
    pattern = r"^(\d+)-([A-Z]+)-(\d+)$"
    match = re.match(pattern, normalized)

    if not match:
        raise ValueError(
            f"Invalid case number format: {case_number}. "
            "Expected format: XXX-YY-XXXX (e.g., 081-CR-0081)"
        )

    first_part, middle_part, last_part = match.groups()

    # Pad first part to 3 digits
    first_part = first_part.zfill(3)

    # Pad last part to 4 digits
    last_part = last_part.zfill(4)

    return f"{first_part}-{middle_part}-{last_part}"


def validate_query(query: str) -> tuple[bool, str | None]:
    """Validate user SQL against read-only and allowlist constraints."""
    normalized = query.strip().lower().rstrip(";")

    if not normalized:
        return False, "Query cannot be empty"

    if not normalized.startswith("select"):
        return False, "Only SELECT queries are allowed"

    for keyword in FORBIDDEN_KEYWORDS:
        if re.search(rf"\b{keyword}\b", normalized):
            return False, f"Forbidden keyword detected: {keyword.upper()}"

    table_pattern = r"\b(?:from|join)\s+([a-z_][a-z0-9_]*(?:\.[a-z_][a-z0-9_]*)?)"
    referenced_tables = {
        table_name.split(".")[-1]
        for table_name in re.findall(table_pattern, normalized)
    }

    if "scraped_dates" in referenced_tables:
        return False, "Access to 'scraped_dates' table is not allowed"

    invalid_tables = referenced_tables - ALLOWED_TABLES
    if invalid_tables:
        return (
            False,
            f"Invalid table(s): {', '.join(sorted(invalid_tables))}. "
            f"Allowed tables: {', '.join(sorted(ALLOWED_TABLES))}",
        )

    return True, None


def apply_row_cap(query: str, max_rows: int) -> str:
    """Apply a hard row cap by wrapping the query as a subquery."""
    cleaned = query.strip().rstrip(";")
    return f"SELECT * FROM ({cleaned}) AS ngm_result LIMIT {int(max_rows)}"


def ensure_ngm_database_configured() -> None:
    database_config = settings.DATABASES.get("ngm")
    if not database_config:
        raise ValueError("NGM database is not configured")


def execute_select_query(query: str, timeout_seconds: float) -> dict:
    """Execute a validated query against the NGM database alias."""
    ensure_ngm_database_configured()

    timeout_ms = int(timeout_seconds * 1000)
    max_rows = int(getattr(settings, "NGM_QUERY_MAX_ROWS", 500))
    capped_query = apply_row_cap(query, max_rows)

    start_time = time.perf_counter()
    try:
        with connections["ngm"].cursor() as cursor:
            if connections["ngm"].vendor == "postgresql":
                cursor.execute("SET statement_timeout = %s", [timeout_ms])
            cursor.execute(capped_query)
            rows = cursor.fetchall()
            columns = [col[0] for col in (cursor.description or [])]
    except DatabaseError as exc:
        logger.exception("NGM database query failed")
        raise ValueError("Database query failed") from exc

    query_time_ms = int((time.perf_counter() - start_time) * 1000)
    row_data = [list(row) for row in rows]

    return {
        "columns": columns,
        "rows": row_data,
        "row_count": len(row_data),
        "max_rows": max_rows,
        "query_time_ms": query_time_ms,
    }


def get_court_case_details(court_identifier: str, case_number: str) -> dict | None:
    """
    Fetch complete case details including hearings and entities.

    Returns None if case not found, otherwise returns dict with:
    - case: dict of case details
    - hearings: list of hearing dicts
    - entities: list of entity dicts
    """
    ensure_ngm_database_configured()

    try:
        with connections["ngm"].cursor() as cursor:
            # Fetch case details
            cursor.execute(
                """
                SELECT case_number, court_identifier, registration_date_bs, 
                       registration_date_ad, case_type, division, category, section,
                       plaintiff, defendant, original_case_number, case_id, priority,
                       registration_number, case_status, verdict_date_bs, 
                       verdict_date_ad, verdict_judge, status
                FROM court_cases
                WHERE court_identifier = %s AND case_number = %s
                """,
                [court_identifier, case_number],
            )
            case_row = cursor.fetchone()

            if not case_row:
                return None

            case_columns = [col[0] for col in cursor.description]
            case_data = dict(zip(case_columns, case_row))

            # Fetch hearings
            cursor.execute(
                """
                SELECT id, case_number, court_identifier, hearing_date_bs, 
                       hearing_date_ad, bench, bench_type, judge_names, lawyer_names,
                       serial_no, case_status, decision_type, remarks
                FROM court_case_hearings
                WHERE court_identifier = %s AND case_number = %s
                ORDER BY hearing_date_ad DESC NULLS LAST
                """,
                [court_identifier, case_number],
            )
            hearing_rows = cursor.fetchall()
            hearing_columns = [col[0] for col in cursor.description]
            hearings = [dict(zip(hearing_columns, row)) for row in hearing_rows]

            # Fetch entities
            cursor.execute(
                """
                SELECT id, case_number, court_identifier, side, name, address, nes_id
                FROM court_case_entities
                WHERE court_identifier = %s AND case_number = %s
                ORDER BY side, name
                """,
                [court_identifier, case_number],
            )
            entity_rows = cursor.fetchall()
            entity_columns = [col[0] for col in cursor.description]
            entities = [dict(zip(entity_columns, row)) for row in entity_rows]

            return {
                "case": case_data,
                "hearings": hearings,
                "entities": entities,
            }

    except DatabaseError as exc:
        logger.exception("Failed to fetch court case details")
        raise ValueError("Database query failed") from exc
