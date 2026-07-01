"""Backfill a default RAW role onto existing source-link entries.

Migration 0025 converted plain string URLs to ``{link, role: None}`` dicts.
Role is now mandatory (defaults to RAW), so any stored entry whose role is
``None`` or missing is backfilled to ``RAW`` here. Without this, re-saving a
legacy source would fail the new ``validate_url_list`` check.
"""

from django.db import migrations

RAW = "RAW"


def backfill_roles_to_raw(apps, schema_editor):
    DocumentSource = apps.get_model("cases", "DocumentSource")
    db_alias = schema_editor.connection.alias
    to_update = []

    for source in DocumentSource.objects.using(db_alias).only("id", "url").iterator():
        url_list = source.url
        if not isinstance(url_list, list):
            continue

        changed = False
        converted = []
        for item in url_list:
            if isinstance(item, dict):
                if item.get("role") is None:
                    item = {**item, "role": RAW}
                    changed = True
                converted.append(item)
            elif isinstance(item, str):
                # Defensive: any leftover string entry becomes a RAW dict.
                stripped = item.strip()
                if stripped:
                    converted.append({"link": stripped, "role": RAW})
                    changed = True
            else:
                converted.append(item)

        if changed:
            source.url = converted
            to_update.append(source)

    if to_update:
        DocumentSource.objects.using(db_alias).bulk_update(
            to_update, ["url"], batch_size=500
        )


def noop_reverse(apps, schema_editor):
    """Irreversible in practice — RAW is indistinguishable from a backfilled
    default once applied, so reversing is a no-op rather than re-nulling roles.
    """
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("cases", "0031_make_source_type_required"),
    ]

    operations = [
        migrations.RunPython(backfill_roles_to_raw, noop_reverse, atomic=True),
    ]
