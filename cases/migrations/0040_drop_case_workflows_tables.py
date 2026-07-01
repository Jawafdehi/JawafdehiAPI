"""Drop the table left behind by the removed ``case_workflows`` app.

The agentic casework engine (``case_workflows``: CaseWorkflowRun + the
LangChain/LangGraph/deepagents workflow stack) was deleted 2026-06-30. Its app is
no longer in INSTALLED_APPS, so its own migrations no longer run; on existing
databases the table would otherwise be orphaned forever. This migration lives in a
surviving app (``cases``) to clean it up — mirroring ``0027_drop_caseworker_tables``
which retired the previous-generation agent app the same way.

Notes:
- ``DROP TABLE IF EXISTS`` is a no-op on fresh databases (test/CI) where the
  case_workflows app never created the table.
- We also purge the stale ``django_migrations`` rows for the removed app.
- The drop is irreversible; reverse is a no-op.
"""

from django.db import migrations

DROP_STATEMENTS = [
    "DROP TABLE IF EXISTS case_workflows_caseworkflowrun",
    "DELETE FROM django_migrations WHERE app = 'case_workflows'",
]


class Migration(migrations.Migration):

    dependencies = [
        ("cases", "0039_ngm_rate_tier_groups"),
    ]

    operations = [
        migrations.RunSQL(
            sql=DROP_STATEMENTS,
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
