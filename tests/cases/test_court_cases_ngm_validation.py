"""Tests for cases.validators.verify_court_cases_in_ngm.

These are pure-unit tests: ``ngm.services.court_case_exists`` is monkeypatched, so
no NGM database is required.
"""

import pytest
from django.core.exceptions import ValidationError

from cases.validators import verify_court_cases_in_ngm


def _patch_exists(monkeypatch, fn):
    """Patch the symbol verify_court_cases_in_ngm imports lazily."""
    import ngm.services as ngm_services

    monkeypatch.setattr(ngm_services, "court_case_exists", fn)


def test_passes_when_all_references_exist(monkeypatch):
    seen = []

    def fake_exists(court_identifier, case_number):
        seen.append((court_identifier, case_number))
        return True

    _patch_exists(monkeypatch, fake_exists)

    # Should not raise.
    verify_court_cases_in_ngm(["special:081-CR-0095", "supreme:080-CR-0081"])
    assert seen == [("special", "081-CR-0095"), ("supreme", "080-CR-0081")]


def test_rejects_reference_missing_from_ngm(monkeypatch):
    _patch_exists(monkeypatch, lambda ci, cn: False)

    with pytest.raises(ValidationError, match="was not found in NGM"):
        verify_court_cases_in_ngm(["special:O81-CR-0095"])


def test_fail_open_when_ngm_unavailable(monkeypatch):
    def raise_unavailable(ci, cn):
        raise ValueError("NGM database is not configured")

    _patch_exists(monkeypatch, raise_unavailable)

    # NGM being unavailable must never block a write.
    verify_court_cases_in_ngm(["special:081-CR-0095"])


def test_skips_when_flag_disabled(settings, monkeypatch):
    settings.VALIDATE_COURT_CASES_AGAINST_NGM = False

    def fail_if_called(ci, cn):
        raise AssertionError("court_case_exists must not be called when disabled")

    _patch_exists(monkeypatch, fail_if_called)

    verify_court_cases_in_ngm(["special:081-CR-0095"])


def test_empty_list_is_noop(monkeypatch):
    def fail_if_called(ci, cn):
        raise AssertionError("court_case_exists must not be called for []")

    _patch_exists(monkeypatch, fail_if_called)

    verify_court_cases_in_ngm([])
    verify_court_cases_in_ngm(None)
