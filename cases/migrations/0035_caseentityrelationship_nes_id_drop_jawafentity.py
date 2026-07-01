"""Make CaseEntityRelationship the Case <-> NES-entity bind and drop JawafEntity.

NES is the single source of truth for entities, so Jawafdehi no longer stores
entity data locally. This migration:

* drops ``Case.unified_entities`` (the M2M to JawafEntity through
  CaseEntityRelationship),
* replaces ``CaseEntityRelationship.entity`` (FK -> JawafEntity) with a
  ``nes_id`` CharField holding the canonical NES entity id, and re-points the
  unique constraint to ``(case, nes_id, relationship_type)`` and the entity
  index to ``nes_id``,
* replaces ``DocumentSource.related_entities`` (M2M to JawafEntity) with an
  EntityListField holding a list of canonical NES entity ids, and
* deletes the ``JawafEntity`` model entirely.

NO DATA MIGRATION IS NEEDED: the dev databases are empty. (Production, if ever
populated, would need a separate backfill that maps each JawafEntity to its
nes_id — but every JawafEntity row would already have to carry an nes_id under
the new "nes_id is required" rule, so the bind has no display-name fallback to
preserve.) This is a pure schema migration.
"""

import cases.fields
import cases.models
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("cases", "0034_merge_20260612_0646"),
    ]

    operations = [
        # --- Drop relations to JawafEntity before deleting the model ---
        migrations.RemoveField(
            model_name="case",
            name="unified_entities",
        ),
        migrations.RemoveIndex(
            model_name="caseentityrelationship",
            name="entity_relationship_type_idx",
        ),
        migrations.RemoveConstraint(
            model_name="caseentityrelationship",
            name="unique_case_entity_relationship_type",
        ),
        migrations.RemoveField(
            model_name="caseentityrelationship",
            name="entity",
        ),
        migrations.RemoveField(
            model_name="documentsource",
            name="related_entities",
        ),
        # --- New CaseEntityRelationship bind shape ---
        migrations.AddField(
            model_name="caseentityrelationship",
            name="nes_id",
            field=models.CharField(
                db_index=True,
                default="entity:person/placeholder",
                help_text=(
                    "Canonical NES entity id (entity:<prefix>/<slug>) this case "
                    "is bound to. NES owns the entity data; this is the join key "
                    "only."
                ),
                max_length=300,
                validators=[cases.models.validate_nes_id],
            ),
            preserve_default=False,
        ),
        migrations.AddIndex(
            model_name="caseentityrelationship",
            index=models.Index(
                fields=["nes_id", "relationship_type"],
                name="entity_relationship_type_idx",
            ),
        ),
        migrations.AddConstraint(
            model_name="caseentityrelationship",
            constraint=models.UniqueConstraint(
                fields=["case", "nes_id", "relationship_type"],
                name="unique_case_entity_relationship_type",
            ),
        ),
        # --- DocumentSource.related_entities as a list of NES ids ---
        migrations.AddField(
            model_name="documentsource",
            name="related_entities",
            field=models.JSONField(
                blank=True,
                default=list,
                help_text="Canonical NES entity ids related to this source",
            ),
        ),
        # --- Remove the local entity model ---
        migrations.DeleteModel(
            name="JawafEntity",
        ),
    ]
