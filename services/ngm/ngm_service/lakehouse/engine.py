"""DuckDB connection factory for the lakehouse (httpfs + iceberg + REST ATTACH).

Builds a DuckDB connection wired to:
1. the ``httpfs`` extension + an S3 secret pointed at Cloudflare R2, and
2. the ``iceberg`` extension attached to the Iceberg REST catalog (Lakekeeper).

Syntax follows the DuckDB 1.4+ iceberg-extension docs surveyed in
``think-big/shared/research/iceberg-catalog-options.md`` (read + write GA since
1.4.0):

    INSTALL httpfs; LOAD httpfs;
    INSTALL iceberg; LOAD iceberg;
    CREATE SECRET r2 (TYPE s3, KEY_ID '…', SECRET '…',
                      ENDPOINT '<acct>.r2.cloudflarestorage.com',
                      URL_STYLE 'path', USE_SSL true, REGION 'auto');
    CREATE SECRET cat (TYPE iceberg, CLIENT_ID '…', CLIENT_SECRET '…',
                       OAUTH2_SERVER_URI '…', OAUTH2_SCOPE '…');
    ATTACH 'ngm_warehouse' AS lake (TYPE iceberg, SECRET cat, ENDPOINT '<uri>');

``duckdb`` is imported lazily *inside* the factory so this module imports with
no native dependency installed — importing ``ngm.lakehouse.engine`` must always
succeed (the repository layer and tests rely on that). The actual catalog
``ATTACH`` is the live-infra step: it's stubbed behind a flag with a clear TODO,
because attaching needs a reachable Lakekeeper + R2 we don't have in CI.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ngm_service.lakehouse.config import LakehouseSettings, load_settings

if TYPE_CHECKING:  # pragma: no cover - typing only, no runtime duckdb import.
    import duckdb

# DuckDB catalog alias the silver/gold tables are reached through, e.g.
# ``SELECT * FROM lake.ngm_silver.court_cases``.
CATALOG_ALIAS = "lake"

_S3_SECRET_NAME = "ngm_r2"
_CATALOG_SECRET_NAME = "ngm_catalog"


def _require_duckdb() -> Any:
    """Import duckdb on demand with an actionable error if it's missing."""
    try:
        import duckdb
    except ModuleNotFoundError as exc:  # pragma: no cover - env-dependent.
        raise RuntimeError(
            "The 'duckdb' package is required for the lakehouse engine. "
            "Install it (poetry add duckdb) — it is declared in pyproject.toml."
        ) from exc
    return duckdb


def _quote(value: str) -> str:
    """Single-quote a literal for inline DuckDB DDL, escaping embedded quotes.

    Secret values come from trusted env, not user input, but we escape anyway so
    a stray apostrophe in a key never breaks the CREATE SECRET statement.
    """
    return "'" + value.replace("'", "''") + "'"


def build_s3_secret_sql(
    settings: LakehouseSettings, name: str = _S3_SECRET_NAME
) -> str:
    """Render the ``CREATE SECRET`` (TYPE s3) statement for the R2/S3 store.

    Uses the persistent-secret syntax DuckDB's httpfs documents; ``URL_STYLE
    'path'`` + an explicit ``ENDPOINT`` is the R2/MinIO recipe (vhost-style and
    no endpoint is the AWS-S3 path).
    """
    s3 = settings.s3
    if not s3.is_configured:
        raise RuntimeError(
            "S3/R2 credentials not configured (set AWS_ACCESS_KEY_ID / "
            "AWS_SECRET_ACCESS_KEY); see ngm.lakehouse.config."
        )
    parts = [
        "TYPE s3",
        f"KEY_ID {_quote(s3.access_key_id)}",
        f"SECRET {_quote(s3.secret_access_key)}",
        f"REGION {_quote(s3.region)}",
        f"URL_STYLE {_quote(s3.url_style)}",
        f"USE_SSL {'true' if s3.use_ssl else 'false'}",
    ]
    if s3.endpoint_host:  # R2/MinIO; omit for plain AWS S3.
        parts.append(f"ENDPOINT {_quote(s3.endpoint_host)}")
    return f"CREATE OR REPLACE SECRET {name} (\n    " + ",\n    ".join(parts) + "\n);"


def build_catalog_secret_sql(
    settings: LakehouseSettings, name: str = _CATALOG_SECRET_NAME
) -> str:
    """Render the ``CREATE SECRET`` (TYPE iceberg) statement for the REST catalog.

    Two auth modes, per the research doc:
    - OAuth2 client-credentials (Lakekeeper default): CLIENT_ID/SECRET +
      OAUTH2_SERVER_URI (+ optional OAUTH2_SCOPE).
    - Pre-issued bearer TOKEN (e.g. Polaris ``external`` OIDC mode, where the
      internal token endpoint returns 501).
    """
    cat = settings.catalog
    if not cat.is_configured:
        raise RuntimeError(
            "Iceberg catalog not configured (set ICEBERG_CATALOG_URI / "
            "ICEBERG_WAREHOUSE); see ngm.lakehouse.config."
        )
    parts = ["TYPE iceberg"]
    if cat.uses_oauth2:
        parts += [
            f"CLIENT_ID {_quote(cat.client_id)}",
            f"CLIENT_SECRET {_quote(cat.client_secret)}",
            f"OAUTH2_SERVER_URI {_quote(cat.oauth2_server_uri)}",
        ]
        if cat.oauth2_scope:
            parts.append(f"OAUTH2_SCOPE {_quote(cat.oauth2_scope)}")
    elif cat.token:
        parts.append(f"TOKEN {_quote(cat.token)}")
    else:
        raise RuntimeError(
            "Iceberg catalog auth not configured: set either OAuth2 client "
            "credentials (ICEBERG_CLIENT_ID/SECRET + ICEBERG_OAUTH2_SERVER_URI) "
            "or a bearer ICEBERG_CATALOG_TOKEN."
        )
    return f"CREATE OR REPLACE SECRET {name} (\n    " + ",\n    ".join(parts) + "\n);"


def build_attach_sql(
    settings: LakehouseSettings,
    *,
    alias: str = CATALOG_ALIAS,
    secret_name: str = _CATALOG_SECRET_NAME,
) -> str:
    """Render the ``ATTACH ... (TYPE iceberg, SECRET ..., ENDPOINT ...)`` call."""
    cat = settings.catalog
    if not cat.is_configured:
        raise RuntimeError("Iceberg catalog not configured; see ngm.lakehouse.config.")
    return (
        f"ATTACH {_quote(cat.warehouse)} AS {alias} (\n"
        f"    TYPE iceberg,\n"
        f"    SECRET {secret_name},\n"
        f"    ENDPOINT {_quote(cat.uri)}\n"
        f");"
    )


def connect(
    settings: LakehouseSettings | None = None,
    *,
    attach_catalog: bool = True,
) -> duckdb.DuckDBPyConnection:
    """Open a DuckDB connection wired for R2 + the Iceberg REST catalog.

    Steps:
    1. Open an in-memory DuckDB connection.
    2. ``INSTALL``/``LOAD`` httpfs + iceberg extensions.
    3. Create the S3/R2 secret and the catalog secret from ``settings``.
    4. If ``attach_catalog``, ``ATTACH`` the REST catalog under
       :data:`CATALOG_ALIAS`.

    ``attach_catalog`` defaults to ``True`` but the ATTACH itself is gated as a
    TODO below: it requires a reachable Lakekeeper + R2 (not present in CI), so
    we raise :class:`NotImplementedError` rather than fail with an opaque
    network error. The secret/DDL builders above are fully real and unit-checked,
    so wiring is correct the moment infra exists — flip the stub.
    """
    settings = settings or load_settings()
    duckdb = _require_duckdb()

    con = duckdb.connect(database=":memory:")
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute("INSTALL iceberg; LOAD iceberg;")
    con.execute(build_s3_secret_sql(settings))
    con.execute(build_catalog_secret_sql(settings))

    if attach_catalog:
        # TODO(live-catalog): execute build_attach_sql(settings) against a
        # reachable Lakekeeper + R2. Gated off until infra exists so callers get
        # a clear signal instead of a connection timeout deep in DuckDB. The SQL
        # is rendered & tested; only the live ATTACH (and credential vending) is
        # unverified end-to-end. Remove this guard once the catalog is stood up.
        raise NotImplementedError(
            "Live Iceberg REST catalog ATTACH is not wired yet — no reachable "
            "Lakekeeper/R2 in this environment. The ATTACH SQL is available via "
            "build_attach_sql(settings); execute it once infra exists. "
            "Pass attach_catalog=False for a secrets-only connection."
        )

    return con
