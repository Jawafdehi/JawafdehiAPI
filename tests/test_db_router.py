"""Tests for the primary/replica database split (config/db_router.py)."""

from types import SimpleNamespace

from django.conf import settings

from config.db_router import (
    PrimaryReplicaRouter,
    _force_primary,
    force_primary_reads,
    install_management_command_primary_reads,
)
from config.middleware import ForcePrimaryReadsMiddleware
from ngm import services


def _obj(db):
    """Minimal stand-in for a model instance bound to a database alias."""
    return SimpleNamespace(_state=SimpleNamespace(db=db))


def _register_replica(monkeypatch, alias="replica"):
    """Pretend a distinct read replica is configured for this test."""
    monkeypatch.setitem(
        settings.DATABASES, alias, {"ENGINE": "django.db.backends.postgresql"}
    )


class TestPrimaryReplicaRouter:
    def test_reads_go_to_replica_when_configured(self, monkeypatch):
        _register_replica(monkeypatch)
        router = PrimaryReplicaRouter()
        assert router.db_for_read(object) == "replica"

    def test_reads_fall_back_to_primary_without_replica(self, monkeypatch):
        monkeypatch.delitem(settings.DATABASES, "replica", raising=False)
        router = PrimaryReplicaRouter()
        assert router.db_for_read(object) == "default"

    def test_writes_go_to_primary(self):
        router = PrimaryReplicaRouter()
        assert router.db_for_write(object) == "default"

    def test_force_primary_reads_pins_reads_to_primary(self, monkeypatch):
        _register_replica(monkeypatch)
        router = PrimaryReplicaRouter()
        with force_primary_reads():
            assert router.db_for_read(object) == "default"
        # Flag is restored on exit.
        assert router.db_for_read(object) == "replica"

    def test_force_primary_reads_nests(self, monkeypatch):
        _register_replica(monkeypatch)
        router = PrimaryReplicaRouter()
        with force_primary_reads():
            with force_primary_reads():
                assert router.db_for_read(object) == "default"
            assert router.db_for_read(object) == "default"
        assert router.db_for_read(object) == "replica"

    def test_migrations_blocked_on_replicas(self):
        router = PrimaryReplicaRouter()
        assert router.allow_migrate("replica", "cases") is False
        assert router.allow_migrate("ngm_replica", "ngm") is False

    def test_migrations_allowed_on_primaries(self):
        router = PrimaryReplicaRouter()
        assert router.allow_migrate("default", "cases") is None
        assert router.allow_migrate("ngm", "ngm") is None

    def test_relations_within_a_pair_allowed(self):
        router = PrimaryReplicaRouter()
        assert router.allow_relation(_obj("default"), _obj("replica")) is True
        assert router.allow_relation(_obj("ngm"), _obj("ngm_replica")) is True

    def test_relations_across_pairs_not_decided(self):
        router = PrimaryReplicaRouter()
        assert router.allow_relation(_obj("default"), _obj("ngm")) is None


class TestReplicaSettings:
    def test_no_replica_alias_without_read_url(self):
        # The test suite runs without DATABASE_READ_URL, so reads stay on the
        # primary and no phantom replica connection is created.
        assert "replica" not in settings.DATABASES

    def test_router_is_registered(self):
        assert "config.db_router.PrimaryReplicaRouter" in settings.DATABASE_ROUTERS


class TestForcePrimaryReadsMiddleware:
    def _middleware(self):
        captured = {}

        def get_response(request):
            from config.db_router import _force_primary

            captured["force_primary"] = _force_primary()
            return "ok"

        return ForcePrimaryReadsMiddleware(get_response), captured

    def test_safe_get_reads_from_replica(self):
        middleware, captured = self._middleware()
        request = SimpleNamespace(method="GET", path="/api/cases/")
        assert middleware(request) == "ok"
        assert captured["force_primary"] is False

    def test_unsafe_method_forces_primary(self):
        middleware, captured = self._middleware()
        request = SimpleNamespace(method="POST", path="/api/cases/")
        middleware(request)
        assert captured["force_primary"] is True

    def test_admin_request_forces_primary(self):
        middleware, captured = self._middleware()
        request = SimpleNamespace(method="GET", path="/admin/cases/case/")
        middleware(request)
        assert captured["force_primary"] is True

    def test_flag_cleared_after_request(self):
        from config.db_router import _force_primary

        middleware, _ = self._middleware()
        middleware(SimpleNamespace(method="POST", path="/admin/"))
        assert _force_primary() is False


class TestManagementCommandPrimaryReads:
    def test_command_handle_runs_with_primary_reads(self):
        from django.core.management.base import BaseCommand

        # ready() already installed the patch; this is idempotent.
        install_management_command_primary_reads()

        captured = {}

        class _Cmd(BaseCommand):
            def handle(self, *args, **options):
                captured["force_primary"] = _force_primary()

        _Cmd().run_from_argv(["manage.py", "_cmd"])
        assert captured["force_primary"] is True
        # Flag is released once the command finishes.
        assert _force_primary() is False

    def test_install_is_idempotent(self):
        from django.core.management.base import BaseCommand

        install_management_command_primary_reads()
        first = BaseCommand.execute
        install_management_command_primary_reads()
        assert BaseCommand.execute is first


class TestNgmReadConnection:
    def test_prefers_replica_when_configured(self, monkeypatch):
        monkeypatch.setitem(
            settings.DATABASES,
            "ngm_replica",
            {"ENGINE": "django.db.backends.postgresql"},
        )
        monkeypatch.setattr(
            services, "connections", {"ngm": "primary", "ngm_replica": "replica"}
        )
        assert services.ngm_read_connection() == "replica"

    def test_falls_back_to_primary_without_replica(self, monkeypatch):
        # Ensure no replica alias is registered.
        monkeypatch.delitem(settings.DATABASES, "ngm_replica", raising=False)
        monkeypatch.setattr(services, "connections", {"ngm": "primary"})
        assert services.ngm_read_connection() == "primary"
