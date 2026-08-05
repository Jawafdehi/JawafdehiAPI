"""Penetration tests for the gated raw-SQL endpoint (``POST /api/query/``).

Target: :class:`courts.views.QueryView` — a gated, SELECT-only raw-SQL surface
over the ngm DB, guarded by :func:`courts.query_guard.validate_query`
(SELECT-only, forbidden-keyword denylist, ``scraped_dates`` block, table
allowlist, row cap, statement timeout) and gated by
:class:`courts.permissions.HasNgmQueryAccess` (``ngm.query`` scope OR NGM role).

These are adversarial tests: every attack asserts the endpoint REJECTS the
input (400 from the guard, or 401/403 from the auth gate) — NEVER a
200-with-data and NEVER a 500. A legitimate SELECT for an authorized caller is
asserted to be ALLOWED so the guard is not merely blanket-denying.

The test DB is sqlite; the guard is a *string-level* policy check that runs
BEFORE execution, so every rejection test is decided by the guard without
needing Postgres. Tests that would need Postgres-specific execution (e.g. real
``pg_sleep``) assert the guard-level decision instead.

Auth mirrors ``courts/tests/test_api.py``: rather than mint real Zitadel JWTs,
we ``force_authenticate`` a user whose synced Django Groups grant an NGM role,
or attach a token dict carrying the ``ngm.query`` scope.

    DJANGO_SETTINGS_MODULE=config.settings_test TESTING=true \\
        uv run pytest -q courts/tests/test_query_guard_security.py

SECURITY FINDINGS (now FIXED). The adversarial sweep found five real bypasses in
the original guard — all closed in ``courts/query_guard.py`` and locked here as
passing tests:
  * comma cross-join to a blocked/non-allowlisted table
    (``FROM courts, scraped_dates``) — guard now enumerates ALL FROM-list entries,
  * comma cross-join to a system catalog (``FROM courts, pg_catalog.pg_authid``,
    which would read password hashes on Postgres) — system schemas now blocked,
  * quoted-identifier table bypass (``FROM "scraped_dates"``) — quoted names now
    parsed,
  * ``pg_sleep`` (and file-read) DoS — volatile/DoS functions now denylisted,
  * stacked second statement (``SELECT 1; SELECT pg_sleep(5)``) — any statement
    separator now rejected; SQL comments are rejected outright.
"""

from __future__ import annotations

from contextlib import nullcontext
from unittest.mock import MagicMock, patch

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from rest_framework import status
from rest_framework.test import APITestCase

from courts import query_guard, views
from courts.models import CaseEntity, Court, CourtCase

# Every test in this module is part of the adversarial security suite. ``django_db``
# is added so the DB-touching endpoint tests may hit every alias (see the
# ``databases`` attr on the base case below).
pytestmark = [pytest.mark.security, pytest.mark.django_db]

User = get_user_model()

QUERY_URL = "/api/query/"


class _QuerySecurityBase(APITestCase):
    """Shared setup: an NGM-role user, a role-less user, and a courts row.

    ``databases = "__all__"`` enrolls every alias so the router-pinned ``ngm``
    queries issued by the endpoint are permitted in tests (mirrors
    ``courts/tests/test_api.py::_DbAPITestCase``).
    """

    databases = "__all__"

    @classmethod
    def setUpTestData(cls):
        # A real row so an allowed SELECT returns data rather than an empty set.
        Court.objects.create(
            identifier="supreme",
            court_type="supreme",
            full_name_nepali="सर्वोच्च अदालत",
        )
        cls.role_group, _ = Group.objects.get_or_create(name="Caseworker")
        cls.user = User.objects.create(username="oidc-sub-ngm")
        cls.user.groups.add(cls.role_group)
        cls.nobody = User.objects.create(username="oidc-sub-norole")

    def _post(self, query, **extra):
        """POST a query as the NGM-role user; ``extra`` merges into the body."""
        self.client.force_authenticate(user=self.user)
        body = {"query": query, **extra}
        return self.client.post(QUERY_URL, body, format="json")

    def _assert_rejected(self, resp):
        """A rejected query is a clean 400 (or 403) — never 200-with-data, never 500."""
        self.assertIn(
            resp.status_code,
            (status.HTTP_400_BAD_REQUEST, status.HTTP_403_FORBIDDEN),
            msg=f"expected rejection, got {resp.status_code}: {getattr(resp, 'data', None)}",
        )


# ---------------------------------------------------------------------------
# 7. Auth gate — checked BEFORE the SELECT guard.
# ---------------------------------------------------------------------------
class TestAuthGate(_QuerySecurityBase):
    def test_unauthenticated_is_401(self):
        resp = self.client.post(QUERY_URL, {"query": "SELECT 1"}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_bogus_bearer_token_is_401(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer not-a-real-jwt")
        resp = self.client.post(QUERY_URL, {"query": "SELECT 1"}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_authenticated_without_ngm_role_or_scope_is_403(self):
        self.client.force_authenticate(user=self.nobody)
        resp = self.client.post(QUERY_URL, {"query": "SELECT 1"}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_non_ngm_scope_without_role_is_403(self):
        self.client.force_authenticate(
            user=self.nobody, token={"scope": "openid profile"}
        )
        resp = self.client.post(QUERY_URL, {"query": "SELECT 1"}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_auth_gate_precedes_guard_write_still_403_not_400(self):
        # An unauthorized caller sending a WRITE must be stopped by auth (403),
        # never reach — nor be triaged by — the SQL guard.
        self.client.force_authenticate(user=self.nobody)
        resp = self.client.post(
            QUERY_URL, {"query": "DROP TABLE court_cases"}, format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)


# ---------------------------------------------------------------------------
# 1. Non-SELECT statements.
# ---------------------------------------------------------------------------
class TestNonSelectRejected(_QuerySecurityBase):
    WRITES = [
        "INSERT INTO court_cases (case_number) VALUES ('x')",
        "UPDATE court_cases SET case_number = 'x'",
        "DELETE FROM court_cases",
        "DROP TABLE court_cases",
        "TRUNCATE court_cases",
        "ALTER TABLE court_cases ADD COLUMN pwned text",
        "CREATE TABLE pwned (id int)",
        "GRANT ALL ON court_cases TO public",
        "REVOKE ALL ON court_cases FROM public",
    ]

    def test_each_non_select_is_rejected(self):
        for sql in self.WRITES:
            with self.subTest(sql=sql):
                self._assert_rejected(self._post(sql))

    def test_guard_rejects_each_non_select(self):
        for sql in self.WRITES:
            with self.subTest(sql=sql):
                ok, _ = query_guard.validate_query(sql)
                self.assertFalse(ok, msg=f"guard accepted non-SELECT: {sql!r}")


# ---------------------------------------------------------------------------
# 2. Multi-statement / stacked queries.
# ---------------------------------------------------------------------------
class TestStackedQueries(_QuerySecurityBase):
    def test_stacked_drop_is_rejected(self):
        # Caught by the forbidden-keyword denylist (DROP), even though it trails
        # a leading SELECT.
        self._assert_rejected(self._post("SELECT 1; DROP TABLE court_cases;"))

    def test_stacked_delete_is_rejected(self):
        self._assert_rejected(self._post("SELECT 1; DELETE FROM court_cases;"))

    def test_stacked_update_is_rejected(self):
        self._assert_rejected(
            self._post("SELECT 1; UPDATE court_cases SET case_number = 'x';")
        )

    # FIXED (was a real bypass; guard now blocks this) — locked as a passing test.
    def test_stacked_second_select_is_rejected_by_guard(self):
        ok, _ = query_guard.validate_query("SELECT 1; SELECT pg_sleep(5)")
        self.assertFalse(ok, msg="guard accepted a stacked second statement")


# ---------------------------------------------------------------------------
# 3. CTE-wrapped writes.
# ---------------------------------------------------------------------------
class TestCteWrappedWrites(_QuerySecurityBase):
    CTE_WRITES = [
        "WITH x AS (DELETE FROM court_cases RETURNING *) SELECT * FROM x",
        "WITH x AS (INSERT INTO court_cases (case_number) VALUES ('x') RETURNING *) SELECT * FROM x",
        "WITH x AS (UPDATE court_cases SET case_number='x' RETURNING *) SELECT * FROM x",
    ]

    def test_each_cte_write_is_rejected(self):
        for sql in self.CTE_WRITES:
            with self.subTest(sql=sql):
                # Rejected twice over: does not start with SELECT (starts with
                # WITH) AND carries a forbidden keyword.
                self._assert_rejected(self._post(sql))

    def test_guard_rejects_each_cte_write(self):
        for sql in self.CTE_WRITES:
            with self.subTest(sql=sql):
                ok, _ = query_guard.validate_query(sql)
                self.assertFalse(ok, msg=f"guard accepted CTE write: {sql!r}")


# ---------------------------------------------------------------------------
# 4. Comment-smuggling.
# ---------------------------------------------------------------------------
class TestCommentSmuggling(_QuerySecurityBase):
    def test_line_comment_hiding_drop_is_rejected(self):
        # The keyword denylist scans the whole string (comments are not stripped),
        # so a DROP hidden after a ``--`` comment is still caught.
        self._assert_rejected(
            self._post("SELECT 1 -- harmless\n; DROP TABLE court_cases")
        )

    def test_block_comment_hiding_delete_is_rejected(self):
        self._assert_rejected(
            self._post("SELECT 1 /* ; DELETE FROM court_cases */ FROM courts")
        )

    # FIXED (was a real bypass; guard now blocks this) — locked as a passing test.
    def test_comment_obscured_comma_table_is_rejected_by_guard(self):
        ok, _ = query_guard.validate_query(
            "SELECT * FROM courts /*x*/ , /*y*/ scraped_dates"
        )
        self.assertFalse(ok, msg="guard accepted a comment-obscured comma join")


# ---------------------------------------------------------------------------
# 5. DoS — pg_sleep and the timeout_seconds bound.
# ---------------------------------------------------------------------------
class TestDoS(_QuerySecurityBase):
    # FIXED (was a real bypass; guard now blocks this) — locked as a passing test.
    def test_pg_sleep_is_rejected_by_guard(self):
        ok, _ = query_guard.validate_query("SELECT pg_sleep(30)")
        self.assertFalse(ok, msg="guard accepted pg_sleep DoS")

    def test_unicode_escaped_pg_sleep_is_rejected_by_guard(self):
        ok, _ = query_guard.validate_query(r'SELECT U&"pg\005fsleep"(30)')
        self.assertFalse(ok, msg="guard accepted Unicode-escaped pg_sleep DoS")

    def test_side_effecting_and_database_dump_functions_are_rejected(self):
        queries = [
            "SELECT setval('court_seq', 1)",
            "SELECT nextval('court_seq')",
            "SELECT lo_create(1234)",
            "SELECT pg_advisory_lock(42)",
            "SELECT pg_notify('events', 'payload')",
            "SELECT pg_sleep_for(interval '1 minute')",
            "SELECT pg_sleep_until(clock_timestamp() + interval '1 minute')",
            "SELECT table_to_xml('scraped_dates'::regclass, true, false, '')",
            "SELECT table_to_xmlschema('courts'::regclass, true, false, '')",
            "SELECT query_to_xml_and_xmlschema('SELECT * FROM courts', true, false, '')",
            "SELECT database_to_xml(true, false, '')",
            "SELECT lo_get(1234)",
        ]
        for sql in queries:
            with self.subTest(sql=sql):
                ok, _ = query_guard.validate_query(sql)
                self.assertFalse(ok, msg=f"guard accepted side-effecting SQL: {sql!r}")

    def test_value_collecting_aggregates_are_rejected(self):
        queries = [
            "SELECT array_agg(case_number) FROM court_cases",
            "SELECT string_agg(case_number, ',') FROM court_cases",
            "SELECT group_concat(case_number) FROM court_cases",
        ]
        for sql in queries:
            with self.subTest(sql=sql):
                ok, error = query_guard.validate_query(sql)
                self.assertFalse(
                    ok,
                    msg=f"guard accepted value-collecting aggregate: {sql!r}",
                )
                self.assertIn("not allowed", error)

    def test_timeout_over_upper_bound_is_400(self):
        self._assert_rejected(
            self._post("SELECT identifier FROM courts", timeout_seconds=121)
        )

    def test_timeout_far_over_bound_is_400(self):
        self._assert_rejected(
            self._post("SELECT identifier FROM courts", timeout_seconds=100000)
        )

    def test_negative_timeout_is_400(self):
        self._assert_rejected(
            self._post("SELECT identifier FROM courts", timeout_seconds=-5)
        )

    def test_non_numeric_timeout_is_400(self):
        self._assert_rejected(
            self._post("SELECT identifier FROM courts", timeout_seconds="not-a-number")
        )

    def test_in_range_timeout_is_allowed(self):
        # A valid timeout with a valid query executes (sqlite skips SET) -> 200.
        resp = self._post("SELECT identifier FROM courts", timeout_seconds=5)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# 6. Blocked / non-allowlisted tables and system catalogs.
# ---------------------------------------------------------------------------
class TestBlockedTables(_QuerySecurityBase):
    def test_blocked_scraped_dates_is_400(self):
        self._assert_rejected(self._post("SELECT * FROM scraped_dates"))

    def test_non_allowlisted_table_is_400(self):
        self._assert_rejected(self._post("SELECT * FROM auth_user"))

    def test_pg_catalog_is_400(self):
        self._assert_rejected(self._post("SELECT * FROM pg_catalog.pg_authid"))

    def test_information_schema_is_400(self):
        self._assert_rejected(self._post("SELECT * FROM information_schema.tables"))

    def test_scraped_dates_via_join_is_400(self):
        self._assert_rejected(
            self._post("SELECT * FROM courts JOIN scraped_dates ON 1=1")
        )

    def test_scraped_dates_in_subquery_is_400(self):
        self._assert_rejected(
            self._post("SELECT (SELECT count(*) FROM scraped_dates) FROM courts")
        )

    def test_scraped_dates_in_union_is_400(self):
        self._assert_rejected(
            self._post(
                "SELECT identifier FROM courts "
                "UNION SELECT scraped_at FROM scraped_dates"
            )
        )

    # FIXED (was a real bypass; guard now blocks this) — locked as a passing test.
    def test_comma_cross_join_to_blocked_table_is_rejected_by_guard(self):
        ok, _ = query_guard.validate_query("SELECT * FROM courts, scraped_dates")
        self.assertFalse(ok, msg="guard accepted a comma cross-join to a blocked table")

    # FIXED (was a real bypass; guard now blocks this) — locked as a passing test.
    def test_comma_cross_join_to_pg_catalog_is_rejected_by_guard(self):
        ok, _ = query_guard.validate_query("SELECT * FROM courts, pg_catalog.pg_authid")
        self.assertFalse(ok, msg="guard accepted a comma cross-join to pg_catalog")

    # FIXED (was a real bypass; guard now blocks this) — locked as a passing test.
    def test_quoted_blocked_table_is_rejected_by_guard(self):
        ok, _ = query_guard.validate_query('SELECT * FROM "scraped_dates"')
        self.assertFalse(ok, msg="guard accepted a double-quoted blocked table")


# ---------------------------------------------------------------------------
# 7. Resource-amplifying joins over otherwise allowed tables.
# ---------------------------------------------------------------------------
class TestJoinConstraints(_QuerySecurityBase):
    def test_unconstrained_joins_are_rejected(self):
        queries = [
            "SELECT * FROM courts, court_cases",
            "SELECT * FROM courts CROSS JOIN court_cases",
            "SELECT * FROM courts JOIN court_cases ON TRUE",
            "SELECT * FROM courts JOIN court_cases ON 1 = 1",
            (
                "SELECT * FROM courts c JOIN court_cases cc "
                "ON c.identifier = c.identifier"
            ),
            (
                "SELECT * FROM courts c JOIN court_cases cc "
                "ON cc.case_number = cc.case_number"
            ),
            (
                "SELECT * FROM courts c JOIN court_cases cc "
                "ON c.identifier = cc.court_identifier OR TRUE"
            ),
            (
                "SELECT * FROM courts c JOIN court_cases cc "
                "ON identifier = court_identifier"
            ),
        ]
        for sql in queries:
            with self.subTest(sql=sql):
                ok, error = query_guard.validate_query(sql)
                self.assertFalse(ok, msg=f"guard accepted unconstrained join: {sql!r}")
                self.assertIn("JOINs must use", error)

    def test_qualified_equijoin_is_allowed(self):
        ok, error = query_guard.validate_query(
            "SELECT cc.case_number FROM courts c "
            "JOIN court_cases cc ON c.identifier = cc.court_identifier"
        )
        self.assertTrue(ok, msg=error)

    def test_using_join_is_allowed(self):
        ok, error = query_guard.validate_query(
            "SELECT case_number FROM court_cases "
            "JOIN court_case_hearings USING (case_number)"
        )
        self.assertTrue(ok, msg=error)


# ---------------------------------------------------------------------------
# Positive control — a legitimate SELECT for an authorized caller is ALLOWED.
# ---------------------------------------------------------------------------
class TestLegitimateSelectAllowed(_QuerySecurityBase):
    def test_select_literal_is_allowed(self):
        resp = self._post("SELECT 1")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertIn("rows", resp.data)
        self.assertEqual(resp.data["max_rows"], query_guard.default_max_rows())

    def test_real_courts_read_is_allowed(self):
        resp = self._post("SELECT identifier FROM courts")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["row_count"], 1)

    def test_ngm_query_scope_without_role_is_allowed(self):
        # Scope-only principals (e.g. MCP service accounts) must keep working.
        self.client.force_authenticate(
            user=self.nobody, token={"scope": "openid ngm.query"}
        )
        resp = self.client.post(
            QUERY_URL, {"query": "SELECT identifier FROM courts"}, format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_scope_only_query_excludes_soft_deleted_cases(self):
        CourtCase.objects.create(case_number="live-case", court_id="supreme")
        CourtCase.objects.create(
            case_number="deleted-case",
            court_id="supreme",
            is_deleted=True,
        )
        self.client.force_authenticate(
            user=self.nobody, token={"scope": "openid ngm.query"}
        )

        resp = self.client.post(
            QUERY_URL,
            {
                "query": (
                    "SELECT case_number FROM court_cases "
                    "ORDER BY case_number"
                )
            },
            format="json",
        )

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["rows"], [["live-case"]])

    def test_scope_only_query_excludes_children_of_deleted_cases(self):
        CourtCase.objects.create(case_number="live-case", court_id="supreme")
        CourtCase.objects.create(
            case_number="deleted-case",
            court_id="supreme",
            is_deleted=True,
        )
        CaseEntity.objects.create(
            case_number="live-case",
            court_id="supreme",
            side="plaintiff",
            name="Visible",
        )
        CaseEntity.objects.create(
            case_number="deleted-case",
            court_id="supreme",
            side="plaintiff",
            name="Hidden",
        )
        self.client.force_authenticate(
            user=self.nobody, token={"scope": "openid ngm.query"}
        )

        resp = self.client.post(
            QUERY_URL,
            {"query": "SELECT name FROM court_case_entities ORDER BY name"},
            format="json",
        )

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["rows"], [["Visible"]])

    def test_scope_only_query_cannot_select_internal_columns(self):
        self.client.force_authenticate(
            user=self.nobody, token={"scope": "openid ngm.query"}
        )

        resp = self.client.post(
            QUERY_URL,
            {"query": "SELECT is_deleted FROM court_cases"},
            format="json",
        )

        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_role_bearing_query_retains_internal_projection(self):
        CourtCase.objects.create(
            case_number="deleted-case",
            court_id="supreme",
            is_deleted=True,
        )

        resp = self._post(
            "SELECT case_number, is_deleted FROM court_cases "
            "WHERE case_number = 'deleted-case'"
        )

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["rows"], [["deleted-case", True]])

    def test_columns_resembling_keywords_are_not_false_positives(self):
        # Column names that merely CONTAIN a forbidden keyword as a substring
        # (updated_at) must not trip the \bword\b denylist.
        resp = self._post("SELECT created_at, updated_at FROM court_cases")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)


def test_postgres_execution_uses_read_only_transaction_and_local_timeout():
    cursor = MagicMock()
    cursor.description = [("identifier",)]
    cursor.fetchmany.side_effect = [[("supreme",)], []]
    connection = MagicMock(vendor="postgresql")
    connection.cursor.return_value.__enter__.return_value = cursor

    with (
        patch.object(views.router, "db_for_read", return_value="ngm"),
        patch.object(views, "connections", {"ngm": connection}),
        patch.object(
            views.transaction,
            "atomic",
            return_value=nullcontext(),
        ) as atomic,
    ):
        result = views.QueryView._execute_select(
            "SELECT identifier FROM courts",
            timeout_seconds=2.5,
        )

    atomic.assert_called_once_with(using="ngm")
    assert cursor.execute.call_args_list == [
        (("SET TRANSACTION READ ONLY",),),
        (("SET LOCAL statement_timeout = %s", [2500]),),
        (("SELECT identifier FROM courts",),),
    ]
    assert result["rows"] == [["supreme"]]


def test_query_execution_rejects_oversized_serialized_result():
    cursor = MagicMock()
    cursor.description = [("value",)]
    cursor.fetchmany.side_effect = [[("x" * 100,)], []]
    connection = MagicMock(vendor="sqlite")
    connection.cursor.return_value.__enter__.return_value = cursor

    with (
        patch.object(views.router, "db_for_read", return_value="ngm"),
        patch.object(views, "connections", {"ngm": connection}),
        patch.object(views.transaction, "atomic", return_value=nullcontext()),
        pytest.raises(views.QueryResultTooLarge),
    ):
        views.QueryView._execute_select(
            "SELECT value FROM courts",
            timeout_seconds=2.5,
            max_response_bytes=50,
        )
