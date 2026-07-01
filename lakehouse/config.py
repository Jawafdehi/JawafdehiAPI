"""Lakehouse connection settings (R2/S3 object store + Iceberg REST catalog).

Read entirely from environment variables so the same code runs against
Cloudflare R2 in prod, a MinIO/iceberg-rest-fixture in dev/CI, or AWS S3, with
no source changes — only env. Names mirror the AWS SDK / DuckDB httpfs
conventions (``AWS_ACCESS_KEY_ID`` etc.) plus NGM-specific catalog vars.

The medallion plan (``ngm-data-lake-plan.md``) and catalog research
(``iceberg-catalog-options.md``) pin the concrete stack:
- Object store: Cloudflare R2 (S3-compatible) — ``S3_ENDPOINT_URL`` set to the
  R2 endpoint; path-style addressing; no STS (R2 has none).
- Catalog: Lakekeeper (Iceberg REST), reached at ``ICEBERG_CATALOG_URI`` with a
  warehouse name and OAuth2/bearer auth.

This module is pure config (a frozen dataclass + an ``env``-loader). It pulls in
no heavy deps (no duckdb/boto3) so it imports cheaply everywhere.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# --- env var names (single source of truth) --------------------------------

# Object store (R2/S3). Endpoint is the R2 S3 API host, e.g.
# "https://<accountid>.r2.cloudflarestorage.com".
ENV_S3_ENDPOINT_URL = "S3_ENDPOINT_URL"
ENV_AWS_ACCESS_KEY_ID = "AWS_ACCESS_KEY_ID"
ENV_AWS_SECRET_ACCESS_KEY = "AWS_SECRET_ACCESS_KEY"
ENV_AWS_REGION = "AWS_REGION"  # R2 ignores region; DuckDB still wants a value.
ENV_S3_USE_SSL = "S3_USE_SSL"
ENV_S3_URL_STYLE = "S3_URL_STYLE"  # "path" for R2/MinIO, "vhost" for AWS S3.

# Bucket names per medallion zone. Bronze is the existing raw landing zone
# (R2 ``uploads/`` today); silver/gold may be the same bucket with different
# prefixes or distinct buckets — left to deployment.
ENV_BRONZE_BUCKET = "NGM_BRONZE_BUCKET"
ENV_SILVER_BUCKET = "NGM_SILVER_BUCKET"
ENV_GOLD_BUCKET = "NGM_GOLD_BUCKET"

# Iceberg REST catalog (Lakekeeper).
ENV_ICEBERG_CATALOG_URI = "ICEBERG_CATALOG_URI"  # e.g. "https://catalog/catalog"
ENV_ICEBERG_WAREHOUSE = "ICEBERG_WAREHOUSE"  # warehouse/project name in the catalog
ENV_ICEBERG_TOKEN = "ICEBERG_CATALOG_TOKEN"  # pre-issued bearer token (optional)
ENV_ICEBERG_OAUTH2_SERVER_URI = "ICEBERG_OAUTH2_SERVER_URI"  # OIDC token endpoint
ENV_ICEBERG_CLIENT_ID = "ICEBERG_CLIENT_ID"
ENV_ICEBERG_CLIENT_SECRET = "ICEBERG_CLIENT_SECRET"
ENV_ICEBERG_OAUTH2_SCOPE = "ICEBERG_OAUTH2_SCOPE"

# Default Iceberg namespace the silver tables live under, e.g. catalog path
# ``ngm_silver.court_cases``.
ENV_SILVER_NAMESPACE = "NGM_SILVER_NAMESPACE"
DEFAULT_SILVER_NAMESPACE = "ngm_silver"


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class S3Settings:
    """Object-store (R2/S3) connection settings for DuckDB httpfs + boto3."""

    endpoint_url: str | None
    access_key_id: str | None
    secret_access_key: str | None
    region: str
    use_ssl: bool
    url_style: str  # "path" (R2/MinIO) or "vhost" (AWS S3)

    @property
    def is_configured(self) -> bool:
        """True when credentials are present (endpoint may be blank for AWS S3)."""
        return bool(self.access_key_id and self.secret_access_key)

    @property
    def endpoint_host(self) -> str | None:
        """Bare host (no scheme) — DuckDB's ``ENDPOINT`` wants host, not URL."""
        if not self.endpoint_url:
            return None
        return self.endpoint_url.split("://", 1)[-1].rstrip("/")


@dataclass(frozen=True)
class CatalogSettings:
    """Iceberg REST catalog (Lakekeeper) connection + auth settings."""

    uri: str | None
    warehouse: str | None
    token: str | None
    oauth2_server_uri: str | None
    client_id: str | None
    client_secret: str | None
    oauth2_scope: str | None

    @property
    def is_configured(self) -> bool:
        return bool(self.uri and self.warehouse)

    @property
    def uses_oauth2(self) -> bool:
        """OAuth2 client-credentials flow (vs a pre-issued bearer ``token``)."""
        return bool(self.client_id and self.client_secret and self.oauth2_server_uri)


@dataclass(frozen=True)
class LakehouseSettings:
    """Top-level lakehouse config: object store + catalog + bucket layout."""

    s3: S3Settings
    catalog: CatalogSettings
    bronze_bucket: str | None
    silver_bucket: str | None
    gold_bucket: str | None
    silver_namespace: str

    @property
    def is_configured(self) -> bool:
        """Both the object store and the catalog must be wired to query live."""
        return self.s3.is_configured and self.catalog.is_configured


def load_settings() -> LakehouseSettings:
    """Build :class:`LakehouseSettings` from the process environment.

    Never raises on missing values — returns a partially-populated settings
    object whose ``is_configured`` flags tell callers what's available. The
    engine factory is responsible for raising a clear error when something it
    needs is absent (so import-time stays side-effect free).
    """
    s3 = S3Settings(
        endpoint_url=os.getenv(ENV_S3_ENDPOINT_URL),
        access_key_id=os.getenv(ENV_AWS_ACCESS_KEY_ID),
        secret_access_key=os.getenv(ENV_AWS_SECRET_ACCESS_KEY),
        region=os.getenv(ENV_AWS_REGION, "auto"),  # R2 convention is "auto".
        use_ssl=_bool_env(ENV_S3_USE_SSL, default=True),
        url_style=os.getenv(ENV_S3_URL_STYLE, "path"),
    )
    catalog = CatalogSettings(
        uri=os.getenv(ENV_ICEBERG_CATALOG_URI),
        warehouse=os.getenv(ENV_ICEBERG_WAREHOUSE),
        token=os.getenv(ENV_ICEBERG_TOKEN),
        oauth2_server_uri=os.getenv(ENV_ICEBERG_OAUTH2_SERVER_URI),
        client_id=os.getenv(ENV_ICEBERG_CLIENT_ID),
        client_secret=os.getenv(ENV_ICEBERG_CLIENT_SECRET),
        oauth2_scope=os.getenv(ENV_ICEBERG_OAUTH2_SCOPE),
    )
    return LakehouseSettings(
        s3=s3,
        catalog=catalog,
        bronze_bucket=os.getenv(ENV_BRONZE_BUCKET),
        silver_bucket=os.getenv(ENV_SILVER_BUCKET),
        gold_bucket=os.getenv(ENV_GOLD_BUCKET),
        silver_namespace=os.getenv(ENV_SILVER_NAMESPACE, DEFAULT_SILVER_NAMESPACE),
    )
