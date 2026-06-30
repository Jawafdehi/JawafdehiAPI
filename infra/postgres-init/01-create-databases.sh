#!/bin/bash
# Create one database per service (+ zitadel) in the single local Postgres instance.
# Database-per-service isolation: each microservice connects ONLY to its own DB;
# cross-service access is REST-only (no cross-DB joins/FKs).
#
# Runs automatically on first container start (empty pgdata volume) via
# /docker-entrypoint-initdb.d. To re-run, wipe the volume: `docker compose down -v`.
set -euo pipefail

for db in jawafdehi nes ngm zitadel; do
  echo "Creating database: $db"
  psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" <<-SQL
    SELECT 'CREATE DATABASE $db'
      WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = '$db')\gexec
SQL
done

# NGM's schema uses pg_trgm (gin_trgm_ops) full-text indexes on court_cases, so
# the extension must exist in the ngm DB before its tables are created.
echo "Enabling pg_trgm in ngm"
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" -d ngm -c "CREATE EXTENSION IF NOT EXISTS pg_trgm;"

echo "Per-service databases ready: jawafdehi, nes, ngm, zitadel"
