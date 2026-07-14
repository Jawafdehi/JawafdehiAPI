"""Audit capture for bulk ``QuerySet.update()`` / ``bulk_update()`` writes.

These exercise the ORM-level bypass closure (``jawafdehi_shared.db.audited``):
django-auditlog only hooks ``save()`` / ``delete()``, so a bulk ``update()`` — the
pod-ORM / management-command / backfill pattern — used to vanish from the trail.
The audited manager now logs those, across all three databases, honoring each
model's registered include/exclude/mask config and bounding huge writes.
"""

import pytest
from auditlog.models import LogEntry
from django.contrib.contenttypes.models import ContentType
from django.test import override_settings

from cases.models import Case, CaseState, CaseType


def _make_case(**kwargs) -> Case:
    defaults = dict(
        title="Bulk case",
        case_type=CaseType.CORRUPTION,
        state=CaseState.DRAFT,
        description="Original",
        short_description="Short",
    )
    defaults.update(kwargs)
    return Case.objects.create(**defaults)


def _updates_for(instance):
    return LogEntry.objects.get_for_object(instance).filter(
        action=LogEntry.Action.UPDATE
    )


# ---------------------------------------------------------------------------
# Core: a bare QuerySet.update() is logged (the pod-ORM path)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_bare_queryset_update_logs_scoped_entry():
    """``Model.objects.filter(...).update(...)`` outside any request is logged."""
    case = _make_case(description="Original")

    before = _updates_for(case).count()
    Case.objects.filter(pk=case.pk).update(description="Amended")

    entries = _updates_for(case)
    assert entries.count() == before + 1
    entry = entries.first()
    assert entry.changes["description"] == ["Original", "Amended"]
    # No HTTP request/context -> honest NULL actor (not a fabricated one).
    assert entry.actor_id is None


@pytest.mark.django_db
def test_update_of_only_auto_now_touch_column_is_not_logged():
    """A pure ``updated_at`` bump is not a substantive edit -> no entry."""
    from django.utils import timezone

    case = _make_case()
    before = _updates_for(case).count()
    Case.objects.filter(pk=case.pk).update(updated_at=timezone.now())
    assert _updates_for(case).count() == before


@pytest.mark.django_db
def test_no_op_update_creates_no_entry():
    """Writing the same value back yields an empty diff -> no entry."""
    case = _make_case(description="Same")
    before = _updates_for(case).count()
    Case.objects.filter(pk=case.pk).update(description="Same")
    assert _updates_for(case).count() == before


@pytest.mark.django_db
def test_disable_auditlog_suppresses_bulk_capture():
    """Bulk loads wrap writes in ``disable_auditlog()`` (e.g. the courts importer);
    the audited manager must honor that flag and log nothing."""
    from auditlog.context import disable_auditlog

    case = _make_case(description="orig")
    before = _updates_for(case).count()
    with disable_auditlog():
        Case.objects.filter(pk=case.pk).update(description="loaded in bulk")
    assert _updates_for(case).count() == before


@pytest.mark.django_db
def test_multi_row_update_logs_one_entry_per_changed_row():
    a = _make_case(description="a")
    b = _make_case(description="b")
    Case.objects.filter(pk__in=[a.pk, b.pk]).update(description="shared")
    assert _updates_for(a).count() == 1
    assert _updates_for(b).count() == 1


@pytest.mark.django_db
def test_audit_composes_with_case_slug_history_override():
    """``Case`` has its own ``CaseQuerySet.update()`` (records slug history). The
    audit hook must WEAVE IN, not clobber it — a bulk re-slug does both."""
    from cases.models import CaseSlugHistory

    case = _make_case(state=CaseState.PUBLISHED)
    old_slug = case.slug
    Case.objects.filter(pk=case.pk).update(slug="reslugged-audit-xyz")

    # Slug-history behavior (CaseQuerySet.update) preserved …
    assert CaseSlugHistory.objects.filter(slug=old_slug, case=case).exists()
    # … and the audit entry (AuditedQuerySet.update) is added.
    entry = _updates_for(case).first()
    assert entry is not None
    assert entry.changes["slug"][1] == "reslugged-audit-xyz"


@pytest.mark.django_db
def test_bulk_update_method_is_logged():
    a = _make_case(description="a")
    b = _make_case(description="b")
    a.description = "a2"
    b.description = "b2"
    Case.objects.bulk_update([a, b], ["description"])
    assert _updates_for(a).first().changes["description"] == ["a", "a2"]
    assert _updates_for(b).first().changes["description"] == ["b", "b2"]


# ---------------------------------------------------------------------------
# Volume guard: a very large update collapses to one summary entry
# ---------------------------------------------------------------------------


@pytest.mark.django_db
@override_settings(AUDIT_BULK_UPDATE_MAX_ROWS=2)
def test_large_update_writes_single_summary_entry():
    cases = [_make_case(description=f"c{i}") for i in range(3)]
    ct = ContentType.objects.get_for_model(Case)
    before = LogEntry.objects.filter(
        action=LogEntry.Action.UPDATE, content_type=ct
    ).count()

    Case.objects.filter(pk__in=[c.pk for c in cases]).update(description="mass")

    entries = LogEntry.objects.filter(action=LogEntry.Action.UPDATE, content_type=ct)
    # One summary row, not three per-row rows.
    assert entries.count() == before + 1
    summary = entries.order_by("-id").first().changes["__bulk_update__"]
    assert summary["rows"] == 3
    assert summary["fields"] == ["description"]


# ---------------------------------------------------------------------------
# Deletes must keep logging (we deliberately do NOT override delete())
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_queryset_delete_still_logs():
    case = _make_case()
    ct = ContentType.objects.get_for_model(Case)
    pk = case.pk  # integer PK -> auditlog indexes it in object_id
    Case.objects.filter(pk=case.pk).delete()
    assert LogEntry.objects.filter(
        action=LogEntry.Action.DELETE, content_type=ct, object_id=pk
    ).exists()


# ---------------------------------------------------------------------------
# PII masking on a registered default-DB model
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_feedback_pii_fields_are_masked_in_diff():
    from cases.models import Feedback, FeedbackType

    fb = Feedback.objects.create(
        feedback_type=FeedbackType.BUG,
        subject="s",
        description="secret personal detail",
        contact_info={"email": "someone@example.com"},
    )
    Feedback.objects.filter(pk=fb.pk).update(description="another secret detail")
    entry = _updates_for(fb).first()
    assert entry is not None
    # The field is tracked (it appears), but its values are redaction markers,
    # not the raw personal text.
    masked = entry.changes["description"]
    assert "another secret detail" not in masked
    assert "secret personal detail" not in masked


# ---------------------------------------------------------------------------
# Cross-DB: nes (entities) — long IRI PK + LogEntry lands in default
# ---------------------------------------------------------------------------


@pytest.mark.django_db(databases=["default", "nes"])
def test_entity_update_logs_cross_db_with_long_iri_pk():
    from entities.models import StoredEntity

    long_iri = "https://jawafdehi.org/entity/person/" + "a" * 300  # > 255 chars
    ent = StoredEntity.objects.create(
        iri=long_iri,
        entity_type="Person",
        prefix="person",
        slug="a" * 300,
        data={"@id": long_iri},
    )

    StoredEntity.objects.filter(pk=long_iri).update(entity_type="Organization")

    ct = ContentType.objects.get_for_model(StoredEntity)
    entry = (
        LogEntry.objects.filter(
            action=LogEntry.Action.UPDATE, content_type=ct, object_pk=long_iri
        )
        .order_by("-id")
        .first()
    )
    assert entry is not None, "cross-DB LogEntry for the entity update was not written"
    assert entry.object_pk == long_iri  # full IRI preserved, not truncated
    assert entry.changes["entity_type"] == ["Person", "Organization"]
    _ = ent  # keep the created row referenced


# ---------------------------------------------------------------------------
# Cross-DB: ngm (materials) — derived ``visibility`` is excluded from the trail
# ---------------------------------------------------------------------------


@pytest.mark.django_db(databases=["default", "ngm"])
def test_material_visibility_change_is_not_logged_but_content_is():
    from materials.models import Material, Visibility

    iri = "https://jawafdehi.org/material/ciaa/case-0001-chargesheet"
    mat = Material.objects.create(
        iri=iri,
        material_type="charge_sheet",
        source="ciaa",
        ident="case-0001-chargesheet",
        data={"title": "orig"},
    )
    ct = ContentType.objects.get_for_model(Material)

    # System-derived visibility recompute -> excluded -> no entry.
    before = LogEntry.objects.filter(
        action=LogEntry.Action.UPDATE, content_type=ct, object_pk=iri
    ).count()
    Material.objects.filter(pk=iri).update(visibility=Visibility.UNLISTED)
    assert (
        LogEntry.objects.filter(
            action=LogEntry.Action.UPDATE, content_type=ct, object_pk=iri
        ).count()
        == before
    )

    # A human/content edit IS logged.
    Material.objects.filter(pk=iri).update(source="ag")
    entry = (
        LogEntry.objects.filter(
            action=LogEntry.Action.UPDATE, content_type=ct, object_pk=iri
        )
        .order_by("-id")
        .first()
    )
    assert entry is not None
    assert entry.changes["source"] == ["ciaa", "ag"]
    _ = mat
