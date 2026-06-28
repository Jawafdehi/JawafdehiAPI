"""Drop the table left behind by the removed ``case_workflows`` app.

The case_workflows agent app (the LangChain/LangGraph/deepagents workflow engine
and the ciaa_caseworker template) was deleted along with the LLM/agent dependency
stack it was the sole consumer of. Its app is no longer in INSTALLED_APPS, so its
own migrations no longer run; on existing databases the table would otherwise be
orphaned forever. This migration lives in a surviving app (``cases``) to clean it
up, mirroring ``0027_drop_caseworker_tables`` which retired the previous agent app.

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
        ("cases", "0034_merge_20260612_0646"),
    ]

    operations = [
        migrations.RunSQL(
            sql=DROP_STATEMENTS,
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
