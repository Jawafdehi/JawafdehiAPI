"""Tests for the Stage 2 merge (``materials.dedup_merge``).

Covers repoint, collision-dedupe (the ``unique_case_material_reference`` case),
note preservation, idempotency, the save()-not-update() soft-delete (so the
search-eviction signal fires), and the deliberate NON-recompute of the canonical's
visibility. Runs on sqlite across the default (cases) + ngm (materials) DBs.
See docs/superpowers/specs/2026-07-14-jawafdehi-dedup-audit-design.md (Stage 2).
"""

from __future__ import annotations

from datetime import timedelta
from unittest import mock

import pytest
from django.utils import timezone

from cases.models import Case, CaseMaterialReference, CaseState, CaseType
from materials.dedup_merge import apply_merge, plan_merge
from materials.models import Material, Visibility

pytestmark = pytest.mark.django_db(databases=["default", "ngm"])

BASE = "https://jawafdehi.org"


def _jawaf(ident="20260507.pr"):
    iri = f"{BASE}/material/jawafdehi/{ident}"
    return Material.objects.create(
        iri=iri, material_type="document", source="jawafdehi", ident=ident,
        data={"@id": iri, "@type": "DigitalDocument", "name": {"ne": "upload"}},
    )


def _canonical(ident="3155", source="ciaa_press_release"):
    iri = f"{BASE}/material/{source}/{ident}"
    return Material.objects.create(
        iri=iri, material_type="document", source=source, ident=ident,
        data={"@id": iri, "@type": "CreativeWork", "name": {"ne": "canonical"}},
    )


def _case(slug, state=CaseState.PUBLISHED):
    return Case.objects.create(
        case_type=CaseType.CORRUPTION, state=state, title=slug, slug=slug
    )


def _ref(case, iri, note="", ordinal=0):
    return CaseMaterialReference.objects.create(
        case=case, material_iri=iri, additional_details=note, ordinal=ordinal
    )


def test_repoint_no_collision_preserves_note_and_ordinal():
    j, c = _jawaf(), _canonical()
    case = _case("case-a")
    _ref(case, j.iri, note="why this matters here", ordinal=3)

    result = apply_merge(j.iri, c.iri)

    assert result.refs_repointed == 1
    assert result.refs_deduped == 0
    assert result.soft_deleted is True
    ref = CaseMaterialReference.objects.get(case=case)
    assert ref.material_iri == c.iri
    assert ref.additional_details == "why this matters here"
    assert ref.ordinal == 3
    assert Material.objects.get(pk=j.iri).is_deleted is True


def test_collision_dedupes_and_merges_note():
    j, c = _jawaf(), _canonical()
    case = _case("case-b")
    _ref(case, c.iri, note="canonical note")
    _ref(case, j.iri, note="jawafdehi note")

    result = apply_merge(j.iri, c.iri)

    assert result.refs_repointed == 0
    assert result.refs_deduped == 1
    # Exactly one (case, canonical) ref remains; the jawafdehi ref is gone.
    remaining = CaseMaterialReference.objects.filter(case=case)
    assert remaining.count() == 1
    ref = remaining.get()
    assert ref.material_iri == c.iri
    assert "canonical note" in ref.additional_details
    assert "jawafdehi note" in ref.additional_details


def test_soft_delete_goes_through_save_not_update():
    # .update() bypasses auto_now; a real save() bumps updated_at. Pin updated_at
    # to the past, then assert apply_merge advanced it -> proves the save() path
    # (which is what fires the post_save search-eviction signal).
    j, c = _jawaf(), _canonical()
    old = timezone.now() - timedelta(days=1)
    Material.objects.filter(pk=j.iri).update(updated_at=old)

    apply_merge(j.iri, c.iri)

    assert Material.objects.get(pk=j.iri).updated_at > old


def test_soft_delete_evicts_from_search(django_capture_on_commit_callbacks):
    j, c = _jawaf(), _canonical()
    _ref(_case("case-c"), j.iri)
    with mock.patch("materials.signals.search_index.delete") as del_mock, \
            django_capture_on_commit_callbacks(execute=True):
        apply_merge(j.iri, c.iri)
    del_mock.assert_called()


def test_canonical_visibility_untouched_by_draft_referrer():
    # The canonical is public corpus (LISTED). Repointing a DRAFT case's evidence
    # onto it must NOT demote it (that would hide a public press release).
    j, c = _jawaf(), _canonical()
    assert c.visibility == Visibility.LISTED
    _ref(_case("case-draft", state=CaseState.DRAFT), j.iri)

    apply_merge(j.iri, c.iri)

    assert Material.objects.get(pk=c.iri).visibility == Visibility.LISTED


def test_apply_is_idempotent():
    j, c = _jawaf(), _canonical()
    _ref(_case("case-d"), j.iri)

    first = apply_merge(j.iri, c.iri)
    second = apply_merge(j.iri, c.iri)

    assert first.soft_deleted is True
    assert second.soft_deleted is False
    assert second.refs_repointed == 0
    assert second.refs_deduped == 0
    # Still exactly one canonical ref, no duplicates minted.
    assert CaseMaterialReference.objects.filter(material_iri=c.iri).count() == 1


def test_plan_merge_previews_repoint_and_collision():
    j, c = _jawaf(), _canonical()
    repoint_case = _case("case-repoint")
    _ref(repoint_case, j.iri)
    collide_case = _case("case-collide")
    _ref(collide_case, c.iri)
    _ref(collide_case, j.iri)

    plan = plan_merge(j.iri, c.iri)

    assert plan.refs_to_repoint == ["case-repoint"]
    assert plan.collisions == ["case-collide"]
    # plan_merge is read-only.
    assert Material.objects.get(pk=j.iri).is_deleted is False
