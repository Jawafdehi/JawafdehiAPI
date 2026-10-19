"""P0-1: publish reads the review verdict (review/gate.py).

warn (the default) records the verdict on the PUBLISHED CaseStateChange row and
never blocks. enforce blocks no/stale review and REJECT, and lets REVISE
through only with an X-Transition-Reason. off reads and records nothing.
"""

from datetime import timedelta

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone

from cases.models import CaseState, CaseStateChange
from review import gate
from review.models import CaseReview
from tests.api.test_case_state_transitions import (
    _authed_client,
    _patch_state,
    _publishable_case,
)
from tests.conftest import create_user_with_role


def _review(case, disposition, *, status=CaseReview.STATUS_DONE, completed=None):
    """A review of ``case``. Done reviews finish after the case's last save
    unless ``completed`` says otherwise."""
    done = status == CaseReview.STATUS_DONE
    return CaseReview.objects.create(
        case=case,
        status=status,
        result={
            "disposition": disposition,
            "overall_score": 72,
            "gate_failures": [{"rule": "g"}] if disposition == "REJECT" else [],
        }
        if done
        else None,
        completed_at=(completed or timezone.now() + timedelta(seconds=1))
        if done
        else None,
    )


def _publish_via_api(case, reason=None):
    user = create_user_with_role("gate-admin", "gate-admin@example.com", "Admin")
    client = _authed_client(user)
    if reason is None:
        return _patch_state(client, case, CaseState.PUBLISHED)
    return client.patch(
        f"/api/cases/{case.slug}/",
        data=[{"op": "replace", "path": "/state", "value": CaseState.PUBLISHED}],
        format="json",
        HTTP_X_TRANSITION_REASON=reason,
    )


def _publish_row(case):
    return CaseStateChange.objects.get(case=case, to_state=CaseState.PUBLISHED)


# --- latest_verdict ---------------------------------------------------------


@pytest.mark.django_db
def test_verdict_with_no_review_is_stale_and_empty():
    case = _publishable_case(state=CaseState.IN_REVIEW)
    v = gate.latest_verdict(case)
    assert v["disposition"] is None
    assert v["review_id"] is None
    assert v["stale"] is True


@pytest.mark.django_db
def test_verdict_reads_the_latest_finished_review():
    case = _publishable_case(state=CaseState.IN_REVIEW)
    _review(case, "REJECT", completed=timezone.now() + timedelta(seconds=1))
    latest = _review(case, "PASS", completed=timezone.now() + timedelta(seconds=2))
    v = gate.latest_verdict(case)
    assert v["disposition"] == "PASS"
    assert v["review_id"] == latest.pk
    assert v["stale"] is False
    assert v["newer_unfinished"] is None


@pytest.mark.django_db
def test_verdict_is_stale_when_the_case_was_saved_after_the_review():
    case = _publishable_case(state=CaseState.IN_REVIEW)
    _review(case, "PASS", completed=timezone.now() - timedelta(hours=1))
    case.save()
    assert gate.latest_verdict(case)["stale"] is True


@pytest.mark.django_db
def test_verdict_reports_a_newer_unfinished_review():
    case = _publishable_case(state=CaseState.IN_REVIEW)
    _review(case, "PASS")
    _review(case, None, status=CaseReview.STATUS_FAILED)
    v = gate.latest_verdict(case)
    assert v["disposition"] == "PASS"
    assert v["newer_unfinished"] == CaseReview.STATUS_FAILED


@pytest.mark.django_db
def test_verdict_ignores_an_unknown_disposition():
    case = _publishable_case(state=CaseState.IN_REVIEW)
    _review(case, "MAYBE")
    assert gate.latest_verdict(case)["disposition"] is None


# --- modes ------------------------------------------------------------------


def test_unknown_mode_falls_back_to_warn(settings):
    settings.REVIEW_GATE_MODE = "enfroce"
    assert gate.gate_mode() == gate.MODE_WARN


def test_default_mode_is_warn():
    assert gate.gate_mode() == gate.MODE_WARN


# --- warn: never blocks, always records --------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize("disposition", [None, "REJECT", "REVISE"])
def test_warn_publishes_and_records_what_enforce_would_block(settings, disposition):
    settings.REVIEW_GATE_MODE = "warn"
    case = _publishable_case(state=CaseState.IN_REVIEW)
    if disposition:
        _review(case, disposition)

    response = _publish_via_api(case)

    assert response.status_code == 200
    case.refresh_from_db()
    assert case.state == CaseState.PUBLISHED
    recorded = _publish_row(case).review_gate
    assert recorded["mode"] == "warn"
    assert recorded["disposition"] == disposition
    assert recorded["would_block"] is True
    assert recorded["block_reason"]


@pytest.mark.django_db
def test_warn_records_a_pass_as_not_blocking(settings):
    settings.REVIEW_GATE_MODE = "warn"
    case = _publishable_case(state=CaseState.IN_REVIEW)
    review = _review(case, "PASS")

    assert _publish_via_api(case).status_code == 200
    recorded = _publish_row(case).review_gate
    assert recorded["disposition"] == "PASS"
    assert recorded["review_id"] == review.pk
    assert recorded["would_block"] is False
    assert recorded["stale"] is False


@pytest.mark.django_db
def test_warn_records_the_revise_override_reason(settings):
    settings.REVIEW_GATE_MODE = "warn"
    case = _publishable_case(state=CaseState.IN_REVIEW)
    _review(case, "REVISE")

    assert _publish_via_api(case, reason="Checked the sources by hand").status_code == 200
    recorded = _publish_row(case).review_gate
    assert recorded["would_block"] is False
    assert recorded["override_reason"] == "Checked the sources by hand"


@pytest.mark.django_db
def test_only_the_publish_transition_carries_a_verdict(settings):
    settings.REVIEW_GATE_MODE = "warn"
    case = _publishable_case(state=CaseState.DRAFT)
    user = create_user_with_role("gate-mod", "gate-mod@example.com", "Moderator")
    _patch_state(_authed_client(user), case, CaseState.IN_REVIEW)
    row = CaseStateChange.objects.get(case=case, to_state=CaseState.IN_REVIEW)
    assert row.review_gate is None


# --- off ----------------------------------------------------------------------


@pytest.mark.django_db
def test_off_records_nothing(settings):
    settings.REVIEW_GATE_MODE = "off"
    case = _publishable_case(state=CaseState.IN_REVIEW)
    assert _publish_via_api(case).status_code == 200
    assert _publish_row(case).review_gate is None


# --- enforce ------------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize("disposition", [None, "REJECT", "REVISE"])
def test_enforce_blocks_with_422_and_leaves_the_case_unpublished(
    settings, disposition
):
    settings.REVIEW_GATE_MODE = "enforce"
    case = _publishable_case(state=CaseState.IN_REVIEW)
    if disposition:
        _review(case, disposition)

    response = _publish_via_api(case)

    assert response.status_code == 422
    assert "review" in response.data
    case.refresh_from_db()
    assert case.state == CaseState.IN_REVIEW
    assert not CaseStateChange.objects.filter(
        case=case, to_state=CaseState.PUBLISHED
    ).exists()


@pytest.mark.django_db
def test_enforce_blocks_reject_even_with_a_reason(settings):
    settings.REVIEW_GATE_MODE = "enforce"
    case = _publishable_case(state=CaseState.IN_REVIEW)
    _review(case, "REJECT")
    assert _publish_via_api(case, reason="I disagree").status_code == 422


@pytest.mark.django_db
def test_enforce_blocks_a_stale_pass(settings):
    settings.REVIEW_GATE_MODE = "enforce"
    case = _publishable_case(state=CaseState.IN_REVIEW)
    _review(case, "PASS", completed=timezone.now() - timedelta(hours=1))
    case.save()
    with pytest.raises(ValidationError):
        case.publish()


@pytest.mark.django_db
def test_enforce_allows_revise_with_a_reason(settings):
    settings.REVIEW_GATE_MODE = "enforce"
    case = _publishable_case(state=CaseState.IN_REVIEW)
    _review(case, "REVISE")
    assert _publish_via_api(case, reason="Sources re-checked").status_code == 200
    assert _publish_row(case).review_gate["override_reason"] == "Sources re-checked"


@pytest.mark.django_db
def test_enforce_allows_a_current_pass(settings):
    settings.REVIEW_GATE_MODE = "enforce"
    case = _publishable_case(state=CaseState.IN_REVIEW)
    _review(case, "PASS")
    assert _publish_via_api(case).status_code == 200
    case.refresh_from_db()
    assert case.state == CaseState.PUBLISHED
