"""Unified Django settings for the CONSOLIDATED Jawafdehi platform (monolith).

The three formerly-separate Django services — NES, NGM, Jawafdehi — now run as
Django *apps* inside ONE project / one process / one image, with ONE settings
module (this file), one ``wsgi``, one ``urls``. In-process inter-app calls
replace the REST hops that used to sit between them.

What is KEPT from the database-per-service design: the THREE separate Postgres
databases. ``DATABASES`` declares ``default`` (Jawafdehi), ``nes`` and ``ngm``;
``monolith.config.db_router.ServiceDatabaseRouter`` pins each app's models to
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

The supporting modules ``config.auth`` / ``config.middleware`` /
``config.structlog_config`` are the Jawafdehi project's existing top-level
``config`` package (it stays importable as ``config`` via the editable install
of services/jawafdehi). Jawafdehi app code that does ``from config.auth import
...`` therefore keeps working unchanged. This settings module lives in the
NEW umbrella package ``monolith.config`` (named ``monolith`` rather than
``platform`` because ``platform`` is a Python stdlib module and a top-level
package of that name would shadow it).
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
from config.structlog_config import configure_structlog

load_dotenv()

configure_structlog()

_sentry_dsn = os.getenv("SENTRY_DSN")
if _sentry_dsn:
    sentry_sdk.init(
        dsn=_sentry_dsn,
        integrations=[DjangoIntegration()],
        traces_sample_rate=float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "1.0")),
        send_default_pii=False,
        environment=os.getenv("SENTRY_ENVIRONMENT", "production"),
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


# BASE_DIR points at the Jawafdehi app tree (services/jawafdehi), which is where
# the templates / static / media that the running project serves live. The
# umbrella `monolith` package sits at the repo root; BASE_DIR is anchored on the
# Jawafdehi service dir so STATICFILES_DIRS / MEDIA_ROOT / SOURCE_MARKDOWN_DIR
# resolve to the same paths the Jawafdehi project used.
BASE_DIR = Path(__file__).resolve().parent.parent.parent / "services" / "jawafdehi"

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
    SECRET_KEY = SECRET_KEY or "dev-insecure-monolith-key"

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
    # Jazzmin must precede django.contrib.admin (admin theme).
    "jazzmin",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Public crawl/discovery surface uses Django's Sitemaps framework (the new
    # IRI-driven sitemap lives in monolith.discovery). The platform has no Site
    # rows — the discovery Sitemap emits the canonical IRI verbatim as loc and
    # never reads the Sites framework — so django.contrib.sites is intentionally
    # NOT installed; only the sitemaps app is needed.
    "django.contrib.sitemaps",
    # Postgres lookups (JSONB containment etc.) used by the NES entity search
    # push-down. Harmless on sqlite (the DB-less test / local fallback): it only
    # registers field lookups. The NES JSONB GIN index is NOT created via
    # Meta.indexes; the 0001_initial migration splits it with
    # SeparateDatabaseAndState so the real ``CREATE INDEX ... USING gin`` runs
    # only on PostgreSQL and no-ops on sqlite (see nes_service.entities).
    "django.contrib.postgres",
    "rest_framework",
    "drf_spectacular",
    "django_filters",
    "corsheaders",
    "auditlog",
    "rules.apps.AutodiscoverRulesConfig",
    # ── NES app (routes to the `nes` DB) ─────────────────────────────────────
    "nes_service.entities",
    # ── NGM apps (route to the `ngm` DB) ─────────────────────────────────────
    "ngm_service.courts",
    "ngm_service.materials",
    # ── Jawafdehi apps (route to the `default` DB) ───────────────────────────
    "cases",
    "review",
    # ── Unified search (platform-wide; queries all three domains' indices) ────
    "monolith.search",
    # ── Public discovery (Sitemaps + ResourceSync, IRI-driven; no models) ─────
    "monolith.discovery",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "config.middleware.RequestIdMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "auditlog.middleware.AuditlogMiddleware",
]

ROOT_URLCONF = "monolith.config.urls"
WSGI_APPLICATION = "monolith.config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
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
# the NES/NGM aliases ALSO fall back to sqlite so the whole monolith runs
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


DATABASES = {
    "default": dj_database_url.config(default="sqlite:///db.sqlite3"),
    # nes DB — the NES entity store.
    "nes": (
        dj_database_url.parse(os.getenv("NES_DB_URL"))
        if os.getenv("NES_DB_URL")
        else _sqlite_alias("db_nes.sqlite3", "test_nes.sqlite3")
    ),
    # ngm DB — courts/materials.
    "ngm": (
        dj_database_url.parse(os.getenv("NGM_DATABASE_URL"))
        if os.getenv("NGM_DATABASE_URL")
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

# Apply SSL options + connection pooling to every Postgres database.
for db_key in DATABASES:
    _apply_db_ssl_options(DATABASES[db_key])
    if DATABASES[db_key].get("ENGINE") == "django.db.backends.postgresql":
        DATABASES[db_key]["CONN_MAX_AGE"] = 60
        DATABASES[db_key]["CONN_HEALTH_CHECKS"] = True

DATABASE_ROUTERS = ["monolith.config.db_router.ServiceDatabaseRouter"]

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

AUTHENTICATION_BACKENDS = [
    "rules.permissions.ObjectPermissionBackend",
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
OIDC_SERVICE_ACCOUNT_SUBJECTS = get_env_list("OIDC_SERVICE_ACCOUNT_SUBJECTS")
OIDC_SERVICE_ACCOUNT_ROLE = os.getenv("OIDC_SERVICE_ACCOUNT_ROLE", "contributor")

if not DEBUG and not TESTING and not _running_build_command and not OIDC_ISSUER:
    raise ImproperlyConfigured(
        "OIDC_ISSUER environment variable must be set in production. "
        "OIDC (Zitadel) is the only authentication method for the API."
    )

# ---------------------------------------------------------------------------
# REST Framework — single config. OIDC is the sole API authenticator. The
# permission default is read-public / authenticated-write (the NES/NGM planes
# relied on ReadOnlyOrAuthenticatedWrite; Jawafdehi's views set their own
# per-view permissions on top, so a global read-public default is compatible
# with them too). Pagination/throttle/schema carried from Jawafdehi.
# ---------------------------------------------------------------------------
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "jawafdehi_shared.auth.oidc.OIDCAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "jawafdehi_shared.drf.base.ReadOnlyOrAuthenticatedWrite",
    ],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 20,
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
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
if not TESTING:
    REST_FRAMEWORK["DEFAULT_THROTTLE_CLASSES"] = [
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
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
]
CORS_ALLOW_CREDENTIALS = True

if not CSRF_TRUSTED_ORIGINS:
    CSRF_TRUSTED_ORIGINS = CORS_ALLOWED_ORIGINS.copy()

# Security headers / TLS enforcement
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
X_FRAME_OPTIONS = "DENY"

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
# NES/NGM config. After the monolith collapse NES and NGM run IN-PROCESS — the
# nes_resolver seam + in-process ORM calls, NOT REST hops. The dead NGM REST
# proxy (and its NGM_API_BASE_URL / NGM_API_TOKEN / NGM_API_TIMEOUT_SECONDS /
# NES_DB_PATH settings) was removed; the only NGM knob still read is the gated
# query row cap. NES_API_URL is retained for the standalone enrich_ciaa_related_
# entities NES-search linker (a separate legacy command, not the in-process seam).
# ---------------------------------------------------------------------------
NES_API_URL = os.getenv("NES_API_URL", "https://nes.jawafdehi.org/api")
NGM_QUERY_MAX_ROWS = int(os.getenv("NGM_QUERY_MAX_ROWS", "500"))

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
    "search_model": ["cases.Case", "cases.DocumentSource"],
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
        "cases.DocumentSource": "fas fa-file-alt",
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
JAWAFDEHI_API_BASE = os.getenv("JAWAFDEHI_API_BASE", "https://portal.jawafdehi.org/api")
JAWAFDEHI_API_TOKEN = os.getenv("JAWAFDEHI_API_TOKEN", "")
JAWAFDEHI_S3_BASE = os.getenv("JAWAFDEHI_S3_BASE", "https://s3.jawafdehi.org")

AWS_PROFILE = os.getenv("REVIEW_AWS_PROFILE", os.getenv("AWS_PROFILE", ""))
AWS_REGION = os.getenv("AWS_REGION", "us-west-2")
BEDROCK_MODEL_ID = os.getenv("BEDROCK_MODEL_ID", "global.anthropic.claude-opus-4-8")
BEDROCK_MAX_WORKERS = int(os.getenv("BEDROCK_MAX_WORKERS", "8"))

SOURCE_MARKDOWN_DIR = Path(
    os.getenv("SOURCE_MARKDOWN_DIR", str(BASE_DIR / "review_source_markdown"))
)
SOURCE_MARKDOWN_DIR.mkdir(parents=True, exist_ok=True)
CONVERT_SOURCE_TIMEOUT = int(os.getenv("CONVERT_SOURCE_TIMEOUT", "180"))

REVIEW_MAX_PARALLEL = int(os.getenv("REVIEW_MAX_PARALLEL", "3"))

CASEWORK_API_BASE = os.getenv("CASEWORK_API_BASE", "http://127.0.0.1:40173/api/casework")
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
