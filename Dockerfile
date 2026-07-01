# Jawafdehi platform — ONE image, ONE Django project.
#
# NES, NGM and Jawafdehi run as Django apps in ONE project (config.settings)
# served by ONE gunicorn. Build from the repo root:
#
#   docker build -f Dockerfile -t jawafdehi-platform .
#
# It installs the single `jawafdehi` project, so one `uv sync` pulls every app
# and every runtime dep (DuckDB/boto3, anthropic, jsonpatch, opensearch, etc.).
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

# Cloud SQL server CA certificate for TLS database connections (applies to all
# three databases).
COPY cloudsql-ca.pem /etc/ssl/certs/cloudsql-ca.pem
ENV DATABASE_SSL_CA_CERT_FILE=/etc/ssl/certs/cloudsql-ca.pem
ENV DATABASE_SSL_MODE=verify-ca

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1
WORKDIR /app

# Copy project metadata + every app package (flat top-level layout).
COPY pyproject.toml uv.lock manage.py ./
COPY config/ ./config/
COPY jawafdehi_shared/ ./jawafdehi_shared/
COPY entities/ ./entities/
COPY courts/ ./courts/
COPY materials/ ./materials/
COPY lakehouse/ ./lakehouse/
COPY cases/ ./cases/
COPY review/ ./review/
COPY search/ ./search/
COPY discovery/ ./discovery/
COPY static/ ./static/

# Install the single `jawafdehi` project + full dependency closure, no dev group.
RUN uv sync --frozen --no-dev

ENV DJANGO_SETTINGS_MODULE=config.settings

# Collect static at build time (skips the prod OIDC/secret guards via the
# _BUILD_TIME_COMMANDS list in settings). STATIC_ROOT resolves under BASE_DIR
# (repo root) where the admin/jazzmin/static assets live.
RUN DEBUG=False SECRET_KEY=foo-bar ALLOWED_HOSTS=portal.jawafdehi.org \
    uv run python manage.py collectstatic --noinput

EXPOSE 8080
CMD ["uv", "run", \
     "gunicorn", "config.wsgi:application", \
     "--bind", "0.0.0.0:8080", "--workers", "2", "--threads", "4", \
     "--timeout", "60", "--access-logfile", "-", "--error-logfile", "-"]
