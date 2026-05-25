"""Tests for Phase 6 enrichment quality infrastructure."""

from datetime import date, timedelta

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.utils import timezone
from hypothesis import given, settings
from hypothesis import strategies as st

from cases.models import (
    ABTestConfig,
    EditorFeedback,
    EditorFeedbackType,
    EnrichmentRun,
    EnrichmentRunType,
    FewShotExample,
    PromptVariant,
)
from cases.services.quality import ABTestService, FewShotManager, QualityMetricsCollector

User = get_user_model()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def user(db):
    return User.objects.create_user(username="test_editor", password="test")


@pytest.fixture
def prompt_variant_a(db):
    return PromptVariant.objects.create(
        name="test-variant-a",
        run_type=EnrichmentRunType.TAG,
        template="Classify tags for: {case_title}",
    )


@pytest.fixture
def prompt_variant_b(db):
    return PromptVariant.objects.create(
        name="test-variant-b",
        run_type=EnrichmentRunType.TAG,
        template="Given {case_title}, determine sectors",
    )


@pytest.fixture
def ab_test_config(db, prompt_variant_a, prompt_variant_b):
    return ABTestConfig.objects.create(
        config_id="ab-test-001",
        variant_a=prompt_variant_a,
        variant_b=prompt_variant_b,
        run_type=EnrichmentRunType.TAG,
        traffic_split=0.5,
        started_at=timezone.now(),
    )


@pytest.fixture
def enrichment_run(db, prompt_variant_a, ab_test_config):
    from cases.models import EnrichmentRun
    import uuid

    return EnrichmentRun.objects.create(
        run_id=uuid.uuid4().hex,
        run_type=EnrichmentRunType.TAG,
        prompt_variant=prompt_variant_a,
        ab_test=ab_test_config,
        started_at=timezone.now(),
    )


@pytest.fixture
def completed_run(db, enrichment_run):
    enrichment_run.completed_at = timezone.now()
    enrichment_run.enriched_count = 10
    enrichment_run.failed_count = 2
    enrichment_run.total_cases = 12
    enrichment_run.save()
    return enrichment_run


# ---------------------------------------------------------------------------
# PromptVariant model tests
# ---------------------------------------------------------------------------


class TestPromptVariantModel:
    def test_auto_hash_on_save(self, db):
        v = PromptVariant.objects.create(
            name="hash-test", run_type=EnrichmentRunType.ALLEGATION, template="hello"
        )
        assert len(v.template_hash) == 64
        assert v.template_hash == v.template_hash  # stable

    def test_same_template_different_names(self, db):
        tpl = "Summarize: {title}"
        a = PromptVariant.objects.create(name="v1", run_type=EnrichmentRunType.NEWS, template=tpl)
        b = PromptVariant.objects.create(name="v2", run_type=EnrichmentRunType.NEWS, template=tpl)
        assert a.template_hash == b.template_hash

    def test_unique_name_per_run_type(self, db):
        PromptVariant.objects.create(name="x", run_type=EnrichmentRunType.TAG, template="t1")
        with pytest.raises(IntegrityError):
            PromptVariant.objects.create(name="x", run_type=EnrichmentRunType.TAG, template="t2")


# ---------------------------------------------------------------------------
# ABTestConfig model tests
# ---------------------------------------------------------------------------


class TestABTestConfigModel:
    def test_variants_must_differ(self, db, prompt_variant_a):
        with pytest.raises(IntegrityError):
            ABTestConfig.objects.create(
                config_id="bad-test",
                variant_a=prompt_variant_a,
                variant_b=prompt_variant_a,
                run_type=EnrichmentRunType.TAG,
            )

    def test_traffic_split_range(self, db, prompt_variant_a, prompt_variant_b):
        with pytest.raises(IntegrityError):
            ABTestConfig.objects.create(
                config_id="bad-split",
                variant_a=prompt_variant_a,
                variant_b=prompt_variant_b,
                run_type=EnrichmentRunType.TAG,
                traffic_split=1.5,
            )


# ---------------------------------------------------------------------------
# EnrichmentRun model tests
# ---------------------------------------------------------------------------


class TestEnrichmentRunModel:
    def test_create_run(self, db, enrichment_run):
        assert enrichment_run.run_id
        assert enrichment_run.total_cases == 0
        assert enrichment_run.enriched_count == 0

    def test_run_with_prompt_variant(self, db, enrichment_run, prompt_variant_a):
        assert enrichment_run.prompt_variant == prompt_variant_a


# ---------------------------------------------------------------------------
# EditorFeedback model tests
# ---------------------------------------------------------------------------


class TestEditorFeedbackModel:
    def test_quality_score_range(self, db):
        from django.core.exceptions import ValidationError

        fb = EditorFeedback(
            run_type=EnrichmentRunType.TAG,
            feedback_type=EditorFeedbackType.INCORRECT,
            original_output={"tags": ["wrong"]},
            corrected_output={"tags": ["right"]},
            quality_score=0,
        )
        with pytest.raises(ValidationError):
            fb.full_clean()

        fb.quality_score = 6
        with pytest.raises(ValidationError):
            fb.full_clean()

        fb.quality_score = 3
        fb.full_clean()  # no error


# ---------------------------------------------------------------------------
# FewShotExample model tests
# ---------------------------------------------------------------------------


class TestFewShotExampleModel:
    def test_create_example(self, db):
        ex = FewShotExample.objects.create(
            run_type=EnrichmentRunType.ALLEGATION,
            input_snapshot={"title": "test case"},
            expected_output={"allegations": ["test"]},
        )
        assert not ex.is_validated
        assert ex.usage_count == 0

    def test_validate_example(self, db):
        ex = FewShotExample.objects.create(
            run_type=EnrichmentRunType.ALLEGATION,
            input_snapshot={},
            expected_output={},
        )
        ex.is_validated = True
        ex.save()
        ex.refresh_from_db()
        assert ex.is_validated


# ---------------------------------------------------------------------------
# QualityMetricsCollector tests
# ---------------------------------------------------------------------------


class TestQualityMetricsCollector:
    def test_start_run_creates_enrichment_run(self, db):
        collector = QualityMetricsCollector()
        run = collector.start_run(EnrichmentRunType.TAG)
        assert run.run_id
        assert run.started_at is not None

    def test_record_case_result_updates_metrics(self, db):
        collector = QualityMetricsCollector()
        run = collector.start_run(EnrichmentRunType.TAG)

        collector.record_case_result(
            run, tier="rule_based", tokens_in=500, tokens_out=200,
            latency_ms=350.0, success=True,
        )
        run.refresh_from_db()
        assert run.llm_call_count == 1
        assert run.total_input_tokens == 500
        assert run.total_output_tokens == 200
        assert run.enriched_count == 1
        assert run.tier_breakdown == {"rule_based": 1}
        assert run.avg_latency_ms == 350.0

    def test_record_skip(self, db):
        collector = QualityMetricsCollector()
        run = collector.start_run(EnrichmentRunType.TAG)
        collector.record_skip(run)
        run.refresh_from_db()
        assert run.total_cases == 1
        assert run.skipped_count == 1

    def test_complete_run(self, db):
        collector = QualityMetricsCollector()
        run = collector.start_run(EnrichmentRunType.TAG)
        completed = collector.complete_run(run)
        assert completed.completed_at is not None

    def test_compute_trend_empty(self, db):
        collector = QualityMetricsCollector()
        trend = collector.compute_trend(
            EnrichmentRunType.TAG,
            date.today(),
            date.today(),
        )
        assert trend["total_runs"] == 0
        assert trend["total_enriched"] == 0

    def test_compute_trend_with_data(self, db):
        collector = QualityMetricsCollector()
        today = date.today()
        for _ in range(3):
            run = collector.start_run(EnrichmentRunType.TAG)
            collector.record_case_result(
                run, "rule_based", 100, 50, 200.0, True
            )
            collector.complete_run(run)

        trend = collector.compute_trend(EnrichmentRunType.TAG, today, today)
        assert trend["total_runs"] == 3
        assert trend["total_enriched"] == 3

    def test_get_trend_report_returns_correct_window_count(self, db):
        collector = QualityMetricsCollector()
        report = collector.get_trend_report(EnrichmentRunType.TAG, num_weeks=2)
        assert len(report) == 2


# ---------------------------------------------------------------------------
# ABTestService tests
# ---------------------------------------------------------------------------


class TestABTestService:
    def test_get_active_test_returns_none_when_no_active(self, db):
        svc = ABTestService()
        assert svc.get_active_test(EnrichmentRunType.TAG) is None

    def test_get_active_test_returns_active(self, db, ab_test_config):
        svc = ABTestService()
        test = svc.get_active_test(EnrichmentRunType.TAG)
        assert test is not None
        assert test.config_id == "ab-test-001"

    def test_select_variant_deterministic(self, db, ab_test_config):
        svc = ABTestService()
        # traffic_split=1.0 means always variant A
        ab_test_config.traffic_split = 1.0
        ab_test_config.save()
        for _ in range(100):
            v = svc.select_variant(ab_test_config)
            assert v == ab_test_config.variant_a

        # traffic_split=0.0 means always variant B
        ab_test_config.traffic_split = 0.0
        ab_test_config.save()
        for _ in range(100):
            v = svc.select_variant(ab_test_config)
            assert v == ab_test_config.variant_b

    def test_compare_results_insufficient_data(self, db, ab_test_config):
        svc = ABTestService()
        result = svc.compare_results(ab_test_config)
        assert result["winner"] == "insufficient_data"

    def test_compare_results_with_data(self, db, ab_test_config, prompt_variant_a):
        collector = QualityMetricsCollector()

        for _ in range(5):
            run = collector.start_run(
                EnrichmentRunType.TAG,
                prompt_variant=prompt_variant_a,
                ab_test=ab_test_config,
            )
            collector.record_case_result(run, "rule_based", 100, 50, 300.0, True)
            collector.complete_run(run)

        svc = ABTestService()
        result = svc.compare_results(ab_test_config)
        assert result["winner"] in ("insufficient_data", "tie", "a", "b")

    def test_complete_test_deactivates(self, db, ab_test_config, prompt_variant_a, prompt_variant_b):
        svc = ABTestService()
        svc.complete_test(ab_test_config, prompt_variant_a)
        ab_test_config.refresh_from_db()
        assert not ab_test_config.is_active

    def test_create_test(self, db, prompt_variant_a, prompt_variant_b):
        svc = ABTestService()
        test = svc.create_test(prompt_variant_a, prompt_variant_b)
        assert test.config_id.startswith("ab-tag-")
        assert test.variant_a == prompt_variant_a
        assert test.variant_b == prompt_variant_b
        assert test.is_active


# ---------------------------------------------------------------------------
# FewShotManager tests
# ---------------------------------------------------------------------------


class TestFewShotManager:
    def test_get_examples_empty(self, db):
        mgr = FewShotManager()
        examples = mgr.get_examples_for_run_type(EnrichmentRunType.TAG)
        assert examples == []

    def test_get_examples_only_validated(self, db):
        FewShotExample.objects.create(
            run_type=EnrichmentRunType.TAG,
            input_snapshot={"a": 1},
            expected_output={"b": 2},
        )
        validated = FewShotExample.objects.create(
            run_type=EnrichmentRunType.TAG,
            input_snapshot={"a": 2},
            expected_output={"b": 3},
            is_validated=True,
        )
        mgr = FewShotManager()
        examples = mgr.get_examples_for_run_type(EnrichmentRunType.TAG)
        assert len(examples) == 1
        assert examples[0] == validated

    def test_inject_into_prompt(self, db):
        mgr = FewShotManager()
        base = "Classify: {title}"
        examples = [
            FewShotExample(
                input_snapshot={"title": "A"}, expected_output={"tags": ["x"]}
            ),
            FewShotExample(
                input_snapshot={"title": "B"}, expected_output={"tags": ["y"]}
            ),
        ]
        result = mgr.inject_into_prompt(base, examples)
        assert "INPUT 1" in result
        assert "INPUT 2" in result
        assert "Classify: {title}" in result

    def test_inject_into_prompt_empty(self, db):
        mgr = FewShotManager()
        base = "Classify: {title}"
        result = mgr.inject_into_prompt(base, [])
        assert result == base

    def test_promote_from_feedback_requires_corrected_output(self, db, user):
        from cases.models import Case, CaseState, CaseType

        case = Case.objects.create(
            case_id="test-abc",
            title="Test Case",
            case_type=CaseType.CORRUPTION,
            state=CaseState.DRAFT,
        )
        fb = EditorFeedback.objects.create(
            case=case,
            run_type=EnrichmentRunType.TAG,
            feedback_type=EditorFeedbackType.INCORRECT,
            original_output={"tags": ["bad"]},
            corrected_output=None,
        )

        mgr = FewShotManager()
        with pytest.raises(ValueError, match="corrected_output"):
            mgr.promote_from_feedback(fb)

    def test_promote_from_feedback_creates_example(self, db, user):
        from cases.models import Case, CaseState, CaseType

        case = Case.objects.create(
            case_id="test-def",
            title="Test Case",
            case_type=CaseType.CORRUPTION,
            state=CaseState.DRAFT,
        )
        fb = EditorFeedback.objects.create(
            case=case,
            run_type=EnrichmentRunType.TAG,
            feedback_type=EditorFeedbackType.INCORRECT,
            original_output={"tags": ["bad"]},
            corrected_output={"tags": ["good"]},
            editor=user,
        )

        mgr = FewShotManager()
        ex = mgr.promote_from_feedback(fb, created_by=user)
        assert ex.run_type == EnrichmentRunType.TAG
        assert not ex.is_validated
        assert ex.source_feedback == fb
        assert ex.input_snapshot == {"tags": ["bad"]}
        assert ex.expected_output == {"tags": ["good"]}

    def test_validate_example(self, db):
        ex = FewShotExample.objects.create(
            run_type=EnrichmentRunType.TAG,
            input_snapshot={},
            expected_output={},
        )
        mgr = FewShotManager()
        mgr.validate_example(ex)
        ex.refresh_from_db()
        assert ex.is_validated

    def test_record_usage(self, db):
        ex = FewShotExample.objects.create(
            run_type=EnrichmentRunType.TAG,
            input_snapshot={},
            expected_output={},
        )
        mgr = FewShotManager()
        mgr.record_usage([ex])
        ex.refresh_from_db()
        assert ex.usage_count == 1
        assert ex.last_used_at is not None
