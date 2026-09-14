# SPDX-License-Identifier: Hippocratic-3.0
"""The publish gate: ``Case.publish()`` obeys the review verdict.

Acceptance criteria (from JD Eval Report 2, P0-1), one test each:

* no completed review        -> refused (enforce)
* REJECT                     -> refused
* REVISE without a reason    -> refused
* REVISE with a reason       -> allowed, reason recorded in versionInfo + log
* PASS                       -> allowed, review id recorded
* case edited after review   -> refused as stale
* warn mode                  -> never refuses, logs the would-block
* off mode                   -> never consulted
* submit() enqueues exactly one review, and not a second while it is in flight
* the API path: X-Transition-Reason unlocks a REVISE publish and lands in the log

The suite-wide default is ``REVIEW_GATE_MODE = "off"`` (config/settings_test.py);
every test here sets the mode it means to exercise.
"""

import logging

import pytest
from django.core.exceptions import ValidationError
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from cases.models import (
    Case,
    CaseEntityRelationship,
    CaseState,
    CaseStateChange,
    CaseType,
    RelationshipType,
)
from review.gate import evaluate_publish_gate, latest_verdict
from review.models import CaseReview
from tests.byline import credit_author
from tests.conftest import create_user_with_role

ENFORCE = override_settings(REVIEW_GATE_MODE="enforce")
WARN = override_settings(REVIEW_GATE_MODE="warn")


def _publishable_case(state=CaseState.IN_REVIEW, **kwargs) -> Case:
    defaults = dict(
        title="Gate case",
        offence_type=CaseType.CORRUPTION,
        state=state,
        description="Detailed allegation description",
        short_description="Short",
        key_allegations=["Primary allegation"],
    )
    defaults.update(kwargs)
    case = Case.objects.create(**defaults)
    CaseEntityRelationship.objects.create(
        case=case,
        nes_id="https://jawafdehi.org/entity/person/ram-prasad-gautam",
        relationship_type=RelationshipType.ACCUSED,
    )
    credit_author(case)
    return Case.objects.get(pk=case.pk)


def _done_review(case, disposition, *, overall=85, gate_failures=None, when=None):
    """A completed review whose started_at is AFTER the case's last save, so it
    is current unless a test edits the case afterwards."""
    now = when or timezone.now()
    return CaseReview.objects.create(
        case=case,
        status=CaseReview.STATUS_DONE,
        started_at=now,
        completed_at=now,
        result={
            "disposition": disposition,
            "overall_score": overall,
            "gate_failures": gate_failures or [],
        },
    )


# ---------------------------------------------------------------------------
# Model-level gate, enforce mode
# ---------------------------------------------------------------------------


@pytest.mark.django_db
@ENFORCE
def test_publish_refused_with_no_completed_review():
    case = _publishable_case()
    with pytest.raises(ValidationError) as exc:
        case.publish()
    assert "no completed review" in str(exc.value.message_dict["review"][0])
    case.refresh_from_db()
    assert case.state == CaseState.IN_REVIEW  # untouched


@pytest.mark.django_db
@ENFORCE
def test_failed_review_is_not_a_verdict():
    """A review the judge could not finish (status failed) must read as 'no
    completed review', never as a REJECT the caseworker cannot act on."""
    case = _publishable_case()
    CaseReview.objects.create(
        case=case, status=CaseReview.STATUS_FAILED, error="JudgeUnavailable"
    )
    assert latest_verdict(case)["review_id"] is None
    with pytest.raises(ValidationError):
        case.publish()


@pytest.mark.django_db
@ENFORCE
def test_publish_refused_on_reject_and_names_failed_gates():
    case = _publishable_case()
    _done_review(
        case,
        "REJECT",
        overall=41,
        gate_failures=[{"key": "accused_present", "title": "x", "score": 0, "gate_min": 100}],
    )
    with pytest.raises(ValidationError) as exc:
        case.publish()
    msg = exc.value.message_dict["review"][0]
    assert "REJECT" in msg and "accused_present" in msg


@pytest.mark.django_db
@ENFORCE
def test_publish_refused_on_revise_without_reason():
    case = _publishable_case()
    _done_review(case, "REVISE", overall=70)
    with pytest.raises(ValidationError) as exc:
        case.publish()
    assert "REVISE" in exc.value.message_dict["review"][0]
    with pytest.raises(ValidationError):
        case.publish(review_override_reason="   ")  # whitespace is not a reason


@pytest.mark.django_db
@ENFORCE
def test_publish_allowed_on_revise_with_reason_and_records_it():
    case = _publishable_case()
    review = _done_review(case, "REVISE", overall=70)
    case.publish(review_override_reason="Bigo confirmed against charge sheet p.3")
    case.refresh_from_db()
    assert case.state == CaseState.PUBLISHED
    assert case.versionInfo["review_id"] == review.pk
    assert case.versionInfo["review_disposition"] == "REVISE"
    assert case.versionInfo["review_override_reason"].startswith("Bigo confirmed")


@pytest.mark.django_db
@ENFORCE
def test_publish_allowed_on_pass_and_records_review_id():
    case = _publishable_case()
    review = _done_review(case, "PASS", overall=91)
    case.publish()
    case.refresh_from_db()
    assert case.state == CaseState.PUBLISHED
    assert case.versionInfo["review_id"] == review.pk
    assert case.versionInfo["review_disposition"] == "PASS"
    assert "review_override_reason" not in case.versionInfo


@pytest.mark.django_db
@ENFORCE
def test_publish_refused_when_case_edited_after_review():
    case = _publishable_case()
    _done_review(case, "PASS", overall=91)
    # Any save bumps updated_at (auto_now) past the review's started_at.
    case.title = "Edited after the judge looked"
    case.save()
    case.refresh_from_db()
    assert latest_verdict(case)["stale"] is True
    with pytest.raises(ValidationError) as exc:
        case.publish()
    assert "stale" in exc.value.message_dict["review"][0]


@pytest.mark.django_db
@ENFORCE
def test_newest_completed_review_wins():
    case = _publishable_case()
    _done_review(case, "REJECT", overall=30, when=timezone.now() - timezone.timedelta(hours=1))
    _done_review(case, "PASS", overall=88)
    allowed, verdict, _ = evaluate_publish_gate(case)
    assert allowed and verdict["disposition"] == "PASS"


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------


@pytest.mark.django_db
@WARN
def test_warn_mode_never_refuses_but_logs(caplog):
    case = _publishable_case()  # no review at all
    with caplog.at_level(logging.WARNING, logger="review.gate"):
        case.publish()
    case.refresh_from_db()
    assert case.state == CaseState.PUBLISHED
    assert any("WOULD BLOCK" in r.getMessage() for r in caplog.records)
    # Still auditable: the publish recorded that it had no verdict.
    assert case.versionInfo["review_id"] is None


@pytest.mark.django_db
@override_settings(REVIEW_GATE_MODE="off")
def test_off_mode_publishes_reject(caplog):
    case = _publishable_case()
    _done_review(case, "REJECT", overall=20)
    with caplog.at_level(logging.WARNING, logger="review.gate"):
        case.publish()
    case.refresh_from_db()
    assert case.state == CaseState.PUBLISHED
    assert not [r for r in caplog.records if "WOULD BLOCK" in r.getMessage()]


@pytest.mark.django_db
@override_settings(REVIEW_GATE_MODE="bogus")
def test_unknown_mode_falls_back_to_warn():
    from review.gate import gate_mode

    assert gate_mode() == "warn"


# ---------------------------------------------------------------------------
# Auto-review on submit
# ---------------------------------------------------------------------------


@pytest.mark.django_db
@override_settings(REVIEW_AUTO_REVIEW_ON_SUBMIT=True)
def test_submit_enqueues_exactly_one_review_and_dedups_in_flight():
    case = _publishable_case(state=CaseState.DRAFT)
    case.submit()
    assert CaseReview.objects.filter(case=case, status=CaseReview.STATUS_PENDING).count() == 1
    # Revert and resubmit while the first is still pending: no second row.
    case.state = CaseState.DRAFT
    case.save()
    case.submit()
    assert CaseReview.objects.filter(case=case).count() == 1


@pytest.mark.django_db
@override_settings(REVIEW_AUTO_REVIEW_ON_SUBMIT=False)
def test_submit_does_not_enqueue_when_disabled():
    case = _publishable_case(state=CaseState.DRAFT)
    case.submit()
    assert not CaseReview.objects.filter(case=case).exists()


# ---------------------------------------------------------------------------
# API path: the header is both the override reason and the audit entry
# ---------------------------------------------------------------------------


def _authed_client(role="Moderator") -> APIClient:
    user = create_user_with_role(f"gate-{role}", f"gate-{role}@example.com", role)
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def _patch_publish(client, case, reason=None):
    headers = {"HTTP_X_TRANSITION_REASON": reason} if reason else {}
    return client.patch(
        f"/api/cases/{case.slug}/",
        data=[{"op": "replace", "path": "/state", "value": CaseState.PUBLISHED}],
        format="json",
        **headers,
    )


@pytest.mark.django_db
@ENFORCE
def test_api_publish_422_on_reject_with_review_keyed_message():
    case = _publishable_case()
    _done_review(case, "REJECT", overall=35)
    resp = _patch_publish(_authed_client(), case)
    assert resp.status_code == 422, resp.data
    assert "review" in resp.data
    case.refresh_from_db()
    assert case.state == CaseState.IN_REVIEW
    assert not CaseStateChange.objects.filter(case=case, to_state=CaseState.PUBLISHED).exists()


@pytest.mark.django_db
@ENFORCE
def test_api_revise_publish_needs_header_and_header_lands_in_log():
    case = _publishable_case()
    _done_review(case, "REVISE", overall=68)
    client = _authed_client()

    assert _patch_publish(client, case).status_code == 422

    resp = _patch_publish(client, case, reason="Verified accused list against NGM by hand")
    assert resp.status_code == 200, resp.data
    case.refresh_from_db()
    assert case.state == CaseState.PUBLISHED
    change = CaseStateChange.objects.get(case=case, to_state=CaseState.PUBLISHED)
    assert change.reason.startswith("Verified accused list")
    assert case.versionInfo["review_override_reason"] == change.reason


@pytest.mark.django_db
@ENFORCE
def test_api_state_only_patch_does_not_make_its_own_review_stale():
    """The PATCH scalar path bumps updated_at on every request; a publish that
    edits nothing the judge graded must not read as 'edited after review'."""
    case = _publishable_case()
    _done_review(case, "PASS", overall=90)
    resp = _patch_publish(_authed_client(), case)
    assert resp.status_code == 200, resp.data
    case.refresh_from_db()
    assert case.state == CaseState.PUBLISHED


@pytest.mark.django_db
@ENFORCE
def test_api_patch_that_edits_content_and_publishes_is_stale():
    """Editing the title in the same PATCH as the publish is a content change
    the judge never saw: the verdict is stale and the publish is refused."""
    case = _publishable_case()
    _done_review(case, "PASS", overall=90)
    resp = _authed_client().patch(
        f"/api/cases/{case.slug}/",
        data=[
            {"op": "replace", "path": "/title", "value": "Retitled at publish time"},
            {"op": "replace", "path": "/state", "value": CaseState.PUBLISHED},
        ],
        format="json",
    )
    assert resp.status_code == 422, resp.data
    assert "stale" in resp.data["review"][0]
    case.refresh_from_db()
    assert case.state == CaseState.IN_REVIEW
