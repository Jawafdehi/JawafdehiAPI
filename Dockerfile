# Consolidated Jawafdehi platform — ONE image for the whole monolith.
#
# Replaces the three former per-service Dockerfiles (services/{nes,ngm,jawafdehi}/
# Dockerfile). NES, NGM and Jawafdehi now run as Django apps in ONE project
# (monolith.config.settings) served by ONE gunicorn. Build from the repo root:
#
#   docker build -f Dockerfile -t jawafdehi-platform .
#
# It installs the umbrella project `jawafdehi-monolith`, which depends on all
# three service packages + shared, so a single `uv sync` pulls every app and
# every app's runtime deps (DuckDB/boto3 from NGM, langchain/anthropic from
# Jawafdehi, jsonpatch from NES, etc.).
FROM python:3.12-slim

# uv: fast, lockfile-driven installs.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# psycopg2-binary ships wheels, but keep the postgres client + a compiler, and
# git (needed for Jawafdehi's git-sourced deps: nepal-entity-service, likhit).
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    git \
    postgresql-client \
    && rm -rf /var/lib/apt/lists/*

# Cloud SQL server CA certificate for TLS database connections (carried over
# from the Jawafdehi image; applies to all three databases now).
COPY services/jawafdehi/cloudsql-ca.pem /etc/ssl/certs/cloudsql-ca.pem
ENV DATABASE_SSL_CA_CERT_FILE=/etc/ssl/certs/cloudsql-ca.pem
ENV DATABASE_SSL_MODE=verify-ca

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1
WORKDIR /app

# Copy the workspace metadata + the umbrella package + ALL members (one image
# now needs every service's code + the shared package).
COPY pyproject.toml uv.lock ./
COPY monolith/ ./monolith/
COPY manage.py ./manage.py
COPY shared/ ./shared/
COPY services/ ./services/

# Install the umbrella project (jawafdehi-monolith) and its full dependency
# closure — all three apps + shared — without the dev group.
RUN uv sync --frozen --no-dev

ENV DJANGO_SETTINGS_MODULE=monolith.config.settings

# Collect static at build time (build-time command — skips the prod OIDC/secret
# guards via the _BUILD_TIME_COMMANDS list in settings). STATIC_ROOT resolves
# under services/jawafdehi (BASE_DIR) where the admin/jazzmin/static assets live.
RUN DEBUG=False SECRET_KEY=foo-bar ALLOWED_HOSTS=portal.jawafdehi.org \
    uv run python manage.py collectstatic --noinput

EXPOSE 8080
CMD ["uv", "run", \
     "gunicorn", "monolith.config.wsgi:application", \
     "--bind", "0.0.0.0:8080", "--workers", "2", "--threads", "4", \
     "--timeout", "60", "--access-logfile", "-", "--error-logfile", "-"]
