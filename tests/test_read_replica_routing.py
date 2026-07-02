"""Read-replica routing: the DB router sends flagged anonymous reads to a
per-service replica, and the middleware sets that flag only for public GETs.

Pure logic — no database is touched (fake models expose only ``_meta.app_label``),
so these run without the ``django_db`` fixture.
"""

from django.test import override_settings

from config.db_router import (
    ServiceDatabaseRouter,
    _reads_use_replica,
    route_reads_to_replica,
)
from config.middleware import ReadReplicaRoutingMiddleware

router = ServiceDatabaseRouter()

_ALL_RO = {"default": "default_ro", "nes": "nes_ro", "ngm": "ngm_ro"}


def _model(app_label):
    return type("M", (), {"_meta": type("Meta", (), {"app_label": app_label})})


def teardown_function():
    # Never leak the per-thread flag into another test.
    route_reads_to_replica(False)


def test_writes_always_go_to_the_primary():
    route_reads_to_replica(True)  # even when reads are replica-eligible
    assert router.db_for_write(_model("cases")) == "default"
    assert router.db_for_write(_model("entities")) == "nes"
    assert router.db_for_write(_model("courts")) == "ngm"


def test_reads_use_primary_when_not_flagged():
    with override_settings(REPLICA_ALIASES=_ALL_RO):
        assert router.db_for_read(_model("cases")) == "default"
        assert router.db_for_read(_model("entities")) == "nes"


def test_reads_use_replica_only_when_flagged():
    with override_settings(REPLICA_ALIASES=_ALL_RO):
        route_reads_to_replica(True)
        assert router.db_for_read(_model("cases")) == "default_ro"
        assert router.db_for_read(_model("entities")) == "nes_ro"
        assert router.db_for_read(_model("courts")) == "ngm_ro"


def test_reads_fall_back_to_primary_for_a_service_without_a_replica():
    with override_settings(REPLICA_ALIASES={"default": "default_ro"}):
        route_reads_to_replica(True)
        assert router.db_for_read(_model("cases")) == "default_ro"  # configured
        assert router.db_for_read(_model("entities")) == "nes"  # no replica → primary


def test_no_replicas_configured_is_a_noop():
    with override_settings(REPLICA_ALIASES={}):
        route_reads_to_replica(True)
        assert router.db_for_read(_model("cases")) == "default"


def test_allow_migrate_never_targets_a_replica():
    assert router.allow_migrate("default", "cases") is True
    assert router.allow_migrate("default_ro", "cases") is False
    assert router.allow_migrate("nes_ro", "entities") is False


# ── middleware ───────────────────────────────────────────────────────────────
def _flag_during(method, path):
    captured = {}

    def get_response(_request):
        captured["flag"] = _reads_use_replica()
        return "response"

    request = type("R", (), {"method": method, "path": path})()
    ReadReplicaRoutingMiddleware(get_response)(request)
    return captured["flag"]


def test_anon_public_get_is_replica_eligible():
    assert _flag_during("GET", "/api/cases/") is True
    assert _flag_during("GET", "/api/entities/foo") is True
    assert _flag_during("GET", "/sitemap.xml") is True
    assert _flag_during("HEAD", "/api/courtcases/") is True


def test_writes_and_editor_surfaces_stay_on_primary():
    assert _flag_during("POST", "/api/cases/") is False
    assert _flag_during("PATCH", "/api/cases/x/") is False
    assert _flag_during("GET", "/django-admin/") is False
    assert _flag_during("GET", "/api/casework/reviews") is False
    assert _flag_during("GET", "/api/jobs/status") is False
    assert _flag_during("GET", "/newsroom/pages/") is False


def test_flag_is_reset_after_the_request():
    _flag_during("GET", "/api/cases/")
    assert _reads_use_replica() is False
