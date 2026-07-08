from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('cases', '0047_caseentityrelationship_outcome'),
    ]

    operations = [
        migrations.AddField(
            model_name='case',
            name='case_start_date_bs',
            field=models.CharField(blank=True, help_text="Bikram Sambat (BS) start date, e.g. '2080-09-18' (optional)", max_length=10, null=True),
        ),
        migrations.AddField(
            model_name='case',
            name='case_end_date_bs',
            field=models.CharField(blank=True, help_text="Bikram Sambat (BS) end date, e.g. '2080-09-18' (optional)", max_length=10, null=True),
        ),
    ]
