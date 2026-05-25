# Generated manually for JAWA-1894 Phase 6

from django.conf import settings
from django.db import migrations, models
import django.core.validators
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("cases", "0024_alter_chat_user_identity"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="PromptVariant",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("name", models.CharField(max_length=200, unique=True)),
                (
                    "run_type",
                    models.CharField(
                        choices=[
                            ("tag", "Tag Enrichment"),
                            ("allegation", "Allegation Enrichment"),
                            ("bigo", "Bigo Enrichment"),
                            ("news", "News Article Enrichment"),
                            ("section", "Section Generation"),
                        ],
                        max_length=20,
                    ),
                ),
                (
                    "template",
                    models.TextField(help_text="Prompt template with {placeholders}"),
                ),
                (
                    "template_hash",
                    models.CharField(db_index=True, editable=False, max_length=64),
                ),
                ("description", models.TextField(blank=True)),
                ("is_active", models.BooleanField(db_index=True, default=True)),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "ordering": ["run_type", "-created_at"],
                "unique_together": {("name", "run_type")},
            },
        ),
        migrations.CreateModel(
            name="ABTestConfig",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "config_id",
                    models.CharField(db_index=True, max_length=100, unique=True),
                ),
                (
                    "run_type",
                    models.CharField(
                        choices=[
                            ("tag", "Tag Enrichment"),
                            ("allegation", "Allegation Enrichment"),
                            ("bigo", "Bigo Enrichment"),
                            ("news", "News Article Enrichment"),
                            ("section", "Section Generation"),
                        ],
                        max_length=20,
                    ),
                ),
                (
                    "traffic_split",
                    models.FloatField(
                        default=0.5,
                        help_text="Fraction routed to variant A (0.0-1.0)",
                    ),
                ),
                ("is_active", models.BooleanField(db_index=True, default=True)),
                (
                    "sample_size",
                    models.PositiveIntegerField(
                        blank=True,
                        help_text="Target sample size; null=indefinite",
                        null=True,
                    ),
                ),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("ended_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "variant_a",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="ab_tests_as_a",
                        to="cases.promptvariant",
                    ),
                ),
                (
                    "variant_b",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="ab_tests_as_b",
                        to="cases.promptvariant",
                    ),
                ),
            ],
            options={
                "ordering": ["-created_at"],
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(("traffic_split__gte", 0), ("traffic_split__lte", 1)),
                        name="ab_test_traffic_split_range",
                    ),
                    models.CheckConstraint(
                        condition=~models.Q(
                            ("variant_a", models.F("variant_b"))
                        ),
                        name="ab_test_variants_must_differ",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="EnrichmentRun",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "run_id",
                    models.CharField(db_index=True, max_length=100, unique=True),
                ),
                (
                    "run_type",
                    models.CharField(
                        choices=[
                            ("tag", "Tag Enrichment"),
                            ("allegation", "Allegation Enrichment"),
                            ("bigo", "Bigo Enrichment"),
                            ("news", "News Article Enrichment"),
                            ("section", "Section Generation"),
                        ],
                        max_length=20,
                    ),
                ),
                ("total_cases", models.PositiveIntegerField(default=0)),
                ("enriched_count", models.PositiveIntegerField(default=0)),
                ("skipped_count", models.PositiveIntegerField(default=0)),
                ("failed_count", models.PositiveIntegerField(default=0)),
                ("llm_call_count", models.PositiveIntegerField(default=0)),
                ("total_input_tokens", models.PositiveBigIntegerField(default=0)),
                ("total_output_tokens", models.PositiveBigIntegerField(default=0)),
                (
                    "total_cost_usd",
                    models.DecimalField(decimal_places=6, default=0, max_digits=10),
                ),
                ("avg_latency_ms", models.FloatField(blank=True, null=True)),
                ("tier_breakdown", models.JSONField(blank=True, default=dict)),
                ("metadata", models.JSONField(blank=True, default=dict)),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "ab_test",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="runs",
                        to="cases.abtestconfig",
                    ),
                ),
                (
                    "prompt_variant",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="runs",
                        to="cases.promptvariant",
                    ),
                ),
            ],
            options={
                "ordering": ["-created_at"],
                "indexes": [
                    models.Index(
                        fields=["run_type", "-created_at"],
                        name="enrichment_run_type_created_idx",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="EditorFeedback",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "run_type",
                    models.CharField(
                        choices=[
                            ("tag", "Tag Enrichment"),
                            ("allegation", "Allegation Enrichment"),
                            ("bigo", "Bigo Enrichment"),
                            ("news", "News Article Enrichment"),
                            ("section", "Section Generation"),
                        ],
                        max_length=20,
                    ),
                ),
                (
                    "feedback_type",
                    models.CharField(
                        choices=[
                            ("correct", "Correct"),
                            ("incorrect", "Incorrect"),
                            ("partially_correct", "Partially Correct"),
                            ("missing", "Missing Content"),
                            ("irrelevant", "Irrelevant Output"),
                        ],
                        max_length=30,
                    ),
                ),
                (
                    "original_output",
                    models.JSONField(
                        help_text="Enrichment output presented to the editor"
                    ),
                ),
                (
                    "corrected_output",
                    models.JSONField(
                        blank=True,
                        help_text="Editor-corrected version",
                        null=True,
                    ),
                ),
                ("comment", models.TextField(blank=True, max_length=2000)),
                (
                    "quality_score",
                    models.PositiveSmallIntegerField(
                        blank=True,
                        null=True,
                        validators=[
                            django.core.validators.MinValueValidator(1),
                            django.core.validators.MaxValueValidator(5),
                        ],
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "case",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="enrichment_feedback",
                        to="cases.case",
                    ),
                ),
                (
                    "editor",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "enrichment_run",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="editor_feedback",
                        to="cases.enrichmentrun",
                    ),
                ),
            ],
            options={
                "ordering": ["-created_at"],
                "indexes": [
                    models.Index(
                        fields=["case", "run_type"], name="editor_fb_case_type_idx"
                    ),
                    models.Index(
                        fields=["feedback_type", "-created_at"],
                        name="editor_fb_type_created_idx",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="FewShotExample",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "run_type",
                    models.CharField(
                        choices=[
                            ("tag", "Tag Enrichment"),
                            ("allegation", "Allegation Enrichment"),
                            ("bigo", "Bigo Enrichment"),
                            ("news", "News Article Enrichment"),
                            ("section", "Section Generation"),
                        ],
                        max_length=20,
                    ),
                ),
                (
                    "input_snapshot",
                    models.JSONField(help_text="Case data snapshot used as input"),
                ),
                (
                    "expected_output",
                    models.JSONField(help_text="Expected/correct enrichment output"),
                ),
                (
                    "is_validated",
                    models.BooleanField(
                        db_index=True,
                        default=False,
                        help_text="Moderator-validated for inclusion in prompts",
                    ),
                ),
                (
                    "usage_count",
                    models.PositiveIntegerField(
                        default=0,
                        help_text="Times this example was injected into prompts",
                    ),
                ),
                ("last_used_at", models.DateTimeField(blank=True, null=True)),
                ("notes", models.TextField(blank=True, max_length=1000)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "source_feedback",
                    models.ForeignKey(
                        blank=True,
                        help_text="Derived from this editor correction",
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        to="cases.editorfeedback",
                    ),
                ),
            ],
            options={
                "ordering": ["run_type", "-created_at"],
                "indexes": [
                    models.Index(
                        fields=["run_type", "is_validated"],
                        name="few_shot_type_validated_idx",
                    ),
                ],
            },
        ),
    ]
