"""Hardening for the gated raw-SQL ``/query`` endpoint.

Ported verbatim (in spirit) from the FastAPI NGM ``ngm.api.query_guard``:
SELECT-only, forbidden-keyword denylist, ``scraped_dates`` blocked, table
allowlist, hard row cap, statement timeout. Pure functions — no DB or framework
coupling — so they're equally usable against Postgres today or a DuckDB/Trino
dialect later. This module owns *policy* (what is allowed); execution lives in
the view.
"""

from __future__ import annotations

import os
import re

# Tables the raw-SQL surface may read. Mirrors the FastAPI allowlist; the
# document index and firms are intentionally NOT here yet. ``scraped_dates`` is
# explicitly blocked below.
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

# Internal scrape-state table — never exposed via the query endpoint.
BLOCKED_TABLES = {"scraped_dates"}


def default_max_rows() -> int:
    """Hard row cap, overridable via ``NGM_QUERY_MAX_ROWS`` (default 500)."""
    try:
        return int(os.getenv("NGM_QUERY_MAX_ROWS", "500"))
    except ValueError:
        return 500


def default_timeout_seconds() -> float:
    """Statement timeout, overridable via ``NGM_QUERY_TIMEOUT_SECONDS``."""
    try:
        return float(os.getenv("NGM_QUERY_TIMEOUT_SECONDS", "30"))
    except ValueError:
        return 30.0


def validate_query(query: str) -> tuple[bool, str | None]:
    """Validate user SQL against read-only and allowlist constraints.

    Returns ``(ok, error_message)``: SELECT-only, forbidden-keyword denylist,
    ``scraped_dates`` blocked, table allowlist.
    """
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

    blocked = referenced_tables & BLOCKED_TABLES
    if blocked:
        return False, f"Access to '{', '.join(sorted(blocked))}' table is not allowed"

    invalid_tables = referenced_tables - ALLOWED_TABLES
    if invalid_tables:
        return (
            False,
            f"Invalid table(s): {', '.join(sorted(invalid_tables))}. "
            f"Allowed tables: {', '.join(sorted(ALLOWED_TABLES))}",
        )

    return True, None


def apply_row_cap(query: str, max_rows: int) -> str:
    """Wrap the query as a subquery with a hard ``LIMIT`` row cap."""
    cleaned = query.strip().rstrip(";")
    return f"SELECT * FROM ({cleaned}) AS ngm_result LIMIT {int(max_rows)}"
