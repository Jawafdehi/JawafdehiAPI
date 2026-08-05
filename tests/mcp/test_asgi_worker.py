"""Tests for bounded ASGI worker configuration."""

from config.asgi_worker import (
    DEFAULT_ASGI_LIMIT_CONCURRENCY,
    MAX_ASGI_LIMIT_CONCURRENCY,
    BoundedUvicornWorker,
    asgi_limit_concurrency,
)


def test_asgi_worker_has_a_default_concurrency_ceiling():
    assert (
        BoundedUvicornWorker.CONFIG_KWARGS["limit_concurrency"]
        == DEFAULT_ASGI_LIMIT_CONCURRENCY
    )


def test_asgi_concurrency_ceiling_is_configurable(monkeypatch):
    monkeypatch.setenv("ASGI_LIMIT_CONCURRENCY", "64")
    assert asgi_limit_concurrency() == 64


def test_asgi_concurrency_ceiling_rejects_invalid_values(monkeypatch):
    monkeypatch.setenv("ASGI_LIMIT_CONCURRENCY", "0")
    assert asgi_limit_concurrency() == DEFAULT_ASGI_LIMIT_CONCURRENCY

    monkeypatch.setenv("ASGI_LIMIT_CONCURRENCY", "not-a-number")
    assert asgi_limit_concurrency() == DEFAULT_ASGI_LIMIT_CONCURRENCY


def test_asgi_concurrency_ceiling_is_bounded(monkeypatch):
    monkeypatch.setenv("ASGI_LIMIT_CONCURRENCY", "999999")
    assert asgi_limit_concurrency() == MAX_ASGI_LIMIT_CONCURRENCY
