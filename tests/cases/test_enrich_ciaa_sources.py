"""
Tests for the enrich_ciaa_sources management command.

Covers:
- Command loads data indices and processes DRAFT cases
- AG charge sheet source creation and idempotency
- Press release metadata enrichment
- Press release discovery via defendant name matching
- Dry-run mode
- --limit and --case-id flags
- --skip-press-releases flag
- Missing data directory handling
"""

import csv
from datetime import date
from io import StringIO

import pytest

from django.core.management import call_command

from cases.models import (
    Case,
    CaseEntityRelationship,
    DocumentSource,
    JawafEntity,
    RelationshipType,
    SourceType,
)


@pytest.fixture
def data_dir(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    return d


@pytest.fixture
def ag_index_csv(data_dir):
    path = data_dir / "ag_index.csv"
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(
            ["case_number", "title", "filing_date", "pdf_url", "court_office"]
        )
        writer.writerow(
            [
                "081-CR-0002",
                "Test AG Charge Sheet",
                "2081-03-29",
                "https://ag.gov.np/storage/abhiyogPatra/test.pdf",
                "Special Court",
            ]
        )
        writer.writerow(
            [
                "081-CR-0099",
                "Another Case",
                "2081-04-15",
                "https://ag.gov.np/storage/abhiyogPatra/other.pdf",
                "District Court",
            ]
        )
    return path


@pytest.fixture
def pr_index_csv(data_dir):
    path = data_dir / "ciaa-press-releases.csv"
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["press_id", "publication_date", "title", "source_url"])
        writer.writerow(
            [
                "3345",
                "2081-12-15",
                "प्रतिवादी राम शर्मा समेत रहेको भ्रष्टाचार मुद्दा",
                "https://ciaa.gov.np/pressrelease/3345",
            ]
        )
        writer.writerow(
            [
                "3400",
                "2082-01-10",
                "भ्रष्टाचार मुद्दाको अनुसन्धान",
                "https://ciaa.gov.np/pressrelease/3400",
            ]
        )
    return path


@pytest.fixture
def draft_case_with_court_case(db):
    case = Case.objects.create(
        case_type="CORRUPTION",
        state="DRAFT",
        title="Test Case",
        court_cases=["special:081-CR-0002"],
        evidence=[],
    )
    return case


@pytest.fixture
def draft_case_with_press_release(db):
    source = DocumentSource.objects.create(
        title="CIAA Press Release",
        source_type=SourceType.LEGAL_PROCEDURAL,
        url=["https://ciaa.gov.np/pressrelease/3345"],
    )
    case = Case.objects.create(
        case_type="CORRUPTION",
        state="DRAFT",
        title="Test Case with PR",
        court_cases=["special:081-CR-0002"],
        evidence=[{"source_id": source.source_id, "description": "CIAA Press Release"}],
    )
    return case, source


@pytest.fixture
def draft_case_with_defendant(db):
    entity = JawafEntity.objects.create(display_name="राम शर्मा")
    case = Case.objects.create(
        case_type="CORRUPTION",
        state="DRAFT",
        title="Test Case with Defendant",
        court_cases=["special:081-CR-0099"],
        evidence=[],
    )
    CaseEntityRelationship.objects.create(
        case=case,
        entity=entity,
        relationship_type=RelationshipType.ACCUSED,
    )
    return case


class TestDataLoading:
    def test_missing_data_dir(self):
        out = StringIO()
        call_command(
            "enrich_ciaa_sources",
            "--data-dir=/nonexistent/path",
            stdout=out,
        )
        output = out.getvalue()
        assert "not found" in output

    def test_missing_ag_index(self, tmp_path):
        d = tmp_path / "empty"
        d.mkdir()
        out = StringIO()
        call_command(
            "enrich_ciaa_sources",
            f"--data-dir={d}",
            stdout=out,
        )
        output = out.getvalue()
        assert "AG index not found" in output


class TestAGChargeSheet:
    def test_creates_ag_source(
        self, data_dir, ag_index_csv, pr_index_csv, draft_case_with_court_case
    ):
        out = StringIO()
        call_command(
            "enrich_ciaa_sources",
            f"--data-dir={data_dir}",
            "--skip-press-releases",
            stdout=out,
        )
        output = out.getvalue()

        assert "Created AG charge sheet source" in output
        assert "Cases enriched:     1" in output

        case = Case.objects.get(pk=draft_case_with_court_case.pk)
        assert len(case.evidence) == 1
        source_id = case.evidence[0]["source_id"]
        source = DocumentSource.objects.get(source_id=source_id)
        assert source.source_type == SourceType.LEGAL_PROCEDURAL
        assert "https://ag.gov.np/storage/abhiyogPatra/test.pdf" in source.url
        assert "AG Charge Sheet" in source.title

    def test_idempotent_skips_existing_ag_source(
        self, data_dir, ag_index_csv, pr_index_csv, draft_case_with_court_case
    ):
        call_command(
            "enrich_ciaa_sources",
            f"--data-dir={data_dir}",
            "--skip-press-releases",
        )

        out = StringIO()
        call_command(
            "enrich_ciaa_sources",
            f"--data-dir={data_dir}",
            "--skip-press-releases",
            stdout=out,
        )
        output = out.getvalue()

        assert "AG charge sheet already attached" in output
        assert "No new sources to attach" in output

        case = Case.objects.get(pk=draft_case_with_court_case.pk)
        assert len(case.evidence) == 1

    def test_dry_run_does_not_persist(
        self, data_dir, ag_index_csv, pr_index_csv, draft_case_with_court_case
    ):
        out = StringIO()
        call_command(
            "enrich_ciaa_sources",
            f"--data-dir={data_dir}",
            "--dry-run",
            "--skip-press-releases",
            stdout=out,
        )
        output = out.getvalue()

        assert "[DRY RUN] Would create AG charge sheet source" in output
        assert "This was a dry run" in output

        case = Case.objects.get(pk=draft_case_with_court_case.pk)
        assert not case.evidence

    def test_limit_restricts_case_count(self, data_dir, ag_index_csv, pr_index_csv, db):
        for i in range(5):
            Case.objects.create(
                case_type="CORRUPTION",
                state="DRAFT",
                title=f"Test Case {i}",
                court_cases=[f"special:081-CR-{i:04d}"],
                evidence=[],
            )

        out = StringIO()
        call_command(
            "enrich_ciaa_sources",
            f"--data-dir={data_dir}",
            "--limit=2",
            "--skip-press-releases",
            stdout=out,
        )
        output = out.getvalue()
        assert "Found 2 DRAFT case(s)" in output

    def test_case_id_filters_single_case(
        self, data_dir, ag_index_csv, pr_index_csv, draft_case_with_court_case, db
    ):
        Case.objects.create(
            case_type="CORRUPTION",
            state="DRAFT",
            title="Other Case",
            court_cases=["special:081-CR-0099"],
            evidence=[],
        )

        out = StringIO()
        call_command(
            "enrich_ciaa_sources",
            f"--data-dir={data_dir}",
            f"--case-id={draft_case_with_court_case.case_id}",
            "--skip-press-releases",
            stdout=out,
        )
        output = out.getvalue()
        assert "Found 1 DRAFT case(s)" in output


class TestPressReleaseEnrichment:
    def test_enriches_existing_pr_metadata(
        self, data_dir, ag_index_csv, pr_index_csv, draft_case_with_press_release
    ):
        case, source = draft_case_with_press_release
        assert source.publication_date is None
        assert source.title == "CIAA Press Release"

        out = StringIO()
        call_command(
            "enrich_ciaa_sources",
            f"--data-dir={data_dir}",
            stdout=out,
        )
        output = out.getvalue()

        assert "Enriched existing source" in output

        source.refresh_from_db()
        assert source.publication_date is not None
        assert source.title != "CIAA Press Release"

    def test_discovers_pr_by_defendant_name(
        self, data_dir, ag_index_csv, pr_index_csv, draft_case_with_defendant
    ):
        out = StringIO()
        call_command(
            "enrich_ciaa_sources",
            f"--data-dir={data_dir}",
            stdout=out,
        )
        output = out.getvalue()

        assert "Created press release source" in output
        assert "Cases enriched:     1" in output

        case = Case.objects.get(pk=draft_case_with_defendant.pk)
        assert len(case.evidence) >= 1

        source_ids = [e["source_id"] for e in case.evidence]
        sources = DocumentSource.objects.filter(source_id__in=source_ids)

        pr_source = None
        for s in sources:
            if (
                isinstance(s.url, list)
                and "https://ciaa.gov.np/pressrelease/3345" in s.url
            ):
                pr_source = s
                break
        assert pr_source is not None
        assert pr_source.source_type == SourceType.LEGAL_PROCEDURAL

    def test_skip_press_releases_flag(
        self, data_dir, ag_index_csv, pr_index_csv, draft_case_with_press_release
    ):
        case, source = draft_case_with_press_release

        out = StringIO()
        call_command(
            "enrich_ciaa_sources",
            f"--data-dir={data_dir}",
            "--skip-press-releases",
            stdout=out,
        )
        source.refresh_from_db()
        assert source.publication_date is None

    def test_pr_dry_run(
        self, data_dir, ag_index_csv, pr_index_csv, draft_case_with_defendant
    ):
        out = StringIO()
        call_command(
            "enrich_ciaa_sources",
            f"--data-dir={data_dir}",
            "--dry-run",
            stdout=out,
        )
        output = out.getvalue()

        assert "[DRY RUN] Would create press release source" in output
        assert "This was a dry run" in output

        case = Case.objects.get(pk=draft_case_with_defendant.pk)
        assert not case.evidence


class TestPublicationDateParsing:
    def test_parse_bs_date_returns_ad_date(
        self, data_dir, ag_index_csv, pr_index_csv, draft_case_with_court_case
    ):
        out = StringIO()
        call_command(
            "enrich_ciaa_sources",
            f"--data-dir={data_dir}",
            "--skip-press-releases",
            stdout=out,
        )

        case = Case.objects.get(pk=draft_case_with_court_case.pk)
        source_id = case.evidence[0]["source_id"]
        source = DocumentSource.objects.get(source_id=source_id)

        assert source.publication_date is not None
        assert isinstance(source.publication_date, date)
