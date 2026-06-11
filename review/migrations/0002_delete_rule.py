from django.db import migrations


class Migration(migrations.Migration):
    """Drop the editable Rule table.

    Review rules are now code-enforced (review/rule_defaults.py via
    review/code_rules.py) and read-only. The DB-backed Rule model was removed.
    """

    dependencies = [
        ("review", "0001_initial"),
    ]

    operations = [
        migrations.DeleteModel(name="Rule"),
    ]
