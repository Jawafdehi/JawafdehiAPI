# ``Case.case_type`` and ``CourtCase.case_type`` meant two different things under
# one name -- the offence (10 enum values) and NGM's free-text Nepali मुद्दा
# string (130k distinct values) -- and both reached the search index as a field
# literally called ``case_type``. Only the Case side is renamed here; ``courts``
# keeps its own column, which is what bounds this migration to one table.
#
# Pure RenameField: every stored value is kept. The choices are spelled out
# rather than imported from ``CaseType`` so a later edit to the enum cannot
# rewrite this migration's history.
#
# DEPLOY PREREQUISITE -- this migration is NOT rolling-deploy safe.
# ``RenameField`` emits ``ALTER TABLE ... RENAME COLUMN``. Between the rename
# committing and the last pod of the previous release terminating, every
# SELECT/INSERT from that release names ``case_type`` and fails -- and that
# column is on the case list, the case detail, the search reindex and the
# admin, so the window is a hard 500 on the busiest path. A rollback past
# this point is a second rename, not a no-op.
#
# So it must be deployed with migrations gated BEFORE any new pod serves and
# the old pods already drained (a stop-the-world migrate, not a rolling
# update). If the rollout is a rolling one, this needs the usual split
# instead: add ``offence_type``, dual-write, backfill, cut readers, drop
# ``case_type`` a release later. The rollout config is not in this repo --
# confirm the ordering with whoever owns it before shipping, alongside the
# ``reindex_all --rebuild`` in the same deploy note.

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
