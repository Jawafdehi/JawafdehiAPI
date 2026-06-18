from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("cases", "0035_add_corruption_derived_case_types"),
    ]

    operations = [
        migrations.DeleteModel(
            name="ChatUserIdentity",
        ),
    ]
