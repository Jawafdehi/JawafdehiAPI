# APPLY THIS FOR REAL — do not --fake it. The nes migrations are normally faked in
# prod because the SQLAlchemy side owns those tables (see the managed-table note in
# entities/models.py), but entities.merged_into is a genuinely new column. Faking it
# leaves the column missing, so every entity read fails on it and no merged entity's
# URL redirects to its survivor.
#
# If you ran an earlier revision of this branch, you applied a since-deleted
# 0004_entity_merge that added the same column (indexed) plus an entity_merges table.
# This migration then fails with 'column "merged_into" already exists', and Django
# cannot reverse a migration whose file is gone — renaming this one does not help,
# because the column is what collides. Clear it by hand first:
#
#   DELETE FROM django_migrations WHERE app='entities' AND name='0004_entity_merge';
#   DROP TABLE IF EXISTS entity_merges;
#   ALTER TABLE entities DROP COLUMN IF EXISTS merged_into;
#
# Local databases only. No shared environment ever applied it.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("entities", "0003_storedentity_is_deleted"),
    ]

    operations = [
        migrations.AddField(
            model_name="storedentity",
            name="merged_into",
            field=models.TextField(blank=True, null=True),
        ),
    ]
