"""Pytest configuration and fixtures."""

import pytest


@pytest.fixture(autouse=True)
def mock_env(monkeypatch):
    """Set up mock environment variables for all tests."""
    monkeypatch.setenv("JAWAFDEHI_API_BASE_URL", "https://jawafdehi.invalid")
    monkeypatch.setenv("JAWAFDEHI_API_TOKEN", "test-token")
