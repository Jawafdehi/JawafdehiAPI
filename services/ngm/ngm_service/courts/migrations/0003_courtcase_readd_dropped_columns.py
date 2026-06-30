"""Re-add 8 load-bearing legacy columns to the ``CourtCase`` ORM (spec 01 §5).

These columns (``status``, ``verdict_type``, ``verdict_date_bs``,
``verdict_date_ad``, ``verdict_judge``, ``case_subject``, ``hearing_count``,
``registration_number``) PHYSICALLY pre-exist on the shared ``ngm_v1``
``court_cases`` table (created/owned by the SQLAlchemy scraper side). They were
omitted from the original ORM projection; this migration re-adds them so they are
queryable/indexable typed fields.

PROD posture (SAME as 0001_initial for these managed tables): apply with
``manage.py migrate --fake ngm_service.courts 0003`` so Django records the
migration as run WITHOUT issuing ``ALTER TABLE`` (the columns already exist on
``ngm_v1`` / on a copy-mode target stood up from the legacy schema dump). Issuing
real DDL there would error on the already-present columns.

TEST / fresh-from-migrations posture: the operations below are real ``AddField``s,
so a database built purely from migrations (the pytest sqlite test DB, or a fresh
target bootstrapped from Django migrations alone) gets the columns created. This
is why the migration is NOT ``SeparateDatabaseAndState`` with empty
``database_operations`` — that would leave the test DB without the columns and
break the importer's copy-mode tests that write them.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("courts", "0002_alter_blacklistedfirm_nes_id_alter_caseentity_nes_id_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="courtcase",
            name="status",
            field=models.CharField(
                max_length=50, null=True, blank=True, db_index=True
            ),
        ),
        migrations.AddField(
            model_name="courtcase",
            name="verdict_type",
            field=models.CharField(max_length=200, null=True, blank=True),
        ),
        migrations.AddField(
            model_name="courtcase",
            name="verdict_date_bs",
            field=models.CharField(max_length=50, null=True, blank=True),
        ),
        migrations.AddField(
            model_name="courtcase",
            name="verdict_date_ad",
            field=models.DateField(null=True, blank=True, db_index=True),
        ),
        migrations.AddField(
            model_name="courtcase",
            name="verdict_judge",
            field=models.CharField(max_length=500, null=True, blank=True),
        ),
        migrations.AddField(
            model_name="courtcase",
            name="case_subject",
            field=models.TextField(null=True, blank=True),
        ),
        migrations.AddField(
            model_name="courtcase",
            name="hearing_count",
            field=models.IntegerField(null=True, blank=True),
        ),
        migrations.AddField(
            model_name="courtcase",
            name="registration_number",
            field=models.CharField(max_length=100, null=True, blank=True),
        ),
    ]
