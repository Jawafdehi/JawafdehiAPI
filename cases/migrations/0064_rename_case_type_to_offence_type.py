# ``Case.case_type`` and ``CourtCase.case_type`` meant two different things under
# one name -- the offence (10 enum values) and NGM's free-text Nepali मुद्दा
# string (130k distinct values) -- and both reached the search index as a field
# literally called ``case_type``. Only the Case side is renamed here; ``courts``
# keeps its own column, which is what bounds this migration to one table.
#
# Pure RenameField: every stored value is kept. The choices are spelled out
# rather than imported from ``CaseType`` so a later edit to the enum cannot
# rewrite this migration's history.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("cases", "0063_case_tags_source"),
    ]

    operations = [
        migrations.RenameField(
            model_name="case",
            old_name="case_type",
            new_name="offence_type",
        ),
        migrations.AlterField(
            model_name="case",
            name="offence_type",
            field=models.CharField(
                choices=[
                    ("CORRUPTION", "Corruption"),
                    ("BRIBERY", "Bribery"),
                    ("FORGERY", "Forgery"),
                    ("EMBEZZLEMENT", "Embezzlement"),
                    ("ABUSE_OF_OFFICE", "Abuse of Office"),
                    ("MONEY_LAUNDERING", "Money Laundering"),
                    ("ILLEGAL_PROPERTY", "Illegal Property"),
                    ("EXAM_RIGGING", "Exam Rigging"),
                    ("TAX_EVASION", "Tax Evasion"),
                    ("BANKING_OFFENCE", "Banking Offence"),
                ],
                help_text="The offence alleged in this case",
                max_length=20,
            ),
        ),
    ]
