# Add the caseworker-controlled visibility_policy (see materials.models.Policy)
# and backfill it from source. The cached `visibility` column is NOT recomputed
# here — it is healed post-deploy by materials.visibility.recompute_all() (which
# promotes corpus docs mis-demoted by the doc-dedup re-point back to LISTED).

from django.db import migrations, models

# The stable IRI namespace for case-UPLOADED materials (JAWAF_SOURCE). Anything
# else is a corpus document that is public on its own merits.
_JAWAF_SOURCE = "jawafdehi"


def backfill_policy(apps, schema_editor):
    Material = apps.get_model("materials", "Material")
    # Route to the DB currently being migrated (Material lives on ``ngm``); the
    # router's allow_migrate gates this RunPython to that alias's pass. AddField
    # already stamped every row PUBLIC (the field default); only the case-uploaded
    # (jawafdehi-source) rows need flipping to CASE_GATED so raw uploads are
    # embargoed until their case advances.
    db_alias = schema_editor.connection.alias
    Material.objects.using(db_alias).filter(source=_JAWAF_SOURCE).update(
        visibility_policy="CASE_GATED"
    )


class Migration(migrations.Migration):

    dependencies = [
        ("materials", "0004_material_visibility"),
    ]

    operations = [
        migrations.AddField(
            model_name="material",
            name="visibility_policy",
            field=models.CharField(
                choices=[
                    ("PUBLIC", "Public"),
                    ("CASE_GATED", "Case-gated"),
                    ("PRIVATE", "Private"),
                ],
                db_index=True,
                default="PUBLIC",
                max_length=12,
            ),
        ),
        migrations.RunPython(backfill_policy, migrations.RunPython.noop),
    ]
