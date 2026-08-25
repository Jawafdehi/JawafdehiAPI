# Jawafdehi platform — ONE image, ONE Django project.
#
# NES, NGM and Jawafdehi run as Django apps in ONE project (config.settings)
# served by ONE gunicorn ASGI deployment. Build from the repo root:
#
#   docker build -f Dockerfile -t jawafdehi-platform .
#
# It installs the single `jawafdehi` project, so one `uv sync` pulls every app
# and every runtime dep (DuckDB/boto3, anthropic, jsonpatch, opensearch, etc.).
ARG PYTHON_IMAGE=python:3.12-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.10.9@sha256:10902f58a1606787602f303954cea099626a4adb02acbac4c69920fe9d278f82

FROM ${UV_IMAGE} AS uv

FROM ${PYTHON_IMAGE} AS builder

COPY --from=uv /uv /uvx /bin/
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    git \
    && rm -rf /var/lib/apt/lists/*

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1
WORKDIR /app

# Copy project metadata + every app package (flat top-level layout).
#
# This list is explicit, so a NEW APP MUST BE ADDED HERE as well as to
# INSTALLED_APPS and the wheel `packages` list — three places, none of which
# fail at test time. tests/test_app_package_names.py asserts all three agree.
COPY pyproject.toml uv.lock manage.py ./
COPY config/ ./config/
COPY jawafdehi_shared/ ./jawafdehi_shared/
COPY entities/ ./entities/
COPY courts/ ./courts/
COPY materials/ ./materials/
COPY lakehouse/ ./lakehouse/
COPY cases/ ./cases/
COPY case_tags/ ./case_tags/
COPY review/ ./review/
COPY case_proposals/ ./case_proposals/
COPY newsletter/ ./newsletter/
COPY jobs/ ./jobs/
COPY case_events/ ./case_events/
COPY llm/ ./llm/
COPY jawafdehi_mcp/ ./jawafdehi_mcp/
COPY search/ ./search/
COPY discovery/ ./discovery/
COPY content/ ./content/
COPY static/ ./static/
COPY templates/ ./templates/

# Install the single `jawafdehi` project + full dependency closure, no dev group.
RUN uv sync --frozen --no-dev

ENV DJANGO_SETTINGS_MODULE=config.settings

# Collect static at build time (skips the prod OIDC/secret guards via the
# _BUILD_TIME_COMMANDS list in settings). STATIC_ROOT resolves under BASE_DIR
# (repo root) where the admin/jazzmin/static assets live.
RUN DEBUG=False SECRET_KEY=foo-bar ALLOWED_HOSTS=portal.jawafdehi.org \
    uv run python manage.py collectstatic --noinput

FROM ${PYTHON_IMAGE} AS runtime

COPY --from=uv /uv /uvx /bin/
RUN apt-get update && apt-get install -y --no-install-recommends \
    antiword \
    # Devanagari shaping for the composed Open Graph cards (cases/og_cards.py).
    # Pillow VENDORS Raqm but dlopen()s its backends at runtime: libharfbuzz
    # ships inside the Pillow wheel, libfribidi does NOT. Without this package
    # PIL.features.check("raqm") is False and Pillow silently falls back to
    # unshaped rendering — Nepali names come out with their matras and conjuncts
    # detached, with no error and no failed test. ~30 KB; do not drop it.
    libfribidi0 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 10001 jawafdehi \
    && useradd --system --uid 10001 --gid jawafdehi \
       --create-home --home-dir /home/jawafdehi jawafdehi

# Cloud SQL server CA certificate for TLS database connections (applies to all
# three databases).
COPY cloudsql-ca.pem /etc/ssl/certs/cloudsql-ca.pem
ENV DATABASE_SSL_CA_CERT_FILE=/etc/ssl/certs/cloudsql-ca.pem
ENV DATABASE_SSL_MODE=verify-ca
ENV PYTHONUNBUFFERED=1
ENV DJANGO_SETTINGS_MODULE=config.settings

WORKDIR /app
COPY --from=builder --chown=jawafdehi:jawafdehi /app /app

USER jawafdehi

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8080/mcp/health').status==200 else 1)"
CMD ["/app/.venv/bin/gunicorn", "config.asgi:application", \
     "--config", "config/gunicorn.py", \
     "--worker-class", "config.asgi_worker.BoundedUvicornWorker", \
     "--bind", "0.0.0.0:8080", "--workers", "2", \
     "--timeout", "60", "--access-logfile", "-", "--error-logfile", "-"]
