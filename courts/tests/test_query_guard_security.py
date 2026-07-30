"""Penetration tests for the raw-SQL endpoint (``POST /api/query/``).

Target: :class:`courts.views.QueryView` — a SELECT-only raw-SQL surface over the
ngm DB, open to ANY authenticated principal (no role, no scope) and bounded by
:func:`courts.query_guard.validate_query` (SELECT-only, forbidden-keyword
denylist, ``scraped_dates`` block, table allowlist, row cap, statement timeout).

Because the role gate is gone, ``query_guard`` is the ONLY thing standing
between an ordinary signed-in account and the ngm DB. That makes this suite the
load-bearing control on this surface, not a belt-and-braces extra — every
rejection below must hold for a caller with no privileges whatsoever.

These are adversarial tests: every attack asserts the endpoint REJECTS the
input (400 from the guard, or 401 when unauthenticated) — NEVER a
200-with-data and NEVER a 500. A legitimate SELECT is asserted to be ALLOWED so
the guard is not merely blanket-denying.

The test DB is sqlite; the guard is a *string-level* policy check that runs
BEFORE execution, so every rejection test is decided by the guard without
needing Postgres. Tests that would need Postgres-specific execution (e.g. real
``pg_sleep``) assert the guard-level decision instead.

Auth mirrors ``courts/tests/test_api.py``: rather than mint real Zitadel JWTs,
we ``force_authenticate`` a user — either one whose synced Django Groups grant
an NGM role, or ``nobody``, who has no groups at all and stands in for an
ordinary signed-in account.

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

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from rest_framework import status
from rest_framework.test import APITestCase

from courts import query_guard
from courts.models import Court

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
        """A rejected query is a clean 400 — never 200-with-data, never 500.

        Tightened from ``400 or 403`` now that the role gate is gone: with an
        authenticated caller there is no longer any path that legitimately
        answers 403 here, so every rejection must come from the guard itself.
        """
        self.assertEqual(
            resp.status_code,
            status.HTTP_400_BAD_REQUEST,
            msg=f"expected rejection, got {resp.status_code}: {getattr(resp, 'data', None)}",
        )


# ---------------------------------------------------------------------------
# 7. Auth gate — authentication required, role NOT required.
# ---------------------------------------------------------------------------
class TestAuthGate(_QuerySecurityBase):
    """Authentication is required; a ROLE is not.

    This plane reads the same rows the public REST plane already serves
    anonymously, so any authenticated principal may query it. Authentication is
    kept so every query is attributable to an identity and metered by the
    ``user`` throttle rather than the anon one.
    """

    def test_unauthenticated_is_401(self):
        resp = self.client.post(QUERY_URL, {"query": "SELECT 1"}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_bogus_bearer_token_is_401(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer not-a-real-jwt")
        resp = self.client.post(QUERY_URL, {"query": "SELECT 1"}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_authenticated_without_any_role_or_scope_is_allowed(self):
        # The point of the change: a signed-in principal with NO granted role
        # (empty Zitadel role claim -> groups.set([])) can reproduce our
        # published analysis without an admin first granting them ReadOnly.
        self.client.force_authenticate(user=self.nobody)
        resp = self.client.post(QUERY_URL, {"query": "SELECT 1"}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_non_ngm_scope_without_role_is_allowed(self):
        self.client.force_authenticate(
            user=self.nobody, token={"scope": "openid profile"}
        )
        resp = self.client.post(QUERY_URL, {"query": "SELECT 1"}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_role_less_caller_sending_a_write_is_still_rejected(self):
        # Dropping the ROLE gate must not let a write through. Auth now passes,
        # so the SQL guard is the only thing stopping this -> 400, never 200.
        self.client.force_authenticate(user=self.nobody)
        resp = self.client.post(
            QUERY_URL, {"query": "DROP TABLE court_cases"}, format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_role_less_caller_cannot_reach_blocked_tables(self):
        # Same idea for the allowlist: the guard, not the role gate, is what
        # keeps a role-less caller out of scraped_dates, the auth tables and the
        # system catalogs. These were previously masked by the 403.
        self.client.force_authenticate(user=self.nobody)
        for sql in (
            "SELECT * FROM scraped_dates",
            "SELECT * FROM auth_user",
            "SELECT * FROM pg_catalog.pg_authid",
            "SELECT * FROM information_schema.tables",
        ):
            with self.subTest(sql=sql):
                resp = self.client.post(QUERY_URL, {"query": sql}, format="json")
                self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)


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
        ok, _ = query_guard.validate_query(
            "SELECT * FROM courts, pg_catalog.pg_authid"
        )
        self.assertFalse(ok, msg="guard accepted a comma cross-join to pg_catalog")

    # FIXED (was a real bypass; guard now blocks this) — locked as a passing test.
    def test_quoted_blocked_table_is_rejected_by_guard(self):
        ok, _ = query_guard.validate_query('SELECT * FROM "scraped_dates"')
        self.assertFalse(ok, msg="guard accepted a double-quoted blocked table")


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
        # They now pass on plain authentication rather than on the scope, but
        # the observable contract for those clients is unchanged.
        self.client.force_authenticate(
            user=self.nobody, token={"scope": "openid ngm.query"}
        )
        resp = self.client.post(
            QUERY_URL, {"query": "SELECT identifier FROM courts"}, format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_row_cap_is_500_per_request(self):
        # The bulk-pull budget callers rely on: one request may return up to
        # 500 rows, and the cap is applied by wrapping the caller's SELECT.
        self.assertEqual(query_guard.default_max_rows(), 500)
        capped = query_guard.apply_row_cap("SELECT identifier FROM courts", 500)
        self.assertIn("LIMIT 500", capped)

    def test_columns_resembling_keywords_are_not_false_positives(self):
        # Column names that merely CONTAIN a forbidden keyword as a substring
        # (updated_at) must not trip the \bword\b denylist.
        resp = self._post("SELECT created_at, updated_at FROM court_cases")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
