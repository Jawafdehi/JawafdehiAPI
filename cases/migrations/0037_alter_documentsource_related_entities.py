"""Document the entity @id IRI re-key on ``DocumentSource.related_entities``.

``EntityListField`` now validates entries as canonical entity @id IRIs
(``https://jawafdehi.org/entity/<prefix>/<slug>``) via
``jawafdehi_shared.entities.ids.is_valid_entity_iri`` — the same id form as
``CaseEntityRelationship.nes_id`` — instead of the legacy ``entity:`` form. The
validator lives in ``.validate()`` (not migration state), so this migration only
records the help-text change.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("cases", "0036_alter_caseentityrelationship_nes_id"),
    ]

    operations = [
        migrations.AlterField(
            model_name="documentsource",
            name="related_entities",
            field=models.JSONField(
                blank=True,
                default=list,
                help_text="Canonical NES entity @id IRIs related to this source",
            ),
        ),
    ]
