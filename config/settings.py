"""Unified Django settings for the unified Jawafdehi platform.

The three formerly-separate Django services — NES, NGM, Jawafdehi — now run as
Django *apps* inside ONE project / one process / one image, with ONE settings
module (this file), one ``wsgi``, one ``urls``. In-process inter-app calls
replace the REST hops that used to sit between them.

What is KEPT from the database-per-service design: the THREE separate Postgres
databases. ``DATABASES`` declares ``default`` (Jawafdehi), ``nes`` and ``ngm``;
``config.db_router.ServiceDatabaseRouter`` pins each app's models to
its own DB. There are NO cross-DB foreign keys or joins — cross-app access
queries the other app's models in-process (routed to that DB) and joins in
Python (see ``cases.services.nes_resolver``).

This file is the union of the three former settings modules:
  * Jawafdehi's settings (the richest: admin/jazzmin, storages, CORS/CSRF,
    auditlog, drf-spectacular, review/case-workflow config, structlog, sentry)
    — carried over essentially whole.
  * NES + NGM's INSTALLED_APPS entries and their DB URLs (NES_DB_URL,
    NGM_DATABASE_URL), folded in.
  * One OIDC config + one shared prod-guard pattern (SECRET_KEY / ALLOWED_HOSTS
    / OIDC_ISSUER fail-closed in prod, relaxed for DEBUG / TESTING / build
    commands), one REST_FRAMEWORK block.

The supporting modules ``jawafdehi_shared.identity`` / ``jawafdehi_shared.middleware`` /
``jawafdehi_shared.logging_config`` are the Jawafdehi project's existing top-level
``config`` package (it stays importable as ``config`` via the editable install
of services/jawafdehi). Jawafdehi app code that does ``from jawafdehi_shared.identity import
...`` therefore keeps working unchanged. This settings module lives in the
top-level ``config`` package (the project's settings/urls/wsgi/asgi/db_router).
"""

import os
import sys
from pathlib import Path

import dj_database_url
import sentry_sdk
import structlog as _structlog
from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv
from sentry_sdk.integrations.django import DjangoIntegration

# Reuse the Jawafdehi project's structlog config (top-level `config` package,
# importable via the services/jawafdehi editable install).
from jawafdehi_mcp import __version__
from jawafdehi_shared.logging_config import (
    add_service_name,
    configure_structlog,
    drop_transport_noise,
)

load_dotenv()

configure_structlog()

_sentry_dsn = os.getenv("SENTRY_DSN")
_sentry_environment = os.getenv("SENTRY_ENVIRONMENT", "production")

# Filenames the interpreter assigns to code that has no source file: `manage.py
# shell -c "..."` and any exec()/eval() compile to "<string>", the interactive
# shell REPL to "<console>", piped stdin to "<stdin>". Real app code — HTTP
# request handlers, mgmt-command bodies, tasks — always has a real filename, so a
# stack that passes through any of these came from an operator one-off or a
# health-check probe, never the running service.
_EXEC_PSEUDO_FILENAMES = {"<string>", "<stdin>", "<console>"}


def _drop_exec_originated_events(event, _hint):
    """Sentry ``before_send``: drop events raised from exec'd / interactive code.

    This kills the dominant source of API Sentry noise: exceptions from ad-hoc
    ``manage.py shell -c "..."`` sessions and the CronJob pre-flight readiness
    probes (e.g. reindex-cases' ``shell -c "... assert make_client().ping()"``,
    which fires an ``AssertionError`` on every cold start until deps are
    reachable). Any event whose stack passes through a ``<string>``/``<stdin>``/
    ``<console>`` frame is such noise; everything with a real filename is kept.
    """
    for value in (event.get("exception") or {}).get("values") or []:
        for frame in (value.get("stacktrace") or {}).get("frames") or []:
            if frame.get("filename") in _EXEC_PSEUDO_FILENAMES:
                return None
    return event


def _before_send(event, hint):
    event = _drop_exec_originated_events(event, hint)
    if event is None:
        return None
    return drop_transport_noise(event, hint)


# Belt-and-suspenders: never ship events from local / dev runs even if a DSN
# leaked into a developer's .env. Prod sets SENTRY_ENVIRONMENT=production.
if _sentry_dsn and _sentry_environment.strip().lower() not in {
    "local",
    "dev",
    "development",
    "test",
}:
    sentry_sdk.init(
        dsn=_sentry_dsn,
        integrations=[DjangoIntegration()],
        traces_sample_rate=float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "1.0")),
        send_default_pii=False,
        environment=_sentry_environment,
        release=os.getenv("SENTRY_RELEASE", f"jawafdehi@{__version__}"),
        before_send=_before_send,
    )


def get_env_list(name, default=""):
    value = os.getenv(name, default)
    return [item.strip() for item in value.split(",") if item.strip()]


def env_flag(name, default=False):
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"true", "1", "yes", "y", "on"}:
        return True
    if normalized in {"false", "0", "no", "n", "off"}:
        return False
    return default


def ensure_trailing_slash(value):
    if not value:
        return value
    return value if value.endswith("/") else f"{value}/"


def build_s3_storage_options(
    access_key,
    secret_key,
    bucket_name,
    region_name,
    endpoint_url,
    use_ssl,
    custom_domain=None,
):
    storage_options = {
        "access_key": access_key,
        "secret_key": secret_key,
        "bucket_name": bucket_name,
        "region_name": region_name,
        "endpoint_url": endpoint_url,
        "use_ssl": use_ssl,
        "querystring_auth": False,
    }
    if custom_domain:
        storage_options["custom_domain"] = custom_domain
    return storage_options


def build_media_url(
    explicit_media_url=None,
    custom_domain=None,
    endpoint_url=None,
    bucket_name=None,
    use_ssl=True,
):
    if explicit_media_url:
        return ensure_trailing_slash(explicit_media_url)
    if custom_domain:
        if custom_domain.startswith(("http://", "https://")):
            return ensure_trailing_slash(custom_domain)
        scheme = "https" if use_ssl else "http"
        return f"{scheme}://{ensure_trailing_slash(custom_domain)}"
    if endpoint_url and bucket_name:
        return f"{ensure_trailing_slash(endpoint_url)}{bucket_name}/"
    return "/media/"


# BASE_DIR is the repo root (where config/, the app packages, and the served
# static/ media/ review_source_markdown/ trees live). config/settings.py → parent
# is config/, its parent is the repo root.
BASE_DIR = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Production fail-closed guard (shared pattern across the 3 former services).
# Build/admin management commands run before secrets are injected at image-build
# time — don't fail those. The running server (runserver/gunicorn) imports
# settings without these argv markers and stays protected.
# ---------------------------------------------------------------------------
DEBUG = os.getenv("DEBUG", "False") == "True"

TESTING = os.getenv("TESTING") == "true" or any("pytest" in arg for arg in sys.argv)

_BUILD_TIME_COMMANDS = {"collectstatic", "makemigrations", "migrate", "compilemessages"}
_running_build_command = any(cmd in sys.argv for cmd in _BUILD_TIME_COMMANDS)

# SECRET_KEY: fail closed in production rather than shipping a known dev key.
# Reject Django's generated-insecure prefixes AND the well-known dev sentinels
# we ship as defaults (the compose ``dev-secret-change-me`` and our own
# ``dev-insecure-`` fallback) so none of them can silently reach production.
SECRET_KEY = os.getenv("SECRET_KEY")
_REJECTED_SECRET_KEYS = frozenset({"dev-secret-change-me"})
if (
    not SECRET_KEY
    or SECRET_KEY.startswith(("django-insecure-", "dev-insecure-"))
    or SECRET_KEY in _REJECTED_SECRET_KEYS
):
    if not DEBUG and not TESTING and not _running_build_command:
        raise ImproperlyConfigured(
            "SECRET_KEY environment variable must be set to a secure value. "
            "Generate one with: python -c 'from django.core.management.utils "
            "import get_random_secret_key; print(get_random_secret_key())'"
        )
    SECRET_KEY = SECRET_KEY or "dev-insecure-platform-key"

ALLOWED_HOSTS = get_env_list("ALLOWED_HOSTS", "")
if not ALLOWED_HOSTS:
    if DEBUG:
        ALLOWED_HOSTS = ["localhost", "127.0.0.1", "[::1]"]
    elif not TESTING and not _running_build_command:
        raise ImproperlyConfigured(
            "ALLOWED_HOSTS environment variable must be set in production. "
            "Set it to a comma-separated list of allowed hostnames "
            "(e.g. ALLOWED_HOSTS=portal.jawafdehi.org)."
        )
# The pod's own IP (injected via the downward API), so Prometheus scraping of
# /metrics by pod IP isn't rejected as a DisallowedHost. No-op when unset (dev).
_pod_ip = os.getenv("POD_IP")
if _pod_ip and _pod_ip not in ALLOWED_HOSTS:
    ALLOWED_HOSTS.append(_pod_ip)

CSRF_TRUSTED_ORIGINS = get_env_list("CSRF_TRUSTED_ORIGINS")

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "json": {
            "()": _structlog.stdlib.ProcessorFormatter,
            "processor": _structlog.processors.JSONRenderer(),
            "foreign_pre_chain": [
                _structlog.contextvars.merge_contextvars,
                _structlog.processors.TimeStamper(fmt="iso", utc=True),
                _structlog.stdlib.add_logger_name,
                _structlog.stdlib.add_log_level,
                _structlog.stdlib.ExtraAdder(),
                add_service_name,
            ],
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "json",
        },
    },
    "loggers": {
        "django": {
            "handlers": ["console"],
            "level": os.getenv("DJANGO_LOG_LEVEL", "INFO"),
            "propagate": False,
        },
        "django.request": {
            "handlers": ["console"],
            "level": "ERROR",
            "propagate": False,
        },
        "django.server": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
    },
    "root": {
        "handlers": ["console"],
        "level": os.getenv("ROOT_LOG_LEVEL", "INFO"),
    },
}

# ---------------------------------------------------------------------------
# INSTALLED_APPS — the UNION of all three former projects' apps.
# ---------------------------------------------------------------------------
INSTALLED_APPS = [
    # Prometheus /metrics (HTTP request metrics via the Before/After middleware).
    "django_prometheus",
    # Jazzmin must precede django.contrib.admin (admin theme).
    "jazzmin",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Public crawl/discovery surface uses Django's Sitemaps framework (the new
    # IRI-driven sitemap lives in discovery). The platform has no Site
    # rows — the discovery Sitemap emits the canonical IRI verbatim as loc and
    # never reads the Sites framework — so django.contrib.sites is intentionally
    # NOT installed; only the sitemaps app is needed.
    "django.contrib.sitemaps",
    # Postgres lookups (JSONB containment etc.) used by the NES entity search
    # push-down. Harmless on sqlite (the DB-less test / local fallback): it only
    # registers field lookups. The NES JSONB GIN index is NOT created via
    # Meta.indexes; the 0001_initial migration splits it with
    # SeparateDatabaseAndState so the real ``CREATE INDEX ... USING gin`` runs
    # only on PostgreSQL and no-ops on sqlite (see entities).
    "django.contrib.postgres",
    "rest_framework",
    "mozilla_django_oidc",
    "drf_spectacular",
    "django_filters",
    "corsheaders",
    "auditlog",
    "rules.apps.AutodiscoverRulesConfig",
    # ── NES app (routes to the `nes` DB) ─────────────────────────────────────
    "entities",
    # ── NGM apps (route to the `ngm` DB) ─────────────────────────────────────
    "courts",
    "materials",
    # ── Jawafdehi apps (route to the `default` DB) ───────────────────────────
    "cases",
    "review",
    "case_proposals",
    "case_tags",
    # ── Newsletter (model-less proxy to the SendPulse ESP; no DB tables) ──────
    "newsletter",
    # ── Central job queue (platform-wide; Postgres-backed, no broker) ─────────
    "jobs",
    # ── Case-enrichment event bus (NATS/JetStream; no models, no migrations) ──
    # NOT "events": that name is taken by the `Events` dist (a transitive
    # dependency of opensearch-py), and a top-level collision breaks the
    # installed wheel even though a source checkout shadows it fine.
    "case_events",
    # ── Generic LLM invocation (provider registry: bedrock/proxy/CLI harnesses) ─
    "llm",
    # ── Unified search (platform-wide; queries all three domains' indices) ────
    "search",
    # ── Public discovery (Sitemaps + ResourceSync, IRI-driven; no models) ─────
    "discovery",
    # ── Wagtail CMS (headless) — public /updates news section ────────────────
    # The SPA consumes the "Jawafdehi Newsroom" CMS via the API v2 endpoints at
    # /api/cms/v2/. All Wagtail tables live on the `default` DB (the config
    # db_router routes any app_label not in NES/NGM to `default`, and the User /
    # auth tables it FKs to live there too). Editorial admin at /newsroom/.
    "wagtail.contrib.forms",
    "wagtail.contrib.redirects",
    "wagtail.embeds",
    "wagtail.sites",
    "wagtail.users",
    "wagtail.snippets",
    "wagtail.documents",
    "wagtail.images",
    "wagtail.search",
    "wagtail.admin",
    "wagtail.api.v2",
    "wagtail",
    # Redirects the page-preview iframe to the SPA so editors preview the real
    # headless article instead of a (non-existent) server-rendered template.
    "wagtail_headless_preview",
    "modelcluster",
    "taggit",
    "content",
]

MIDDLEWARE = [
    # django-prometheus: Before must be FIRST and After LAST so the request timer
    # spans the whole middleware chain.
    "django_prometheus.middleware.PrometheusBeforeMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "jawafdehi_shared.middleware.RequestIdMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # After auth so session/user reads use the primary; steers the VIEW's
    # anonymous public reads to the DB read replica (config.db_router).
    "config.middleware.ReadReplicaRoutingMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "auditlog.middleware.AuditlogMiddleware",
    # Wagtail's editor-managed redirects (wagtail.contrib.redirects). Last so it
    # only runs on responses no earlier middleware/view produced (e.g. 404s).
    "wagtail.contrib.redirects.middleware.RedirectMiddleware",
    # django-prometheus After must be the LAST middleware (see the Before entry).
    "django_prometheus.middleware.PrometheusAfterMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

# django-auditlog: the audited manager (jawafdehi_shared.db.audited) logs one
# LogEntry per changed row for a bulk ``QuerySet.update()`` / ``bulk_update()``
# up to this many affected rows; a larger write records a single summary entry
# instead, bounding both LogEntry growth and the O(rows) diff cost of a big
# backfill. AUDITLOG_STORE_JSON_CHANGES is intentionally left at its default
# (False → ``{field: [old, new]}``); flipping it would change the stored diff
# shape that existing rows/tests rely on.
AUDIT_BULK_UPDATE_MAX_ROWS = 1000

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        # Repo-root templates/ holds the Wagtail admin logo override
        # (templates/wagtailadmin/logo.html — Jawafdehi Newsroom branding).
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

# Session logins (Django admin only — the portal uses JWT) redirect to the
# Django admin, now mounted at /django-admin/ (the SPA owns /admin/*).
LOGIN_REDIRECT_URL = "/django-admin/"

# ---------------------------------------------------------------------------
# DATABASES — three databases, one per former service. The DB router pins each
# app's models to its database. No cross-DB FKs/joins.
# ---------------------------------------------------------------------------
_SSL_PARAMS = frozenset(
    {
        "sslmode",
        "sslrootcert",
        "sslcert",
        "sslkey",
        "sslcrl",
        "sslpassword",
        "sslcompression",
        "sslsni",
    }
)


def _apply_db_ssl_options(db_config):
    """Move URL-query SSL params into OPTIONS; apply env-var SSL defaults."""
    if db_config.get("ENGINE") != "django.db.backends.postgresql":
        return db_config
    options = db_config.setdefault("OPTIONS", {})
    for param in _SSL_PARAMS:
        if param in db_config:
            options.setdefault(param, db_config.pop(param))
    ssl_mode = os.getenv("DATABASE_SSL_MODE")
    if ssl_mode:
        options.setdefault("sslmode", ssl_mode)
    ca_cert = os.getenv("DATABASE_SSL_CA_CERT_FILE") or os.getenv("SSL_CA_CERT_FILE")
    if ca_cert:
        options.setdefault("sslrootcert", ca_cert)
    client_cert = os.getenv("DATABASE_SSL_CLIENT_CERT_FILE") or os.getenv(
        "SSL_CLIENT_CERT_FILE"
    )
    if client_cert:
        options.setdefault("sslcert", client_cert)
    client_key = os.getenv("DATABASE_SSL_CLIENT_KEY_FILE") or os.getenv(
        "SSL_CLIENT_KEY_FILE"
    )
    if client_key:
        options.setdefault("sslkey", client_key)
    return db_config


def interpolate_db_url(url_env_name):
    """Inject DATABASE_PASSWORD into ${DATABASE_PASSWORD} placeholders."""
    url = os.getenv(url_env_name)
    if not url:
        return
    password = os.getenv("DATABASE_PASSWORD")
    if password:
        url = url.replace("${DATABASE_PASSWORD}", password)
        os.environ[url_env_name] = url


interpolate_db_url("DATABASE_URL")
interpolate_db_url("NGM_DATABASE_URL")
interpolate_db_url("NES_DB_URL")

# default = Jawafdehi DB. In tests / no-DB-URL dev it falls back to sqlite, and
# the NES/NGM aliases ALSO fall back to sqlite so the whole platform runs
# DB-less. CRITICAL: each alias gets its OWN sqlite database (distinct file +
# distinct TEST NAME), NOT one shared ``db.sqlite3``. Sharing a single file
# would defeat the DB router's per-service isolation — a row routed to the
# ``nes`` alias would be visible on ``default`` — and Django would mirror the
# three aliases into ONE test database (collapsing the router's behaviour under
# tests). Distinct names mean the test runner builds three separate sqlite test
# DBs, exercising read/write/allow_migrate the same way three separate Postgres
# DBs do in prod. In prod (URLs set) these defaults are never used.
_default_is_sqlite = not os.getenv("DATABASE_URL")


def _sqlite_alias(file_name: str, test_name: str) -> dict:
    """A sqlite alias config with a distinct on-disk file and TEST NAME."""
    return {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": file_name,
        "TEST": {"NAME": test_name},
    }


# Read once each, rather than calling `os.getenv` twice per alias (once to test and
# once to pass). Two calls mean the value handed to `parse()` is not provably the
# non-empty one the test checked.
_NES_DB_URL = os.getenv("NES_DB_URL")
_NGM_DB_URL = os.getenv("NGM_DATABASE_URL")

DATABASES = {
    "default": dj_database_url.config(default="sqlite:///db.sqlite3"),
    # nes DB — the NES entity store.
    "nes": (
        dj_database_url.parse(_NES_DB_URL)
        if _NES_DB_URL
        else _sqlite_alias("db_nes.sqlite3", "test_nes.sqlite3")
    ),
    # ngm DB — courts/materials.
    "ngm": (
        dj_database_url.parse(_NGM_DB_URL)
        if _NGM_DB_URL
        else _sqlite_alias("db_ngm.sqlite3", "test_ngm.sqlite3")
    ),
}

# Give the sqlite ``default`` fallback an explicit, distinct TEST NAME too, so
# the three test databases never collide/mirror.
if DATABASES["default"].get("ENGINE") == "django.db.backends.sqlite3":
    DATABASES["default"].setdefault("TEST", {}).setdefault(
        "NAME", "test_default.sqlite3"
    )

# Dynamic password interpolation (Google Secret Manager at runtime).
db_password = os.getenv("DATABASE_PASSWORD")
if db_password:
    _url_env_for_alias = {
        "default": "DATABASE_URL",
        "nes": "NES_DB_URL",
        "ngm": "NGM_DATABASE_URL",
    }
    for db_key, env_name in _url_env_for_alias.items():
        db_url = os.getenv(env_name)
        if db_url and "${DATABASE_PASSWORD}" in db_url:
            new_url = db_url.replace("${DATABASE_PASSWORD}", db_password)
            DATABASES[db_key] = dj_database_url.parse(new_url)

# Read replicas (optional). When a "*_READ_URL" is set, register a "<alias>_ro"
# connection to the read-only replica endpoint. The DB router (config.db_router)
# sends anonymous public reads there, while writes + admin/casework traffic stay
# on the primary (read-your-write). REPLICA_ALIASES maps primary alias -> replica
# alias; it is EMPTY when no replica is configured (dev/tests), so the router
# transparently falls back to the primary and behaviour is unchanged.
REPLICA_ALIASES: dict[str, str] = {}
_replica_url_env = {
    "default": "DATABASE_READ_URL",
    "nes": "NES_DB_READ_URL",
    "ngm": "NGM_DATABASE_READ_URL",
}
for _primary_alias, _read_env in _replica_url_env.items():
    _read_url = os.getenv(_read_env)
    if _read_url:
        if db_password and "${DATABASE_PASSWORD}" in _read_url:
            _read_url = _read_url.replace("${DATABASE_PASSWORD}", db_password)
        _ro_alias = f"{_primary_alias}_ro"
        DATABASES[_ro_alias] = dj_database_url.parse(_read_url)
        REPLICA_ALIASES[_primary_alias] = _ro_alias

# Apply SSL options to every Postgres database, and disable Django's persistent
# connections. Connection lifetime is not dependable under ASGI: `connections` is
# a thread_critical Local (django.db.utils.ConnectionHandler), while the
# request_finished signal that runs close_old_connections is sent from the event
# loop, so the handler does not necessarily see the connection that served the
# request. CONN_MAX_AGE = 0 keeps that from turning into idle backends that
# nothing reclaims; the cost is one connect per request, which is part of why
# ASGI_LIMIT_CONCURRENCY bounds how many can be in flight per worker.
for db_key in DATABASES:
    _apply_db_ssl_options(DATABASES[db_key])
    if DATABASES[db_key].get("ENGINE") == "django.db.backends.postgresql":
        DATABASES[db_key]["CONN_MAX_AGE"] = 0
        DATABASES[db_key]["CONN_HEALTH_CHECKS"] = False

DATABASE_ROUTERS = ["config.db_router.ServiceDatabaseRouter"]

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

AUTHENTICATION_BACKENDS = [
    "rules.permissions.ObjectPermissionBackend",
    # Django-admin SSO (Zitadel via mozilla-django-oidc). ModelBackend stays for
    # local break-glass superusers created with createsuperuser.
    "config.oidc_admin.AdminOIDCBackend",
    "django.contrib.auth.backends.ModelBackend",
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "Asia/Kathmandu"
USE_I18N = True
USE_TZ = True

# Static files
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]

AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY")
AWS_STORAGE_BUCKET_NAME = os.getenv("AWS_STORAGE_BUCKET_NAME", "jawafdehi")
AWS_S3_REGION_NAME = os.getenv("AWS_S3_REGION_NAME", "auto")
AWS_S3_ENDPOINT_URL = os.getenv("AWS_S3_ENDPOINT_URL")
AWS_S3_CUSTOM_DOMAIN = os.getenv("AWS_S3_CUSTOM_DOMAIN")
AWS_S3_USE_SSL = env_flag("AWS_S3_USE_SSL", True)

if AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY:
    storage_options = build_s3_storage_options(
        access_key=AWS_ACCESS_KEY_ID,
        secret_key=AWS_SECRET_ACCESS_KEY,
        bucket_name=AWS_STORAGE_BUCKET_NAME,
        region_name=AWS_S3_REGION_NAME,
        endpoint_url=AWS_S3_ENDPOINT_URL,
        use_ssl=AWS_S3_USE_SSL,
        custom_domain=AWS_S3_CUSTOM_DOMAIN,
    )
    STORAGES = {
        "default": {
            "BACKEND": "cases.storage.HashedFilenameS3Boto3Storage",
            "OPTIONS": storage_options,
        },
        "staticfiles": {
            "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
        },
    }
else:
    STORAGES = {
        "default": {
            "BACKEND": "django.core.files.storage.FileSystemStorage",
        },
        "staticfiles": {
            "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
        },
    }

MEDIA_URL = build_media_url(
    explicit_media_url=os.getenv("MEDIA_URL"),
    custom_domain=AWS_S3_CUSTOM_DOMAIN,
    endpoint_url=AWS_S3_ENDPOINT_URL,
    bucket_name=(
        AWS_STORAGE_BUCKET_NAME if AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY else None
    ),
    use_ssl=AWS_S3_USE_SSL,
)
MEDIA_ROOT = os.getenv("MEDIA_ROOT", BASE_DIR / "media")

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ---------------------------------------------------------------------------
# OIDC (Zitadel) — single config shared by every app's DRF auth.
# ---------------------------------------------------------------------------
OIDC_ISSUER = os.getenv("OIDC_ISSUER", "")
_oidc_audience = get_env_list("OIDC_AUDIENCE")
OIDC_AUDIENCE = (
    _oidc_audience[0] if len(_oidc_audience) == 1 else (_oidc_audience or None)
)
OIDC_JWKS_URI = os.getenv("OIDC_JWKS_URI") or (
    f"{ensure_trailing_slash(OIDC_ISSUER)}oauth/v2/keys" if OIDC_ISSUER else ""
)
OIDC_ROLES_CLAIM = os.getenv("OIDC_ROLES_CLAIM", "urn:zitadel:iam:org:project:roles")
OIDC_ALGORITHMS = get_env_list("OIDC_ALGORITHMS", "RS256")
OIDC_LEEWAY = int(os.getenv("OIDC_LEEWAY", "30"))
OIDC_JWKS_CACHE_SECONDS = int(os.getenv("OIDC_JWKS_CACHE_SECONDS", "300"))
OIDC_JWKS_TIMEOUT = float(os.getenv("OIDC_JWKS_TIMEOUT", "10"))
OIDC_SERVICE_ACCOUNT_SUBJECTS = get_env_list("OIDC_SERVICE_ACCOUNT_SUBJECTS")
OIDC_SERVICE_ACCOUNT_ROLE = os.getenv("OIDC_SERVICE_ACCOUNT_ROLE", "contributor")

if not DEBUG and not TESTING and not _running_build_command and not OIDC_ISSUER:
    raise ImproperlyConfigured(
        "OIDC_ISSUER environment variable must be set in production. "
        "OIDC (Zitadel) is the only authentication method for the API."
    )

# ---------------------------------------------------------------------------
# Django-admin SSO (mozilla-django-oidc, session login). Separate from the DRF
# bearer auth above: /django-admin/ flows through Zitadel using the SAME public
# PKCE client the SPA uses (no client secret). Roles -> Groups/is_staff via
# config.oidc_admin.AdminOIDCBackend.
# ---------------------------------------------------------------------------
OIDC_RP_CLIENT_ID = os.getenv("OIDC_RP_CLIENT_ID", "")
OIDC_RP_CLIENT_SECRET = os.getenv("OIDC_RP_CLIENT_SECRET", "")  # empty = public PKCE client
OIDC_USE_PKCE = True
OIDC_RP_SIGN_ALGO = "RS256"
OIDC_RP_SCOPES = "openid email profile"
OIDC_OP_AUTHORIZATION_ENDPOINT = os.getenv("OIDC_OP_AUTHORIZATION_ENDPOINT") or (
    f"{ensure_trailing_slash(OIDC_ISSUER)}oauth/v2/authorize"
)
OIDC_OP_TOKEN_ENDPOINT = os.getenv("OIDC_OP_TOKEN_ENDPOINT") or (
    f"{ensure_trailing_slash(OIDC_ISSUER)}oauth/v2/token"
)
OIDC_OP_USER_ENDPOINT = os.getenv("OIDC_OP_USER_ENDPOINT") or (
    f"{ensure_trailing_slash(OIDC_ISSUER)}oidc/v1/userinfo"
)
OIDC_OP_JWKS_ENDPOINT = OIDC_JWKS_URI
LOGIN_URL = "/oidc/authenticate/"
LOGOUT_REDIRECT_URL = "/django-admin/login/"

# ---------------------------------------------------------------------------
# Wagtail CMS (headless — serves the public /updates news section via API v2).
# Ported from the pre-unification Jawafdehi monolith ("Jawafdehi Newsroom").
# Wagtail images/documents share the platform default storage
# (HashedFilenameS3Boto3Storage when S3 creds are set, else FileSystemStorage);
# it leaves their namespaced upload paths untouched (see cases/storage.py).
# ---------------------------------------------------------------------------
WAGTAIL_SITE_NAME = "Jawafdehi Newsroom"
# Base URL used for absolute links in the admin (notification emails, previews).
# Not used for public delivery — the SPA consumes the API v2 endpoints.
WAGTAILADMIN_BASE_URL = os.getenv(
    "WAGTAILADMIN_BASE_URL", "https://portal.jawafdehi.org"
)
# ---------------------------------------------------------------------------
# Outbound email — DISABLED.
# ---------------------------------------------------------------------------
# Wagtail sends notification emails synchronously on the edit screen (comment-
# subscriber notifications and workflow/moderation transitions) and on admin
# password reset. The platform has no mail relay, so Django's default SMTP
# backend dialed localhost:25, raised ConnectionRefusedError, and returned a
# 500 from the triggering save — and because the comment had already committed,
# each retry re-inserted it as a duplicate. We don't want these notifications,
# so route mail to the dummy backend: messages are accepted and discarded
# without ever opening a connection. Editorial features (comments, workflow)
# keep working; they simply don't email. The newsletter is unaffected — it
# posts to SendPulse's REST API, not through Django's mail backend. Set
# EMAIL_BACKEND in the environment to re-enable delivery.
EMAIL_BACKEND = os.getenv(
    "EMAIL_BACKEND", "django.core.mail.backends.dummy.EmailBackend"
)
# Headless preview: the edit-screen preview iframe 302-redirects to the public
# SPA's article preview route (which fetches the unsaved draft from the
# page_preview API by token), so editors see the real styled article instead of
# a server-rendered template that doesn't exist.
WAGTAIL_HEADLESS_PREVIEW = {
    "CLIENT_URLS": {
        # rstrip so a stray trailing slash in the env value can't break the
        # redirect URL or the worker's frame-allow path match.
        "default": os.getenv(
            "WAGTAIL_HEADLESS_PREVIEW_CLIENT_URL",
            "https://jawafdehi.org/updates/preview",
        ).rstrip("/"),
    },
    "REDIRECT_ON_PREVIEW": True,
    # Keep the redirect URL slash-free so it matches the SPA's `/updates/preview`
    # route and the worker's frame-allow check exactly.
    "ENFORCE_TRAILING_SLASH": False,
}
WAGTAILSEARCH_BACKENDS = {
    "default": {
        "BACKEND": "wagtail.search.backends.database",
    }
}
WAGTAILDOCS_EXTENSIONS = ["csv", "docx", "pdf", "pptx", "rtf", "txt", "xlsx", "zip"]
WAGTAILDOCS_MAX_UPLOAD_SIZE = 10 * 1024 * 1024  # 10 MB, matches case upload limit
WAGTAILIMAGES_MAX_UPLOAD_SIZE = 10 * 1024 * 1024
# Headless: pages are delivered via the API, not Wagtail's own routing.
WAGTAIL_APPEND_SLASH = False
# Complex StreamField pages can exceed Django's default form field cap.
DATA_UPLOAD_MAX_NUMBER_FIELDS = 10_000
# Route the Wagtail admin (/newsroom) through the same OIDC SSO flow instead of
# Wagtail's built-in username/password form. require_admin_access redirects
# unauthenticated users here (with ?next=), so the OIDC callback returns them to
# the newsroom. The /newsroom/login/ form is also redirected (see config/urls).
WAGTAILADMIN_LOGIN_URL = LOGIN_URL

# ---------------------------------------------------------------------------
# REST Framework — single config. OIDC is the sole API authenticator. The
# permission default is read-public / authenticated-write (the NES/NGM planes
# relied on ReadOnlyOrAuthenticatedWrite; Jawafdehi's views set their own
# per-view permissions on top, so a global read-public default is compatible
# with them too). Pagination/throttle/schema carried from Jawafdehi.
# ---------------------------------------------------------------------------
# Local development auth: Zitadel/OIDC is the production identity provider, but
# spinning up Zitadel for local work is heavy. When DEV_AUTH is enabled (only
# honored under DEBUG or TESTING — never in production), we ALSO accept Django
# session + HTTP-Basic auth so a developer can log in with a plain
# username/password (e.g. a `createsuperuser` account, or a seeded Caseworker).
# Role/group membership is the SAME model as prod (Admin/Moderator/Caseworker/
# ReadOnly/Public Django Groups + is_superuser) — dev login just skips the JWT.
# OIDCAuthentication stays FIRST so bearer tokens keep working unchanged; the
# session/basic classes are additive and gated, so production auth is untouched.
DEV_AUTH = env_flag("DEV_AUTH", False) and (DEBUG or TESTING)
DEV_NGM_QUERY_TOKEN = (
    os.getenv("DEV_NGM_QUERY_TOKEN", "") if DEV_AUTH and TESTING else ""
)
DEV_NGM_QUERY_USERNAME = os.getenv("DEV_NGM_QUERY_USERNAME", "mcp-query-e2e")

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        *(
            ["jawafdehi_shared.auth.dev_service.DevelopmentQueryTokenAuthentication"]
            if DEV_NGM_QUERY_TOKEN
            else []
        ),
        "jawafdehi_shared.auth.oidc.OIDCAuthentication",
        *(
            [
                "rest_framework.authentication.SessionAuthentication",
                "rest_framework.authentication.BasicAuthentication",
            ]
            if DEV_AUTH
            else []
        ),
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "jawafdehi_shared.drf.base.ReadOnlyOrAuthenticatedWrite",
    ],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 20,
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
        # Browsable API (with its login form) only in local dev, so DEV_AUTH
        # session login is usable from a browser. JSON-only in production.
        *(
            ["rest_framework.renderers.BrowsableAPIRenderer"]
            if DEV_AUTH
            else []
        ),
    ],
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
}

# Throttling is enabled by default (protects the live API). The anon/user rates
# are env-configurable (THROTTLE_RATE_ANON / THROTTLE_RATE_USER) so an operator
# can tune them without a rebuild. The anon bucket defaults to a generous
# 1000/hour: high enough that the unauthenticated integration-test suite hits a
# RUNNING container without 429-ing, while still capping abusive scrapers.
# Throttling is fully disabled only under the test runner (TESTING) — DRF's
# rate-limit caches make per-request unit tests flaky otherwise.
#
# F14 (throttle counter scope): DRF's stock throttles store counters in the
# ``default`` cache. Backed by ``LocMemCache`` (per-process) the cap is counted PER
# gunicorn worker, so the effective ceiling is ~rate × workers × replicas and grows
# as you scale; backed by a shared Redis it is global but puts a cache round-trip on
# every request — costly here because the shared Valkey can sit in the other cloud
# (Monal↔OCI WireGuard mesh). The Synced* throttles below resolve both: they count
# in-process on the hot path (no network, latency-neutral) and reconcile to a shared
# Redis (``THROTTLE_SYNC_URL``) on a background timer, giving an APPROXIMATELY global
# cap that is pod/worker-count-independent and fail-open. See
# jawafdehi_shared/drf/throttling.py and docs/security/threat-model.md.
if not TESTING:
    REST_FRAMEWORK["DEFAULT_THROTTLE_CLASSES"] = [
        # local count + async Redis reconciliation; fall back to DRF's stock
        # per-process behaviour when THROTTLE_SYNC_URL is unset (dev/off-cluster).
        "jawafdehi_shared.drf.throttling.SyncedAnonRateThrottle",
        "jawafdehi_shared.drf.throttling.SyncedUserRateThrottle",
    ]
    REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"] = {
        "anon": os.getenv("THROTTLE_RATE_ANON", "1000/hour"),
        "user": os.getenv("THROTTLE_RATE_USER", "5000/hour"),
    }
else:
    # Under the test runner, ensure no throttle classes/rates leak through.
    REST_FRAMEWORK["DEFAULT_THROTTLE_CLASSES"] = []
    REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"] = {}

SPECTACULAR_SETTINGS = {
    "TITLE": "Jawafdehi Public Accountability API",
    "DESCRIPTION": "Public API for the Jawafdehi accountability platform.",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "COMPONENT_SPLIT_REQUEST": True,
    "SCHEMA_PATH_PREFIX": "/api/",
    "SERVERS": None,
    "ENUM_NAME_OVERRIDES": {
        "CaseTypeEnum": "cases.models.CaseType",
    },
}

# CORS / CSRF
CORS_ALLOW_ALL_ORIGINS = False
CORS_ALLOWED_ORIGINS = get_env_list(
    "CORS_ALLOWED_ORIGINS",
    "http://localhost:8080,http://127.0.0.1:8080,https://jawafdehi.org,https://beta.jawafdehi.org",
)
CORS_ALLOWED_ORIGIN_REGEXES = get_env_list(
    "CORS_ALLOWED_ORIGIN_REGEXES",
    r"^https://([a-z0-9-]+\.)?newnepal\.workers\.dev$",
)
CORS_ALLOW_METHODS = ["GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"]
CORS_ALLOW_HEADERS = [
    "accept",
    "accept-encoding",
    "authorization",
    "content-type",
    "dnt",
    "origin",
    "user-agent",
    "x-csrftoken",
    "x-requested-with",
    # Case optimistic-concurrency precondition + moderator transition reason
    # (see CaseViewSet.partial_update). Neither is CORS-safelisted, so a
    # cross-origin admin panel must be allowed to send them.
    "if-match",
    "x-transition-reason",
]
# Expose the ETag so a cross-origin admin panel can read the optimistic-
# concurrency token off retrieve/PATCH responses (same-origin dev already sees
# it; this keeps a split-origin deploy working).
CORS_EXPOSE_HEADERS = ["ETag"]
CORS_ALLOW_CREDENTIALS = True

if not CSRF_TRUSTED_ORIGINS:
    CSRF_TRUSTED_ORIGINS = CORS_ALLOWED_ORIGINS.copy()

# Security headers / TLS enforcement
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
X_FRAME_OPTIONS = "DENY"
# Exempt /metrics from the HTTPS redirect: vmagent scrapes the pod IP over plain
# HTTP (no Traefik, so no X-Forwarded-Proto=https), which SECURE_SSL_REDIRECT would
# otherwise 301 to an https:// URL the pod doesn't serve. Defined unconditionally —
# harmless when the redirect is off (DEBUG/tests) — so it always applies when the
# redirect is on, and is testable without being overridden. Trailing slash optional.
# The ingress still blocks /metrics from the public internet (metrics-deny-public).
SECURE_REDIRECT_EXEMPT = [r"^metrics/?$"]

if not DEBUG:
    SECURE_HSTS_SECONDS = int(os.getenv("SECURE_HSTS_SECONDS", "300"))
    SECURE_HSTS_INCLUDE_SUBDOMAINS = env_flag("SECURE_HSTS_INCLUDE_SUBDOMAINS", False)
    SECURE_HSTS_PRELOAD = env_flag("SECURE_HSTS_PRELOAD", False)
    # Default on in prod, overridable for local HTTP dev (compose serves plain
    # HTTP behind no TLS terminator). Disabled under TESTING so the discovery
    # client tests (and others) pass at the default DEBUG=False used by the test
    # runner without being redirected to https. TESTING is only true under the
    # test runner (settings.py: pytest / TESTING=true), so prod is unaffected.
    SECURE_SSL_REDIRECT = env_flag("SECURE_SSL_REDIRECT", True) and not TESTING
    CSRF_COOKIE_SECURE = True
    SESSION_COOKIE_SECURE = True
    SESSION_COOKIE_HTTPONLY = True
    CSRF_COOKIE_HTTPONLY = True

# ---------------------------------------------------------------------------
# Newsletter / SendPulse ESP. The `newsletter` app is a model-less proxy: SendPulse
# stores subscribers and runs its own double opt-in confirmation (and hosts the
# unsubscribe link in every email, so the backend owns no unsubscribe route). When
# these are unset (not yet provisioned, or under CI) the subscribe endpoint still
# accepts requests (HTTP 202) and logs them — the flow degrades gracefully rather
# than 500-ing.
#
# Auth is either a static SENDPULSE_API_KEY (preferred: one secret, used directly
# as a Bearer token, no OAuth exchange) or the classic SENDPULSE_CLIENT_ID /
# _CLIENT_SECRET pair. An address book id is required for both.
# ---------------------------------------------------------------------------
SENDPULSE_API_KEY = os.getenv("SENDPULSE_API_KEY", "")
SENDPULSE_CLIENT_ID = os.getenv("SENDPULSE_CLIENT_ID", "")
SENDPULSE_CLIENT_SECRET = os.getenv("SENDPULSE_CLIENT_SECRET", "")
SENDPULSE_ADDRESSBOOK_ID = os.getenv("SENDPULSE_ADDRESSBOOK_ID", "")
SENDPULSE_TIMEOUT_SECONDS = float(os.getenv("SENDPULSE_TIMEOUT_SECONDS", "5"))
# Double opt-in: when SENDPULSE_CONFIRMATION is on AND a (verified) sender is set,
# SendPulse sends its confirmation email and holds the contact until they click.
# SENDPULSE_CONFIRMATION_TEMPLATE_ID is optional (SendPulse's default confirmation
# email is used until a custom template is moderated). message_lang has no Nepali
# option, so the bilingual template covers Nepali readers.
SENDPULSE_CONFIRMATION = env_flag("SENDPULSE_CONFIRMATION", False)
SENDPULSE_SENDER_EMAIL = os.getenv("SENDPULSE_SENDER_EMAIL", "")
SENDPULSE_SENDER_NAME = os.getenv("SENDPULSE_SENDER_NAME", "Jawafdehi")
SENDPULSE_CONFIRMATION_TEMPLATE_ID = os.getenv("SENDPULSE_CONFIRMATION_TEMPLATE_ID", "")
SENDPULSE_MESSAGE_LANG = os.getenv("SENDPULSE_MESSAGE_LANG", "en")
# Welcome email: when on (and a verified SENDPULSE_SENDER_EMAIL is set), a
# transactional welcome is sent to each new subscriber via SendPulse /smtp/emails.
# This is the "email send" for single opt-in (double opt-in via API is unavailable
# for this account). Best-effort — a send failure never fails the subscribe.
SENDPULSE_WELCOME_EMAIL = env_flag("SENDPULSE_WELCOME_EMAIL", False)
SENDPULSE_WELCOME_SUBJECT = os.getenv("SENDPULSE_WELCOME_SUBJECT", "Welcome to Jawafdehi")

# Corruption case reports arrive through the public feedback endpoint and have no
# read API, so without an alert they sit unseen until someone opens Django admin.
# When on, each one triggers a SendPulse notification carrying only a reference
# number and an admin link — never the report's contents. Best-effort: a send
# failure never fails the submission.
CASE_REPORT_NOTIFY = env_flag("CASE_REPORT_NOTIFY", False)
CASE_REPORT_NOTIFY_EMAIL = os.getenv("CASE_REPORT_NOTIFY_EMAIL", "report@jawafdehi.org")

# ---------------------------------------------------------------------------
# NES/NGM config. After the service consolidation NES and NGM run IN-PROCESS — the
# nes_resolver seam + in-process ORM calls, NOT REST hops. The dead NGM REST
# proxy (and its NGM_API_BASE_URL / NGM_API_TOKEN / NGM_API_TIMEOUT_SECONDS /
# NES_DB_PATH settings) was removed; the only NGM knob still read is the gated
# query row cap. NES_API_URL is retained for the standalone enrich_ciaa_related_
# entities NES-search linker (a separate legacy command, not the in-process seam).
# ---------------------------------------------------------------------------
NES_API_URL = os.getenv("NES_API_URL", "https://nes.jawafdehi.org/api")
NGM_QUERY_MAX_ROWS = int(os.getenv("NGM_QUERY_MAX_ROWS", "500"))

# Default cache. Also backs DRF's rate-limit throttle counters (see the
# throttling block above). LocMemCache is per-process, so with >1 gunicorn worker
# the anon/user throttle is per-worker, not global (F14). Set ``CACHE_URL`` to a
# shared backend (e.g. ``redis://host:6379/0``) to make the throttle a true global
# quota; when unset we fall back to per-process LocMem (fine for dev/tests).
_CACHE_URL = os.getenv("CACHE_URL", "").strip()
if _CACHE_URL:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": _CACHE_URL,
            "TIMEOUT": 300,
        }
    }
else:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "jawafdehi-cache",
            "TIMEOUT": 300,
        }
    }

# Jazzmin admin theme
JAZZMIN_SETTINGS = {
    "site_title": "Jawafdehi Admin",
    "site_header": "Jawafdehi",
    "site_brand": "Jawafdehi",
    "site_logo": "corruption-db-logo.png",
    "login_logo": "corruption-db-logo.png",
    "site_logo_classes": "img-circle",
    "site_icon": "corruption-db-logo.png",
    "welcome_sign": "Welcome to Jawafdehi Contributor Portal",
    "copyright": "Jawafdehi",
    "search_model": ["cases.Case"],
    "user_avatar": None,
    "topmenu_links": [
        {"name": "Home", "url": "admin:index", "permissions": ["auth.view_user"]},
        {"name": "Public API", "url": "/api/swagger", "new_window": True},
    ],
    "show_sidebar": True,
    "navigation_expanded": True,
    "hide_apps": [],
    "hide_models": [],
    "order_with_respect_to": ["cases", "auth"],
    "icons": {
        "auth": "fas fa-users-cog",
        "auth.user": "fas fa-user",
        "auth.Group": "fas fa-users",
        "cases.Case": "fas fa-gavel",
    },
    "custom_css": None,
    "custom_js": None,
    "use_google_fonts_cdn": True,
    "show_ui_builder": False,
    "changeform_format": "horizontal_tabs",
    "changeform_format_overrides": {
        "auth.user": "collapsible",
        "auth.group": "vertical_tabs",
    },
}

# ============================================================================
# Casework Review System (VOL-3)
# ============================================================================
REVIEW_CASE_SOURCE = os.getenv("REVIEW_CASE_SOURCE", "local")
JAWAFDEHI_API_BASE = os.getenv("JAWAFDEHI_API_BASE", "https://api.jawafdehi.org/api")
JAWAFDEHI_API_TOKEN = os.getenv("JAWAFDEHI_API_TOKEN", "")
JAWAFDEHI_S3_BASE = os.getenv("JAWAFDEHI_S3_BASE", "https://s3.jawafdehi.org")

# AWS Bedrock (LLM judge). Distinct from the S3 storage creds above: the judge
# uses a named profile / cross-region inference profile model id.
AWS_PROFILE = os.getenv("REVIEW_AWS_PROFILE", os.getenv("AWS_PROFILE", ""))
AWS_REGION = os.getenv("AWS_REGION", "us-west-2")
BEDROCK_MODEL_ID = os.getenv("BEDROCK_MODEL_ID", "global.anthropic.claude-opus-4-8")
BEDROCK_MAX_WORKERS = int(os.getenv("BEDROCK_MAX_WORKERS", "8"))
# Prompt-cache the shared rule-grading prefix (case data + source excerpts) so it
# is billed once per case instead of once per (rule x sample) call. Kill switch
# for regions/models where Bedrock prompt caching is unavailable.
BEDROCK_PROMPT_CACHE = env_flag("BEDROCK_PROMPT_CACHE", default=True)
# Tiered model routing: high-stakes GATE rules (a low score can REJECT the case)
# are graded by the premium model above; routine non-gate rules and the narrative
# use this cheaper SKU. Defaults to the premium model id, so routing is a no-op
# until a real cheaper model id is configured for this deployment's region/account
# (the operator picks the SKU — we never guess one that may not exist).
BEDROCK_MODEL_ID_CHEAP = os.getenv("BEDROCK_MODEL_ID_CHEAP", BEDROCK_MODEL_ID)

# Review-judge LLM provider. "bedrock" (default) invokes AWS Bedrock directly;
# "claude_cli" / "codex_cli" run locally-installed subscription CLIs; "proxy"
# routes through the in-house OpenAI-compatible llm-proxy so the judge can run
# on non-Bedrock models without code changes.
REVIEW_LLM_PROVIDER = os.getenv("REVIEW_LLM_PROVIDER", "bedrock").strip().lower()
REVIEW_LLM_PROVIDER_PREMIUM = (
    os.getenv("REVIEW_LLM_PROVIDER_PREMIUM", REVIEW_LLM_PROVIDER).strip().lower()
)
REVIEW_LLM_PROVIDER_CHEAP = (
    os.getenv("REVIEW_LLM_PROVIDER_CHEAP", REVIEW_LLM_PROVIDER).strip().lower()
)
# llm-proxy (OpenAI-compatible) connection. In-cluster callers MUST use the
# internal ClusterIP base URL: the public llm-proxy.jawafdehi.org host sits behind
# a Cloudflare WAF that 403s the OpenAI SDK user-agent.
LLM_PROXY_BASE_URL = os.getenv(
    "LLM_PROXY_BASE_URL", "http://llm-proxy.app.svc.cluster.local/v1"
)
LLM_PROXY_API_KEY = os.getenv("LLM_PROXY_API_KEY", "")
# Override the OpenAI SDK User-Agent when fronting the proxy through the public
# Cloudflare-WAF host (which 403s the default SDK UA). Leave empty for the
# in-cluster ClusterIP path.
LLM_PROXY_USER_AGENT = os.getenv("LLM_PROXY_USER_AGENT", "")
# Proxy model ids per tier (the proxy registry's names differ from Bedrock SKU
# ids, so they are configured separately). Premium grades hard GATE rules; cheap
# grades routine rules + the narrative. Cheap defaults to premium, so tiering is a
# no-op until a real cheaper id is set. Operators pick the ids — we never guess.
LLM_PROXY_MODEL_ID = os.getenv("LLM_PROXY_MODEL_ID", "")
LLM_PROXY_MODEL_ID_CHEAP = os.getenv("LLM_PROXY_MODEL_ID_CHEAP", LLM_PROXY_MODEL_ID)
# The proxied models (gpt-5.x, deepseek) are *reasoning* models: max_tokens caps
# reasoning + answer combined. The judge's tight caps (tuned for Claude's direct
# output) would be spent on reasoning, leaving the JSON answer empty/truncated, so
# we add this headroom to every proxy call's budget. Bedrock (Claude) is unaffected.
LLM_PROXY_REASONING_HEADROOM = int(os.getenv("LLM_PROXY_REASONING_HEADROOM", "8000"))

# CLI-harness providers (codex_cli / claude_cli) — invoke locally-installed CLIs
# authenticated by the user's SUBSCRIPTION (never API keys).
CODEX_CLI_BIN = os.getenv("CODEX_CLI_BIN", "codex")
CLAUDE_CLI_BIN = os.getenv("CLAUDE_CLI_BIN", "claude")
CODEX_HOME = os.getenv("CODEX_HOME", "")  # if set, used as the codex auth dir
CLAUDE_CLI_HOME = os.getenv(
    "CLAUDE_CLI_HOME", ""
)  # if set, used as $HOME for claude creds
CODEX_MODEL_ID = os.getenv("CODEX_MODEL_ID", "")
CLAUDE_CLI_MODEL_ID = os.getenv("CLAUDE_CLI_MODEL_ID", "")
# Per-tier claude model selection ("opus"/"sonnet"/"haiku" or full ids) — gates
# vs routine. Fall back to CLAUDE_CLI_MODEL_ID, then the CLI default.
CLAUDE_CLI_MODEL_PREMIUM = os.getenv("CLAUDE_CLI_MODEL_PREMIUM", "")
CLAUDE_CLI_MODEL_CHEAP = os.getenv("CLAUDE_CLI_MODEL_CHEAP", "")
# claude -p reasoning budget: low/medium/high/xhigh/max ("" -> CLI default).
CLAUDE_CLI_EFFORT = os.getenv("CLAUDE_CLI_EFFORT", "")
# Turn cap for a plain (tool-less) `claude -p` call. Was hardcoded to 1, which is
# not the same as "one model response": a turn that runs long wants a
# continuation, and denying it aborts the entire call as error_max_turns with
# nothing returned — after billing for the work already done. Measured on the
# case_proposal.intent prompt (2026-08-03, identical payload and budget, arms
# interleaved): 3/5 succeeded at 1 turn, 5/5 at 3. Env-tunable because the right
# number is an empirical question that should not need a code deploy to revisit.
CLAUDE_CLI_MAX_TURNS = int(os.getenv("CLAUDE_CLI_MAX_TURNS", "3"))
REVIEW_CLI_MAX_WORKERS = int(os.getenv("REVIEW_CLI_MAX_WORKERS", "2"))
REVIEW_CLI_TIMEOUT = int(os.getenv("REVIEW_CLI_TIMEOUT", "300"))
REVIEW_CLI_MAX_RETRIES = int(os.getenv("REVIEW_CLI_MAX_RETRIES", "3"))
# Batch this many rules into ONE grading call on CLI-harness providers (which
# can't reuse prompt cache across subprocess calls). 1 disables batching (per-rule
# path; use for A/B vs batched). Only affects codex_cli / claude_cli; bedrock/proxy
# always use the per-rule + prompt-cache path.
REVIEW_RULE_BATCH_SIZE = int(os.getenv("REVIEW_RULE_BATCH_SIZE", "8"))

SOURCE_MARKDOWN_DIR = Path(
    os.getenv("SOURCE_MARKDOWN_DIR", str(BASE_DIR / "review_source_markdown"))
)
SOURCE_MARKDOWN_DIR.mkdir(parents=True, exist_ok=True)
CONVERT_SOURCE_TIMEOUT = int(os.getenv("CONVERT_SOURCE_TIMEOUT", "180"))

REVIEW_MAX_PARALLEL = int(os.getenv("REVIEW_MAX_PARALLEL", "3"))

CASEWORK_API_BASE = os.getenv("CASEWORK_API_BASE", "http://127.0.0.1:40173/api/casework")
# Central job-queue API base. The poller (a jobs consumer) claims/finalizes work
# here. Defaults to the same host so a single-node dev setup needs no extra config.
JOBS_API_BASE = os.getenv(
    "JOBS_API_BASE", "http://127.0.0.1:40173/api/jobs"
)
CASEWORK_OIDC_CLIENT_ID = os.getenv("CASEWORK_OIDC_CLIENT_ID", "")
CASEWORK_OIDC_CLIENT_SECRET = os.getenv("CASEWORK_OIDC_CLIENT_SECRET", "")
CASEWORK_OIDC_SCOPE = os.getenv("CASEWORK_OIDC_SCOPE", "")
CASEWORK_OIDC_AUDIENCE = os.getenv("CASEWORK_OIDC_AUDIENCE", "")
CASEWORK_POLLER_TOKEN = os.getenv("CASEWORK_POLLER_TOKEN", "")

MEDIA_PUBLIC_BASE = os.getenv("MEDIA_PUBLIC_BASE", "http://127.0.0.1:40173")

# ============================================================================
# Unified search — OpenSearch (bilingual EN + Nepali)
# ============================================================================
# Self-hosted OpenSearch; creds via env. USER/PASSWORD are optional (dev compose
# runs security-disabled with no creds; prod sets them). The shared
# jawafdehi_shared.search.opensearch helpers read these same env vars directly so
# both Django settings consumers and management commands stay consistent.
OPENSEARCH_URL = os.getenv("OPENSEARCH_URL", "http://localhost:9200")
OPENSEARCH_USER = os.getenv("OPENSEARCH_USER", "")
OPENSEARCH_PASSWORD = os.getenv("OPENSEARCH_PASSWORD", "")

# ============================================================================
# Case-enrichment event bus — NATS + JetStream
# ============================================================================
# OPTIONAL, and off by default. When NATS_URL is empty every publish is a logged
# no-op and no connection is ever opened, so the platform runs unchanged with no
# broker: dev and CI need nothing, and this code ships safely before the bus is
# deployed. Setting it is also the whole rollback — no image change required.
#
# Publishing is best-effort by design: a broker outage must never fail a case
# write, because the bus is transport and the case record is the system of
# record. See events/bus.py.
#
# Credentials ride in the URL (nats://user:pass@host:4222) and are per identity,
# not shared — the monolith publishes as itself, and consumers get their own.
NATS_URL = os.getenv("NATS_URL", "")

# Where the notifier consumer announces a proposal decision. Empty means "log
# only", which is what it did before this existed and remains the safe default:
# no URL, no outbound request, no behaviour change.
#
# A SECRET, despite looking like a URL. A webhook endpoint carries its own
# authentication in the path — anyone holding it can post to the channel — so it
# belongs in OpenBao and reaches the pod through an ExternalSecret, never a
# manifest and never a settings literal.
#
# Deliberately provider-agnostic in name. The first consumer of it is a Discord
# webhook, which needs a top-level `content` string; the body sent also carries
# the structured fields so a different receiver can read it without parsing prose.
CASE_EVENTS_WEBHOOK_URL = os.getenv("CASE_EVENTS_WEBHOOK_URL", "")

# Seconds. Short on purpose: this runs on a consumer's worker thread, and a
# notification is the least important thing that thread does. A slow endpoint must
# not extend the message's ack window.
#
# Parsed with a fallback rather than a bare float(), which review caught: an
# unparseable value would raise during settings import, and settings import failing
# takes down every pod in the deployment. A typo in the least important tuning knob
# in this file must not be able to do that.
try:
    CASE_EVENTS_WEBHOOK_TIMEOUT = float(os.getenv("CASE_EVENTS_WEBHOOK_TIMEOUT", "5"))
except (TypeError, ValueError):
    CASE_EVENTS_WEBHOOK_TIMEOUT = 5.0

# Public origin of the SPA, used to build the review link in that notification. A
# consumer has no request to derive it from, and ALLOWED_HOSTS is the API's host
# rather than the front end's, so this is its own setting rather than a guess.
FRONTEND_BASE_URL = os.getenv("FRONTEND_BASE_URL", "https://jawafdehi.org")
