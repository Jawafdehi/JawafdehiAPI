"""Widen origin_msg_id 100 -> 300, to fit the values it is actually given.

The proposal-builder consumer sets ``origin_msg_id`` to the matched signal's
dedup key, and those keys embed a full court-case IRI plus a case slug — a
routine one is 108 characters. At 100 the serializer rejected every
docket-derived proposal as a validation failure, which read as "the model
produced something unusable" and left no row behind, so the duplicate check
found nothing and the next scrape bought another premium model call.

Widening rather than truncating: the value's whole purpose is to be traceable
back to the message that caused the proposal, and a truncated key is not.

Cheap on Postgres — a varchar length increase is a catalogue change, no table
rewrite and no long lock.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('case_proposals', '0002_remove_caseupdateproposal_supersedes_and_more'),
    ]

    operations = [
        migrations.AlterField(
            model_name='caseupdateproposal',
            name='origin_msg_id',
            field=models.CharField(blank=True, default='', max_length=300),
        ),
    ]
