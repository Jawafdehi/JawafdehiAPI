"""Revamp DocumentSource.source_type taxonomy + fix markdown link roles.

NEUTRALIZED (ADR: cases own no documents). This was a historical
``DocumentSource`` data migration that re-classified ``source_type`` via
``cases.services.source_classifier`` and re-roled ``.md`` links to MARKDOWN.
``DocumentSource`` and that service have since been removed, so the forward
operation is now a NO-OP: there is no ``DocumentSource`` model to migrate, and a
fresh database never has legacy source rows to reclassify. The migration is kept
(not deleted) to preserve the linear migration history for existing databases;
its data effect, if any, was already applied before the model was dropped.
"""

from __future__ import annotations

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("cases", "0026_merge_20260611_0704"),
    ]

    operations = [
        # No-op: DocumentSource + source_classifier were removed. See docstring.
        migrations.RunPython(
            migrations.RunPython.noop,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
