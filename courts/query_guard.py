"""Hardening for the gated raw-SQL ``/query`` endpoint.

The policy is intentionally narrow: one parsed PostgreSQL ``SELECT``, a fixed
table allowlist, and a fixed side-effect-free function allowlist. Execution
adds a row cap, response-size cap, read-only transaction, and statement timeout.
"""

from __future__ import annotations

import os
import re

from sqlglot import exp, parse, parse_one
from sqlglot.errors import ParseError

# Tables the raw-SQL surface may read. Mirrors the FastAPI allowlist; the
# document index and firms are intentionally NOT here yet. ``scraped_dates`` is
# explicitly blocked below.
ALLOWED_TABLES = {
    "courts",
    "court_cases",
    "court_case_hearings",
    "court_case_entities",
}

# Scope-only query principals (notably the anonymous MCP's least-privilege
# service account) query these fixed public projections. Base table names stay
# stable for callers, but tombstones, orphaned child rows, and internal columns
# never enter the outer user-controlled SELECT.
_PUBLIC_PROJECTION_SQL = {
    "courts": """
        SELECT identifier, court_type, full_name_nepali, full_name_english
        FROM courts
    """,
    "court_cases": """
        SELECT
            case_number,
            court_identifier,
            registration_date_bs,
            registration_date_ad,
            case_type,
            case_status,
            plaintiff,
            defendant,
            nes_id,
            document_sources
        FROM court_cases
        WHERE NOT is_deleted
    """,
    "court_case_hearings": """
        SELECT
            h.id,
            h.case_number,
            h.court_identifier,
            h.hearing_date_bs,
            h.hearing_date_ad,
            h.bench,
            h.bench_type,
            h.judge_names,
            h.lawyer_names,
            h.serial_no,
            h.case_status,
            h.decision_type,
            h.remarks,
            h.scraped_at,
            h.extra_data
        FROM court_case_hearings AS h
        WHERE EXISTS (
            SELECT 1
            FROM court_cases AS c
            WHERE c.case_number = h.case_number
              AND c.court_identifier = h.court_identifier
              AND NOT c.is_deleted
        )
    """,
    "court_case_entities": """
        SELECT
            e.id,
            e.case_number,
            e.court_identifier,
            e.side,
            e.name,
            e.address,
            e.nes_id
        FROM court_case_entities AS e
        WHERE EXISTS (
            SELECT 1
            FROM court_cases AS c
            WHERE c.case_number = e.case_number
              AND c.court_identifier = e.court_identifier
              AND NOT c.is_deleted
        )
    """,
}
_PUBLIC_PROJECTIONS = {
    name: parse_one(sql, read="postgres")
    for name, sql in _PUBLIC_PROJECTION_SQL.items()
}

FORBIDDEN_FUNCTIONS = {
    "pg_sleep",
    "pg_sleep_for",
    "pg_sleep_until",
    "pg_read_file",
    "pg_read_binary_file",
    "pg_ls_dir",
    "pg_ls_logdir",
    "pg_ls_waldir",
    "pg_stat_file",
    "lo_import",
    "lo_export",
    "lo_create",
    "lo_unlink",
    "lo_from_bytea",
    "lo_put",
    "lo_get",
    "dblink",
    "dblink_exec",
    "query_to_xml",
    "query_to_xmlschema",
    "query_to_xml_and_xmlschema",
    "table_to_xml",
    "table_to_xmlschema",
    "table_to_xml_and_xmlschema",
    "schema_to_xml",
    "schema_to_xmlschema",
    "schema_to_xml_and_xmlschema",
    "database_to_xml",
    "database_to_xmlschema",
    "database_to_xml_and_xmlschema",
    "cursor_to_xml",
    "cursor_to_xmlschema",
    "setval",
    "nextval",
    "set_config",
    "pg_notify",
    "pg_advisory_lock",
    "pg_advisory_lock_shared",
    "pg_advisory_xact_lock",
    "pg_advisory_xact_lock_shared",
    "pg_try_advisory_lock",
    "pg_try_advisory_lock_shared",
    "pg_try_advisory_xact_lock",
    "pg_try_advisory_xact_lock_shared",
    "pg_advisory_unlock",
    "pg_advisory_unlock_shared",
    "pg_advisory_unlock_all",
    "pg_cancel_backend",
    "pg_terminate_backend",
    "pg_reload_conf",
    "pg_rotate_logfile",
    "pg_create_restore_point",
    "pg_switch_wal",
    "pg_logical_emit_message",
    "pg_promote",
    "pg_wal_replay_pause",
    "pg_wal_replay_resume",
}

# SQLGlot normalizes some PostgreSQL spellings to semantic names, for example
# DATE_TRUNC -> TIMESTAMP_TRUNC. Keep this list deliberately small: an unknown
# function is rejected rather than assumed harmless.
ALLOWED_FUNCTIONS = {
    "abs",
    "avg",
    "bool_and",
    "bool_or",
    "cast",
    "ceil",
    "ceiling",
    "char_length",
    "coalesce",
    "concat",
    "concat_ws",
    "count",
    "current_date",
    "date_trunc",
    "dense_rank",
    "extract",
    "floor",
    "greatest",
    "least",
    "length",
    "lower",
    "ltrim",
    "max",
    "min",
    "nullif",
    "rank",
    "replace",
    "round",
    "row_number",
    "rtrim",
    "substring",
    "sum",
    "timestamp_trunc",
    "to_char",
    "trim",
    "upper",
}

# Internal scrape-state table — never exposed via the query endpoint.
BLOCKED_TABLES = {"scraped_dates"}

MAX_QUERY_CHARS = 20_000


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


def default_max_response_bytes() -> int:
    """Maximum serialized result size, bounded to 10 MiB."""
    try:
        value = int(os.getenv("NGM_QUERY_MAX_RESPONSE_BYTES", "1000000"))
    except ValueError:
        return 1_000_000
    if value <= 0:
        return 1_000_000
    return min(value, 10_000_000)


def _function_name(function: exp.Func) -> str:
    if isinstance(function, exp.Anonymous):
        return str(function.name).lower()
    return function.sql_name().lower()


def _and_terms(expression: exp.Expression):
    """Yield top-level AND terms without treating OR branches as constraints."""
    expression = expression.unnest()
    if isinstance(expression, exp.And):
        yield from _and_terms(expression.this)
        yield from _and_terms(expression.expression)
    else:
        yield expression


def _join_is_constrained(join: exp.Join) -> bool:
    """Require a join key that directly constrains the newly joined relation."""
    if str(join.args.get("kind") or "").upper() == "CROSS":
        return False
    if str(join.args.get("method") or "").upper() == "NATURAL":
        return False
    if join.args.get("using"):
        return True

    on_clause = join.args.get("on")
    right_alias = str(join.this.alias_or_name or "").lower()
    if on_clause is None or not right_alias:
        return False

    for term in _and_terms(on_clause):
        term = term.unnest()
        if not isinstance(term, exp.EQ):
            continue
        left = term.this.unnest()
        right = term.expression.unnest()
        if not isinstance(left, exp.Column) or not isinstance(right, exp.Column):
            continue
        left_table = str(left.table or "").lower()
        right_table = str(right.table or "").lower()
        if not left_table or not right_table or left_table == right_table:
            continue
        if right_alias in {left_table, right_table}:
            return True
    return False


def validate_query(query: str) -> tuple[bool, str | None]:
    """Validate one parsed PostgreSQL SELECT against strict allowlists."""
    if not isinstance(query, str) or not query.strip():
        return False, "Query cannot be empty"
    if len(query) > MAX_QUERY_CHARS:
        return False, f"Query exceeds the {MAX_QUERY_CHARS}-character limit"
    if _has_sql_comment(query):
        return False, "SQL comments are not allowed"
    # PostgreSQL Unicode-escaped identifiers are not needed by this API and are
    # parsed inconsistently across generic SQL parsers. Reject the syntax before
    # parsing so U&"pg\005fsleep" can never disguise a function or table name.
    if re.search(r'(?i)\bu\s*&\s*"', query) or re.search(
        r"(?i)\buescape\b", query
    ):
        return False, "Unicode-escaped SQL identifiers are not allowed"

    try:
        statements = parse(query, read="postgres")
    except ParseError:
        return False, "Query is not valid PostgreSQL SELECT syntax"
    if len(statements) != 1:
        return False, "Multiple SQL statements are not allowed"

    statement = statements[0]
    if not isinstance(statement, exp.Select) or statement.find(exp.With):
        return False, "Only SELECT queries are allowed"
    if statement.find(exp.Into) or statement.find(exp.Lock):
        return False, "SELECT INTO and row-locking clauses are not allowed"

    referenced: set[str] = set()
    for table in statement.find_all(exp.Table):
        name = table.name.lower()
        if table.catalog or table.db:
            return False, "Schema-qualified table names are not allowed"
        referenced.add(name)

    blocked = referenced.intersection(BLOCKED_TABLES)
    if blocked:
        return False, f"Access to '{', '.join(sorted(blocked))}' table is not allowed"

    invalid_tables = referenced.difference(ALLOWED_TABLES)
    if invalid_tables:
        return (
            False,
            f"Invalid table(s): {', '.join(sorted(invalid_tables))}. "
            f"Allowed tables: {', '.join(sorted(ALLOWED_TABLES))}",
        )

    for join in statement.find_all(exp.Join):
        if not _join_is_constrained(join):
            return (
                False,
                "JOINs must use USING or a qualified column equality that "
                "constrains the joined relation",
            )

    for function in statement.find_all(exp.Func):
        name = _function_name(function)
        if name in FORBIDDEN_FUNCTIONS:
            return False, f"Forbidden function detected: {name}"
        if name not in ALLOWED_FUNCTIONS:
            return False, f"Function is not allowed: {name}"

    return True, None


def apply_row_cap(query: str, max_rows: int) -> str:
    """Wrap the query as a subquery with a hard ``LIMIT`` row cap."""
    cleaned = query.strip().rstrip(";")
    return f"SELECT * FROM ({cleaned}) AS ngm_result LIMIT {max(1, int(max_rows))}"


def apply_public_projection(query: str) -> str:
    """Replace allowed base tables with fixed public row/column projections."""
    statement = parse_one(query, read="postgres")
    for table in list(statement.find_all(exp.Table)):
        table_name = table.name.lower()
        projection = _PUBLIC_PROJECTIONS.get(table_name)
        if projection is None:
            # Validation runs first, so reaching this branch means the call order
            # is wrong rather than that caller-controlled SQL should be tolerated.
            raise ValueError(f"No public projection exists for table: {table_name}")
        alias = table.alias_or_name
        table.replace(
            exp.Subquery(
                this=projection.copy(),
                alias=exp.TableAlias(this=exp.to_identifier(alias)),
            )
        )
    return statement.sql(dialect="postgres")
