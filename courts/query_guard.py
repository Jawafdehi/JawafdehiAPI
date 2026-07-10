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
    # DDL/DML that can appear inside a CTE or a stacked statement.
    "merge",
    "copy",
    "call",
    "do",
    "vacuum",
    "reindex",
]

# Volatile / resource-abuse functions blocked to stop a read-only SELECT from
# being turned into a DoS (or reading server state). ``pg_sleep`` is the classic
# time-based DoS; the ``pg_read_*`` / ``lo_*`` family can read files/large objects.
FORBIDDEN_FUNCTIONS = [
    "pg_sleep",
    "pg_read_file",
    "pg_read_binary_file",
    "pg_ls_dir",
    "pg_stat_file",
    "lo_import",
    "lo_export",
    "dblink",
    "query_to_xml",
]

# Internal scrape-state table — never exposed via the query endpoint.
BLOCKED_TABLES = {"scraped_dates"}

# Schema prefixes that must never be reachable: Postgres system catalogs (hold
# password hashes in ``pg_authid``, etc.) and the SQL information schema.
BLOCKED_SCHEMA_PREFIXES = ("pg_", "information_schema")

# Clause keywords that terminate a FROM/JOIN table list (so we can carve out the
# table references between ``from``/``join`` and the next clause).
_CLAUSE_BOUNDARY = (
    r"where|group|order|limit|having|on|using|union|intersect|except|"
    r"window|offset|fetch|for|join|left|right|inner|outer|cross|full|natural"
)


def _has_sql_comment(sql: str) -> bool:
    """True if the SQL contains a line (``--``) or block (``/* */``) comment.

    This locked-down allowlist surface has no legitimate use for comments, and
    they are a classic evasion vector (hiding a forbidden keyword/table, or a
    stacked statement, from a string-level denylist). Rather than strip-and-hope,
    we reject any query that contains one — the most conservative posture.
    """
    return bool(re.search(r"--|/\*|\*/", sql))


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


def _extract_table_refs(normalized: str) -> set[str]:
    """Return every table identifier referenced in a FROM/JOIN list.

    Handles what the old single-identifier-after-FROM regex missed: comma-joins
    (``FROM a, b, c``), schema-qualified names (``pg_catalog.pg_authid``), and
    double-quoted identifiers (``"scraped_dates"``). We grab the text of each
    FROM/JOIN clause up to the next SQL clause boundary, then pull every
    identifier (optionally schema-qualified / quoted) out of that comma list.
    Both the bare table name AND any schema qualifier are returned, so the
    caller can block on either.
    """
    refs: set[str] = set()
    # Each FROM/JOIN introduces a comma-separated table list that runs until the
    # next clause keyword (WHERE/ON/GROUP/…) or the end of the (sub)query.
    clause_re = re.compile(
        rf"\b(?:from|join)\s+(.*?)(?=\b(?:{_CLAUSE_BOUNDARY})\b|\)|$)",
        flags=re.DOTALL,
    )
    # An identifier is a quoted or unquoted name, optionally schema-qualified.
    ident_re = re.compile(
        r'(?:"([^"]+)"|([a-z_][a-z0-9_$]*))'
        r'(?:\s*\.\s*(?:"([^"]+)"|([a-z_][a-z0-9_$]*)))?'
    )
    for clause in clause_re.findall(normalized):
        # Split the clause on commas so each table in a comma-join is seen; take
        # only the first identifier of each item (drop any alias after it).
        for item in clause.split(","):
            m = ident_re.search(item.strip())
            if not m:
                continue
            left = m.group(1) or m.group(2)      # schema OR bare table
            right = m.group(3) or m.group(4)     # table when schema-qualified
            if right:  # schema.table → record BOTH the schema and the table
                refs.add(left)
                refs.add(right)
            elif left:
                refs.add(left)
    return refs


def validate_query(query: str) -> tuple[bool, str | None]:
    """Validate user SQL against read-only and allowlist constraints.

    Returns ``(ok, error_message)``. Enforces, in order: comment stripping,
    single-statement, SELECT-only, forbidden-keyword denylist, forbidden-function
    denylist (DoS/file-read), blocked system schemas, ``scraped_dates`` blocked,
    and the table allowlist over EVERY FROM/JOIN reference (comma-joins + quoted +
    schema-qualified included).
    """
    # Reject comments outright — they have no legit use on this allowlist surface
    # and are a classic way to smuggle a forbidden keyword/table/statement past a
    # string-level denylist.
    if _has_sql_comment(query):
        return False, "SQL comments are not allowed"

    normalized = query.strip().lower().rstrip(";").strip()

    if not normalized:
        return False, "Query cannot be empty"

    # Reject multiple statements: after dropping a single trailing ``;``, any
    # remaining ``;`` means a stacked statement (``SELECT 1; SELECT pg_sleep(5)``
    # or ``SELECT 1; DROP …``). Only a single statement is ever allowed.
    if ";" in normalized:
        return False, "Multiple SQL statements are not allowed"

    if not normalized.startswith("select"):
        return False, "Only SELECT queries are allowed"

    for keyword in FORBIDDEN_KEYWORDS:
        if re.search(rf"\b{keyword}\b", normalized):
            return False, f"Forbidden keyword detected: {keyword.upper()}"

    for func in FORBIDDEN_FUNCTIONS:
        if re.search(rf"\b{func}\b", normalized):
            return False, f"Forbidden function detected: {func}"

    referenced = _extract_table_refs(normalized)

    # System catalogs / information_schema (by schema OR table prefix) are never
    # reachable — this is what stops ``FROM courts, pg_catalog.pg_authid``.
    for ref in referenced:
        if ref.startswith(BLOCKED_SCHEMA_PREFIXES):
            return False, f"Access to system schema/table '{ref}' is not allowed"

    blocked = referenced & BLOCKED_TABLES
    if blocked:
        return False, f"Access to '{', '.join(sorted(blocked))}' table is not allowed"

    invalid_tables = referenced - ALLOWED_TABLES
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
