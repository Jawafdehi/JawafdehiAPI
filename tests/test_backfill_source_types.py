"""Tests for backfill_source_types management command."""

from io import StringIO

import pytest
from django.core.management import call_command

from cases.models import DocumentSource, SourceType


@pytest.fixture
def sources(db):
    """Create test sources with NULL source_type for each rule scenario."""
    objs = []

    # Rule 1: CIAA Press Release → OFFICIAL_GOVERNMENT
    objs.append(
        DocumentSource.objects.create(
            source_id="t:r1-ciaa-pr",
            title="CIAA Press Release 3173",
            description="Corruption case update",
            url=["https://ciaa.gov.np/pressrelease/3173"],
        )
    )

    # Rule 1 also handles sub-paths
    objs.append(
        DocumentSource.objects.create(
            source_id="t:r1-ciaa-pr-sub",
            title="Another press release",
            description="",
            url=["https://ciaa.gov.np/pressrelease/1234/"],
        )
    )

    # Rule 2: NGM Court Orders → LEGAL_COURT_ORDER
    objs.append(
        DocumentSource.objects.create(
            source_id="t:r2-ngm-court",
            title="Supreme Court order",
            description="Court verdict document",
            url=["https://ngm-store.jawafdehi.org/court/2024/123.pdf"],
        )
    )

    # Rule 2 also matches NGM store without /court/ prefix
    objs.append(
        DocumentSource.objects.create(
            source_id="t:r2-ngm-other",
            title="NGM document",
            description="Some document from NGM store",
            url=["https://ngm-store.jawafdehi.org/uploads/other/doc.pdf"],
        )
    )

    # Rule 3: CIAA Procedural → LEGAL_PROCEDURAL
    objs.append(
        DocumentSource.objects.create(
            source_id="t:r3-arrest",
            title="Arrest report",
            description="CIAA arrests official in corruption case",
            url=["https://example.com/arrest.pdf"],
        )
    )

    # Rule 3 Nepali keyword
    objs.append(
        DocumentSource.objects.create(
            source_id="t:r3-pakrau",
            title="पक्राउ प्रतिवेदन",
            description="अख्तियारले अधिकारीलाई पक्राउ गरेको",
            url=["https://example.com/doc.pdf"],
        )
    )

    # Rule 4: Financial/Forensic → FINANCIAL_FORENSIC
    objs.append(
        DocumentSource.objects.create(
            source_id="t:r4-audit",
            title="Audit Report 2080/81",
            description="Financial audit of municipal office",
            url=["https://example.com/audit.pdf"],
        )
    )

    # Rule 5: Media/News → MEDIA_NEWS
    objs.append(
        DocumentSource.objects.create(
            source_id="t:r5-ekantipur",
            title="News article",
            description="Daily news update",
            url=["https://ekantipur.com/news/123.html"],
        )
    )

    objs.append(
        DocumentSource.objects.create(
            source_id="t:r5-bbc",
            title="BBC News",
            description="International coverage",
            url=["https://www.bbc.com/news/world-asia-123"],
        )
    )

    # Rule 6: Investigative Reports → INVESTIGATIVE_REPORT
    objs.append(
        DocumentSource.objects.create(
            source_id="t:r6-investigative",
            title="Special investigation report",
            description="Probe into corruption ring",
            url=["https://example.com/probe.pdf"],
        )
    )

    # Rule 7: Public Complaint → PUBLIC_COMPLAINT
    objs.append(
        DocumentSource.objects.create(
            source_id="t:r7-complaint",
            title="Whistleblower complaint",
            description="Tip-off about irregularities",
            url=["https://example.com/complaint.pdf"],
        )
    )

    # Rule 7 Nepali keyword
    objs.append(
        DocumentSource.objects.create(
            source_id="t:r7-ujuri",
            title="उजुरी",
            description="सूचनाको आधारमा अनुसन्धान",
            url=["https://example.com/doc.pdf"],
        )
    )

    # Rule 8: Legislative/Policy → LEGISLATIVE_DOC
    objs.append(
        DocumentSource.objects.create(
            source_id="t:r8-policy",
            title="New policy bill 2081",
            description="Policy document for new regulation",
            url=["https://lawcommission.gov.np/bill.pdf"],
        )
    )

    # Rule 9: Social Media → SOCIAL_MEDIA
    objs.append(
        DocumentSource.objects.create(
            source_id="t:r9-facebook",
            title="Social post",
            description="Facebook screenshot",
            url=["https://facebook.com/posts/123"],
        )
    )

    objs.append(
        DocumentSource.objects.create(
            source_id="t:r9-youtube",
            title="YouTube video",
            description="Video evidence",
            url=["https://www.youtube.com/watch?v=abc"],
        )
    )

    # Rule 10: Internal Corporate → INTERNAL_CORPORATE
    objs.append(
        DocumentSource.objects.create(
            source_id="t:r10-email",
            title="Internal email",
            description="Board meeting minutes",
            url=["https://example.com/email.pdf"],
        )
    )

    # Rule 11: Fallback → OTHER_VISUAL
    objs.append(
        DocumentSource.objects.create(
            source_id="t:r11-fallback",
            title="Unrecognized document",
            description="Some random document with no clear type",
            url=["https://example.com/file.pdf"],
        )
    )

    # Source with no URLs at all, no identifiable keywords → fallback
    objs.append(
        DocumentSource.objects.create(
            source_id="t:r11-no-urls",
            title="Generic file",
            description="Some uploaded image",
            url=[],
        )
    )

    return objs


def test_classify_all_rules(sources):
    """All 11 rules classify correctly in one dry-run pass."""
    out = StringIO()
    call_command(
        "backfill_source_types",
        dry_run=True,
        verbose=True,
        allow_production=True,
        stdout=out,
    )
    output = out.getvalue()

    # Rule 1
    assert "[DRY-RUN] t:r1-ciaa-pr: → OFFICIAL_GOVERNMENT" in output
    assert "[DRY-RUN] t:r1-ciaa-pr-sub: → OFFICIAL_GOVERNMENT" in output

    # Rule 2
    assert "[DRY-RUN] t:r2-ngm-court: → LEGAL_COURT_ORDER" in output
    assert "[DRY-RUN] t:r2-ngm-other: → LEGAL_COURT_ORDER" in output

    # Rule 3
    assert "[DRY-RUN] t:r3-arrest: → LEGAL_PROCEDURAL" in output
    assert "[DRY-RUN] t:r3-pakrau: → LEGAL_PROCEDURAL" in output

    # Rule 4
    assert "[DRY-RUN] t:r4-audit: → FINANCIAL_FORENSIC" in output

    # Rule 5
    assert "[DRY-RUN] t:r5-ekantipur: → MEDIA_NEWS" in output
    assert "[DRY-RUN] t:r5-bbc: → MEDIA_NEWS" in output

    # Rule 6
    assert "[DRY-RUN] t:r6-investigative: → INVESTIGATIVE_REPORT" in output

    # Rule 7
    assert "[DRY-RUN] t:r7-complaint: → PUBLIC_COMPLAINT" in output
    assert "[DRY-RUN] t:r7-ujuri: → PUBLIC_COMPLAINT" in output

    # Rule 8
    assert "[DRY-RUN] t:r8-policy: → LEGISLATIVE_DOC" in output

    # Rule 9
    assert "[DRY-RUN] t:r9-facebook: → SOCIAL_MEDIA" in output
    assert "[DRY-RUN] t:r9-youtube: → SOCIAL_MEDIA" in output

    # Rule 10
    assert "[DRY-RUN] t:r10-email: → INTERNAL_CORPORATE" in output

    # Rule 11
    assert "[DRY-RUN] t:r11-fallback: → OTHER_VISUAL" in output
    assert "[DRY-RUN] t:r11-no-urls: → OTHER_VISUAL" in output


def test_dry_run_does_not_persist(sources):
    """In dry-run mode no source_type should be modified in DB."""
    out = StringIO()
    call_command(
        "backfill_source_types", dry_run=True, allow_production=True, stdout=out
    )

    null_count = DocumentSource.objects.filter(source_type__isnull=True).count()
    assert null_count == len(sources)


def test_live_run_persists(sources):
    """In live mode source_type should be saved to DB."""
    out = StringIO()
    call_command("backfill_source_types", allow_production=True, stdout=out)

    null_count = DocumentSource.objects.filter(source_type__isnull=True).count()
    assert null_count == 0

    # Spot-check a few
    assert (
        DocumentSource.objects.get(source_id="t:r1-ciaa-pr").source_type
        == SourceType.OFFICIAL_GOVERNMENT
    )
    assert (
        DocumentSource.objects.get(source_id="t:r2-ngm-court").source_type
        == SourceType.LEGAL_COURT_ORDER
    )
    assert (
        DocumentSource.objects.get(source_id="t:r5-ekantipur").source_type
        == SourceType.MEDIA_NEWS
    )
    assert (
        DocumentSource.objects.get(source_id="t:r11-fallback").source_type
        == SourceType.OTHER_VISUAL
    )


def test_limit(sources):
    """--limit N processes at most N sources."""
    out = StringIO()
    call_command(
        "backfill_source_types",
        dry_run=True,
        limit=3,
        allow_production=True,
        stdout=out,
    )

    lines = out.getvalue().splitlines()
    classified = sum(1 for line in lines if "[DRY-RUN]" in line)
    assert classified <= 3


def test_source_id(sources):
    """--source-id processes only the specified source."""
    out = StringIO()
    call_command(
        "backfill_source_types",
        dry_run=True,
        source_id="t:r4-audit",
        verbose=True,
        allow_production=True,
        stdout=out,
    )
    output = out.getvalue()
    assert "[DRY-RUN] t:r4-audit: → FINANCIAL_FORENSIC" in output
    # Other sources should not appear
    assert "[DRY-RUN] t:r1-ciaa-pr:" not in output


def test_skips_already_classified_sources(db):
    """Sources with non-null source_type are excluded."""
    DocumentSource.objects.create(
        source_id="t:already-set",
        title="Already classified",
        description="",
        url=[],
        source_type=SourceType.OFFICIAL_GOVERNMENT,
    )
    out = StringIO()
    call_command(
        "backfill_source_types",
        dry_run=True,
        verbose=True,
        allow_production=True,
        stdout=out,
    )
    assert "No sources found with NULL source_type." in out.getvalue()


def test_verbose_output(sources):
    """--verbose produces per-source log lines."""
    out = StringIO()
    call_command(
        "backfill_source_types",
        dry_run=True,
        verbose=True,
        allow_production=True,
        stdout=out,
    )
    assert "[DRY-RUN]" in out.getvalue()


def test_non_verbose_output(sources):
    """Without --verbose, no per-source lines."""
    out = StringIO()
    call_command(
        "backfill_source_types",
        dry_run=True,
        verbose=False,
        allow_production=True,
        stdout=out,
    )
    assert "[DRY-RUN]" not in out.getvalue()


def test_priority_order(sources):
    """When multiple rules could match, the higher-priority one wins.

    A source with a CIAA press release URL + keywords matching a lower-priority
    rule should still get OFFICIAL_GOVERNMENT via Rule 1.
    """
    # Has CIAA press release URL + keyword matching Rule 3/7
    DocumentSource.objects.create(
        source_id="t:priority-test",
        title="Complaint about arrest",
        description="Whistleblower tip-off",
        url=["https://ciaa.gov.np/pressrelease/9999"],
    )
    out = StringIO()
    call_command(
        "backfill_source_types",
        dry_run=True,
        verbose=True,
        allow_production=True,
        stdout=out,
    )
    assert "[DRY-RUN] t:priority-test: → OFFICIAL_GOVERNMENT" in out.getvalue()


def test_summary_line(sources):
    """Dry-run output contains summary with breakdown."""
    out = StringIO()
    call_command(
        "backfill_source_types", dry_run=True, allow_production=True, stdout=out
    )
    assert "Dry-run=True" in out.getvalue()
    assert "Breakdown:" in out.getvalue()
    assert "OFFICIAL_GOVERNMENT:" in out.getvalue()
