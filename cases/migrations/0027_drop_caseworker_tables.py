"""Drop the tables left behind by the removed ``caseworker`` app.

The caseworker agent app (Skills/Summaries/Drafts/MCP servers/LLM providers) was
deleted along with the frontend portal it backed. Its app is no longer in
INSTALLED_APPS, so its own migrations no longer run; on existing databases the
tables would otherwise be orphaned forever. This migration lives in a surviving
app (``cases``) to clean them up.

Notes:
- ``DROP TABLE IF EXISTS`` is a no-op on fresh databases (test/CI) where the
  caseworker app never created the tables.
- Tables are dropped in foreign-key dependency order (referencing tables first)
  so it works on PostgreSQL without ``CASCADE`` — which SQLite does not support.
- We also purge the stale ``django_migrations`` rows for the removed app.
- The drop is irreversible; reverse is a no-op.
"""

from django.db import migrations

DROP_STATEMENTS = [
    "DROP TABLE IF EXISTS caseworker_draftversion",
    "DROP TABLE IF EXISTS caseworker_summary",
    "DROP TABLE IF EXISTS caseworker_draft",
    "DROP TABLE IF EXISTS caseworker_skill",
    "DROP TABLE IF EXISTS caseworker_mcpserver",
    "DROP TABLE IF EXISTS caseworker_llmprovider",
    "DELETE FROM django_migrations WHERE app = 'caseworker'",
]


class Migration(migrations.Migration):

    dependencies = [
        ("cases", "0026_merge_20260611_0704"),
    ]

    operations = [
        migrations.RunSQL(
            sql=DROP_STATEMENTS,
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
