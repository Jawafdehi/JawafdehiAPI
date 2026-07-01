"""Backfill any residual NULL source_type to MISC before enforcing NOT NULL.

The revamp migration (0027) classified every existing source, so in practice
there should be no NULLs left. This is a defensive sweep for any rows created
NULL between that migration and this one (the column was still nullable in
between). Pairs with 0031, which sets the column NOT NULL.
"""

from __future__ import annotations

from django.db import migrations

_MISC = "MISC"  # mirrors cases.models.SourceType.MISC; hardcoded to insulate
# this historical migration from later edits to the enum.


def backfill_null_source_type(apps, schema_editor):
    DocumentSource = apps.get_model("cases", "DocumentSource")
    updated = DocumentSource.objects.filter(source_type__isnull=True).update(
        source_type=_MISC
    )
    if updated:
        print(f"  backfill_null_source_type: set {updated} NULL row(s) to MISC.")


class Migration(migrations.Migration):

    dependencies = [
        ("cases", "0029_merge_20260611_1824"),
    ]

    operations = [
        migrations.RunPython(
            backfill_null_source_type,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
