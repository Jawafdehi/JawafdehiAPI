"""Re-key ``CaseEntityRelationship.nes_id`` to the schema.org ``@id`` IRI.

The Case<->NES-entity join key is now the canonical entity @id IRI
(``https://jawafdehi.org/entity/<prefix>/<slug>``) validated by
``validate_nes_id`` against ``jawafdehi_shared.entities.ids.is_valid_entity_iri``
(the legacy ``entity:<prefix>/<slug>`` form is gone — clean slate, empty data).
This only updates the help text (the validator is referenced by name, so the
swap from the legacy validator is not captured in migration state); the field
type/length are unchanged.
"""

import cases.models
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("cases", "0035_caseentityrelationship_nes_id_drop_jawafentity"),
    ]

    operations = [
        migrations.AlterField(
            model_name="caseentityrelationship",
            name="nes_id",
            field=models.CharField(
                db_index=True,
                help_text="Canonical NES entity @id IRI (https://jawafdehi.org/entity/<prefix>/<slug>) this case is bound to. NES owns the entity data; this is the join key only.",
                max_length=300,
                validators=[cases.models.validate_nes_id],
            ),
        ),
    ]
