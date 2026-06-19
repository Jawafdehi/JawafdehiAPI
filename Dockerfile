FROM python:3.12-slim

WORKDIR /app

# Install system dependencies for psycopg2 (gcc, postgresql-client) and the
# process_queue cron (git: clones/commits/pushes the nes-db repo).
RUN apt-get update && apt-get install -y \
    gcc \
    postgresql-client \
    git \
    && rm -rf /var/lib/apt/lists/*

# Cloud SQL server CA certificate for TLS database connections.
COPY cloudsql-ca.pem /etc/ssl/certs/cloudsql-ca.pem

ENV DATABASE_SSL_CA_CERT_FILE=/etc/ssl/certs/cloudsql-ca.pem
ENV DATABASE_SSL_MODE=verify-ca

RUN pip install poetry

COPY pyproject.toml poetry.lock ./
RUN poetry config virtualenvs.create false && poetry install --only main --extras llm-all --no-interaction --no-root

COPY manage.py ./
COPY gunicorn.conf.py ./
COPY config ./config
COPY cases ./cases
COPY case_workflows ./case_workflows
COPY nesq ./nesq
COPY ngm ./ngm
COPY review ./review
COPY llm ./llm
COPY sourcing ./sourcing
COPY content ./content
COPY static ./static
COPY templates ./templates

# Fail the build early if the app can't load (missing module, broken URLconf, etc.)
RUN DEBUG=False SECRET_KEY=foo-bar ALLOWED_HOSTS=portal.jawafdehi.org python manage.py check

# Collect static files
RUN DEBUG=False SECRET_KEY=foo-bar ALLOWED_HOSTS=portal.jawafdehi.org python manage.py collectstatic --noinput

EXPOSE 8080

CMD ["gunicorn", "-c", "gunicorn.conf.py", "config.wsgi:application"]
