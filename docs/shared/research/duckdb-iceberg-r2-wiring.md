# DuckDB + Iceberg + Cloudflare R2 Lakehouse Wiring

**Status:** Research / decision input for human review.
**Research date:** 2026-06-27. **Versions current as of mid-2026:** DuckDB stable **1.5.4** (LTS line 1.4.5), Apache Iceberg docs "latest" = **1.11.0** (table spec v1/v2/v3), PyIceberg **0.11.1**, Lakekeeper catalog **v0.12.4** / Helm chart **0.11.0** (appVersion 0.12.x), Cloudflare R2 Data Catalog **public beta** (not GA).

This document verifies the live wiring our code skeleton stubbed: how DuckDB attaches an Iceberg REST catalog over R2, whether R2 Data Catalog can replace self-hosted Lakekeeper, who writes the silver tables, how partitioning/maintenance works without Spark, and how Lakekeeper deploys on K8s with Postgres + Zitadel OIDC.

Confidence levels (high/medium/low) and source URLs are attached to each claim. Items flagged **VERIFY** are load-bearing and not fully confirmable from official docs.

---

## TL;DR recommendation

1. **DuckDB CAN write to an Iceberg REST catalog** as of DuckDB 1.4.0 (Sep 2025); 1.5.x adds MERGE INTO + schema evolution. The "DuckDB is read-only" assumption in the skeleton is **outdated** — but only catalog-attached tables are writable; the `iceberg_scan` path is read-only.
2. **Catalog decision:** Self-host **Lakekeeper** if you need fine-grained / multi-tenant access control and IdP (Zitadel) auth — which our permission model implies. Use **R2 Data Catalog** only if you can live with coarse R2-API-token auth and want zero ops. They are not mutually exclusive on storage: **Lakekeeper natively supports R2 as the storage backend**, so "Lakekeeper catalog + R2 storage" is the recommended middle path.
3. **Write path:** A **DuckDB-only (or PyIceberg-only) silver writer is production-viable today**, but ONLY because compaction must be handled out-of-band — neither DuckDB nor PyIceberg can compact. With self-hosted Lakekeeper OSS there is no managed compaction, so you need a maintenance engine (Trino/Flink/Spark via the same REST catalog) or Lakekeeper Plus (paid). Keep that escape hatch.
4. **Partitioning:** `PARTITIONED BY (court, day(event_date))` is idiomatic. There is **no built-in `fiscal_year` transform** — materialize a `fiscal_year` column in ETL and identity-partition on it. Partition evolution lets you change the spec later without rewriting history.
5. **Zitadel + Lakekeeper:** works via Lakekeeper's **generic OIDC** support (`LAKEKEEPER__OPENID_PROVIDER_URI` + `OPENID_AUDIENCE`), but Zitadel is **not** an officially documented provider — you're on the generic path (community Terraform examples exist).

---

## 1. DuckDB Iceberg extension — exact SQL

### Version requirement
- The iceberg extension auto-installs/loads on first use; manual form is `INSTALL iceberg; LOAD iceberg;`. (`FROM duckdb_secrets` is unrelated — it only lists configured secrets.) — high — https://duckdb.org/docs/current/core_extensions/iceberg/overview.html
- **REST-catalog attach + write requires DuckDB >= 1.4.0** (Cloudflare states this verbatim for R2: "DuckDB 1.4.0 or greater is required to attach and write to Iceberg REST Catalogs"). Use a recent **1.5.x** for the full write feature set and run `UPDATE EXTENSIONS;` (the extension updates between core releases). — high — https://developers.cloudflare.com/r2/data-catalog/config-examples/duckdb/ ; https://duckdb.org/docs/current/core_extensions/iceberg/overview.html
- Historical note: REST-catalog attach was previewed at 1.2.1 (Mar 2025) and then required the `core_nightly` repo. That constraint is gone in current stable. — high — https://duckdb.org/2025/03/14/preview-amazon-s3-tables.html

### ATTACH an Iceberg REST catalog
```sql
INSTALL iceberg; LOAD iceberg;      -- usually automatic
LOAD httpfs;                        -- needed for object-store access

ATTACH 'warehouse' AS iceberg_catalog (
    TYPE iceberg,
    SECRET iceberg_secret,           -- references a TYPE iceberg secret (below)
    ENDPOINT 'https://catalog.example.com'
);
```
`ENDPOINT` is the REST catalog URI; `SECRET` names a previously created `TYPE iceberg` secret. Recognized alternatives on ATTACH: `ENDPOINT_TYPE` (e.g. `s3_tables`, `glue`) instead of a literal endpoint, and `ACCESS_DELEGATION_MODE 'vended_credentials'` (Polaris/credential-vending catalogs). — high — https://duckdb.org/docs/current/core_extensions/iceberg/iceberg_rest_catalogs.html

### CREATE SECRET for the catalog (OAuth2 or bearer token)
OAuth2 client-credentials (Lakekeeper / Polaris style — Lakekeeper also takes `OAUTH2_SCOPE`):
```sql
CREATE SECRET iceberg_secret (
    TYPE iceberg,
    CLIENT_ID 'admin',
    CLIENT_SECRET 'password',
    OAUTH2_SERVER_URI 'https://catalog.example.com/v1/oauth/tokens'
    -- , OAUTH2_SCOPE 'lakekeeper'   -- Lakekeeper variant
);
```
Direct bearer token (this is the **R2 Data Catalog** form — token is an R2 API token):
```sql
CREATE SECRET iceberg_secret (
    TYPE iceberg,
    TOKEN 'r2_api_token_value'
);
```
— high — https://duckdb.org/docs/current/core_extensions/iceberg/iceberg_rest_catalogs.html

### CREATE SECRET for R2 storage (S3-compatible) — IMPORTANT CORRECTION
The skeleton assumed a separate `TYPE s3 ... ENDPOINT ... URL_STYLE 'path'` storage secret. **For the REST-catalog flow this is generally NOT needed:** a REST catalog *vends* storage credentials, so storage auth flows through the catalog token (R2 uses only the `TYPE iceberg, TOKEN` secret; Polaris uses `ACCESS_DELEGATION_MODE 'vended_credentials'`). No `URL_STYLE 'path'` storage secret appears on any Iceberg REST-catalog doc page. — high (that the REST docs don't show it) / medium (that R2 never needs one) — https://duckdb.org/docs/current/core_extensions/iceberg/iceberg_rest_catalogs.html

A `TYPE s3` secret is only documented for the **path-based read** (`iceberg_scan`, read-only) flow, and even there the S3 Tables example uses `KEY_ID`/`SECRET`/`REGION` with **no `ENDPOINT` and no `URL_STYLE`**:
```sql
CREATE SECRET (TYPE s3, KEY_ID '...', SECRET '...', REGION 'us-east-1');
-- or
CREATE SECRET (TYPE s3, PROVIDER credential_chain);
```
— high — https://duckdb.org/docs/current/core_extensions/iceberg/amazon_s3_tables.html ; https://duckdb.org/2025/03/14/preview-amazon-s3-tables.html

**VERIFY (low confidence):** The `TYPE s3 ... ENDPOINT '<accountid>.r2.cloudflarestorage.com' ... URL_STYLE 'path'` form you described is the **general httpfs S3 secret** (documented on the httpfs/secrets pages, not the Iceberg pages). It is the right shape if you ever read R2 Iceberg metadata *without* a catalog, or use Lakekeeper with non-vended (remote-signing/client-managed) credentials. It is **not** part of the R2-Data-Catalog attach workflow. Confirm against the httpfs S3 secret docs before relying on the exact `URL_STYLE 'path'` spelling.

### Read vs Write status (current)
- **Catalog-attached tables are WRITABLE** (not read-only). Verbatim: attaching a REST catalog "unlocks the full feature set, including writing." Path-based `iceberg_scan` "requires no catalog and is read-only." — high — https://duckdb.org/docs/current/core_extensions/iceberg/overview.html ; https://duckdb.org/docs/current/core_extensions/iceberg/writing
- Iceberg writing shipped in **DuckDB 1.4.0 LTS (2025-09-16)** (CREATE TABLE + COPY/INSERT at launch). **1.5.3 (2026-05-29)** added full **MERGE INTO** and metadata-only **schema evolution** (ALTER TABLE add/drop/rename/alter-type). Current docs list CREATE/DROP SCHEMA & TABLE, INSERT, UPDATE, DELETE, MERGE INTO, ALTER TABLE, and PARTITIONED BY. — high — https://duckdb.org/2025/09/16/announcing-duckdb-140.html ; https://duckdb.org/2026/05/29/new-iceberg-features.html ; https://duckdb.org/docs/current/core_extensions/iceberg/writing
- **DuckDB write limitations** (verbatim):
  - `UPDATE`/`DELETE` "write positional deletes only; copy-on-write is not supported" (merge-on-read only).
  - `write.target-file-size-bytes` / `write.parquet.row-group-size-bytes` "are not honored for partitioned tables and raise an error" unless you set `ignore_target_file_size_for_partitioned_tables` / `ignore_row_group_size_for_partitioned_tables = true`.
  - **No compaction / `rewrite_data_files`** — absent from all DuckDB Iceberg docs.
  - No partition evolution, no `WHEN NOT MATCHED BY SOURCE` MERGE clause.
  - Storage backends documented: S3, S3 Tables, GCS (R2 works via the S3-compatible path; the "S3/S3 Tables/GCS only" sentence mildly conflicts with the documented R2 support — treat as docs ambiguity). — high — https://duckdb.org/docs/current/core_extensions/iceberg/writing
- **Stale Cloudflare note:** R2's DuckDB page says "DuckDB does not currently support DELETE on partitioned tables" — true at its pinned 1.4.0 baseline, **superseded in DuckDB 1.5.x** (UPDATE/DELETE now work on partitioned + unpartitioned tables). Pin and test your exact DuckDB version against R2. — high — https://developers.cloudflare.com/r2/data-catalog/config-examples/duckdb/ ; https://duckdb.org/docs/current/core_extensions/iceberg/writing

### Minimal end-to-end example (R2 Data Catalog)
```sql
INSTALL iceberg; LOAD iceberg; LOAD httpfs;
CREATE SECRET r2_cat (TYPE iceberg, TOKEN 'r2_api_token');
ATTACH '<WAREHOUSE>' AS lake (TYPE iceberg, ENDPOINT '<CATALOG_URI>');
CREATE SCHEMA IF NOT EXISTS lake.silver;
CREATE TABLE lake.silver.cases (id BIGINT, court VARCHAR, event_date DATE, fiscal_year INT)
  PARTITIONED BY (court, day(event_date));
INSERT INTO lake.silver.cases VALUES (1,'NGM-DIST-01', DATE '2026-06-01', 2026);
```
(`<CATALOG_URI>` and `<WAREHOUSE>` are the literal values returned by `wrangler r2 bucket catalog enable` or the dashboard — see §2.)

---

## 2. R2 Data Catalog vs self-hosted Lakekeeper — the decision

### What R2 Data Catalog is
- A managed Apache Iceberg REST catalog built into R2 buckets. **Status: public beta, not GA** (no GA announcement through mid-2026). — high — https://developers.cloudflare.com/r2/data-catalog/
- Enable per-bucket: `npx wrangler r2 bucket catalog enable <BUCKET>` (or dashboard). Copy the returned **Catalog URI** + **Warehouse** — docs only show placeholders; do not construct the host string yourself. — high (mechanism) / low (any literal `catalog.cloudflarestorage.com/...` format — **VERIFY** from dashboard) — https://developers.cloudflare.com/r2/data-catalog/get-started/ ; https://developers.cloudflare.com/r2/data-catalog/manage-catalogs/
- **Auth = an R2 API token** (needs both R2 storage + Data Catalog permissions) passed as the Iceberg REST bearer `token`; the catalog vends SigV4 creds for data files. Permission groups: "Workers R2 Data Catalog Write" / "...Read" (coarse — Admin Read&Write or Admin Read only). — high — https://developers.cloudflare.com/r2/api/tokens/ ; https://developers.cloudflare.com/r2/data-catalog/get-started/
- **Read AND write** supported via the REST catalog. — high — https://developers.cloudflare.com/r2/data-catalog/get-started/
- **Built-in managed maintenance**: compaction (target 64–512 MB, default 128 MB, Parquet only, bin-packing; sort/z-order not offered) + snapshot expiration (`--older-than-days` default 30, `--retain-last` default 5). No dedicated orphan-file removal beyond snapshot-expiry's unreferenced-file cleanup. — high — https://developers.cloudflare.com/r2/data-catalog/table-maintenance/ ; https://developers.cloudflare.com/r2/data-catalog/manage-catalogs/
- **DuckDB is officially documented** by both Cloudflare and DuckDB; the OAuth-vs-bearer gotcha is resolved (R2 uses the `TOKEN` field directly, no OAuth client flow). — high — https://developers.cloudflare.com/r2/data-catalog/config-examples/duckdb/ ; https://duckdb.org/docs/current/core_extensions/iceberg/iceberg_rest_catalogs.html
- **Pricing: billing still WAIVED as of 2026-06-27.** A rate card is published but the 28-May-2026 changelog says verbatim "Billing is not yet enabled. We will provide at least 30 days notice before we start charging." Published (future) rates: catalog ops $9/M (1M free), compaction data $0.005/GB (10GB free), compaction objects $2/M (1M free), snapshot expiration free — on top of standard R2 storage ($0.015/GB-mo, egress free). — high — https://developers.cloudflare.com/changelog/rss/r2.xml ; https://developers.cloudflare.com/r2/data-catalog/platform/pricing/ ; https://developers.cloudflare.com/r2/pricing/
- **Limits/gaps:** no documented per-catalog table/namespace count limits; account-wide R2 REST API cap of 1,200 req / 5 min; **does not support R2 buckets in a non-default jurisdiction**. — high — https://developers.cloudflare.com/r2/platform/limits/ ; https://developers.cloudflare.com/r2/data-catalog/manage-catalogs/

### What Lakekeeper is
- Apache Iceberg REST catalog in Rust ("single all-in-one binary, no JVM/Python"), Apache-2.0, built on apache/iceberg-rust. Latest **v0.12.4 (2026-06-17)**; **still pre-1.0** (no GA/1.0 declared), but markets production-ready ops (horizontal scaling, read-only maintenance mode) + a paid LTS tier. — high — https://github.com/lakekeeper/lakekeeper ; https://github.com/lakekeeper/lakekeeper/releases
- **Native Cloudflare R2 storage support** via `credential-type: cloudflare-r2` (auto-normalizes `flavor=s3-compat`, `sts-enabled=true`, `assume-role-arn=None`; requires `endpoint`). Also supports S3/MinIO/Azure/GCS. — high — https://docs.lakekeeper.io/docs/nightly/storage/
- **Auth: bring-your-own OIDC** (never issues its own tokens) + optional Kubernetes TokenReview. **Authorization: OpenFGA** (default; also AllowAll/Cedar — Cedar is paid "Lakekeeper+"), with multi-tenant projects → warehouses and per-table/namespace grants. — high — https://docs.lakekeeper.io/docs/nightly/authentication/ ; https://docs.lakekeeper.io/docs/nightly/authorization-openfga/
- **Backing store: Postgres >= 15** (only backend). **Automated table maintenance/compaction is a PAID feature (Lakekeeper Plus)** — OSS core does not auto-compact. — high — https://github.com/lakekeeper/lakekeeper

### Side-by-side

| Dimension | R2 Data Catalog | Self-hosted Lakekeeper (OSS) |
|---|---|---|
| Auth | Coarse R2 API token (Admin R/W or R only) — no IdP | Any OIDC IdP (Zitadel via generic OIDC) + K8s TokenReview |
| Access control | Bucket-level token scope only | Fine-grained per-table/namespace via OpenFGA; multi-tenant projects→warehouses |
| DuckDB compat | Officially documented (TOKEN bearer) | Officially documented (OAuth2 client creds) |
| Managed compaction | Yes, built-in (opt-in, bin-pack) | No (OSS) — needs external engine or Lakekeeper Plus |
| Snapshot expiry | Yes, built-in (free) | Via external engine / PyIceberg client-side |
| Maturity | Public beta, no SLA | Pre-1.0 OSS, 49+ releases, production-ops messaging |
| Cost | Billing waived now; modest rate card; storage always billed | Your infra: catalog pods + Postgres≥15 + OpenFGA service |
| Ops burden | Zero | Run/patch/scale catalog + Postgres + OpenFGA |
| Lock-in | Couples catalog **and** storage to Cloudflare (data files stay portable) | Storage-agnostic; self-hosted; can still use R2 storage |

### Recommendation
**Self-host Lakekeeper, keep R2 as the storage backend.** Our Jawafdehi permission model (groups + predicates, read/write splits, multi-tenant-style access) needs fine-grained authz and IdP-based identity, which **R2 Data Catalog cannot provide** (coarse token scope, no IdP). Lakekeeper gives OIDC (Zitadel) + OpenFGA per-table grants and natively supports R2 storage, so we keep R2's cheap, egress-free object store without coupling our catalog to a beta service.

**Choose R2 Data Catalog instead only if** you decide fine-grained authz and IdP integration are not load-bearing AND you want to eliminate the Postgres+OpenFGA+catalog operational footprint AND you accept beta status + Cloudflare catalog lock-in. Its free managed compaction is the single biggest carrot — see §4 for how to cover that gap on self-hosted Lakekeeper.

---

## 3. Write path — DuckDB vs PyIceberg vs Spark

### Capability summary (all verified against official docs)
- **DuckDB (1.5.x):** CREATE/INSERT/UPDATE/DELETE/MERGE INTO + partitioned writes + schema evolution against an attached REST catalog. **No compaction, no partition evolution, no copy-on-write** (positional deletes only). — high — https://duckdb.org/docs/current/core_extensions/iceberg/writing ; https://duckdb.org/2026/05/29/new-iceberg-features.html
- **PyIceberg (0.11.1):** connects to REST catalog over S3-compatible storage (R2 documented). Writes: `append`, `overwrite` (+filtered/static), `dynamic_partition_overwrite`, row-level `delete`, `upsert` (identifier-field equality only — no `WHEN MATCHED` SQL), `create_table`, `add_files`. Maintenance: **`expire_snapshots` yes**; **compaction NO** ("Compaction is planned", open issue apache/iceberg-python#1092); **no orphan-file removal**, no `rewrite_manifests`, no `retain_last(N)` (age/ID expiry only). — high — https://py.iceberg.apache.org/api/ ; https://py.iceberg.apache.org/configuration/ ; https://github.com/apache/iceberg-python/issues/1092
- **Spark (iceberg-spark-runtime, Iceberg 1.11.0):** the reference engine — "most feature-rich." Full MERGE INTO (incl. `WHEN NOT MATCHED BY SOURCE`), partition evolution (`ADD/DROP PARTITION FIELD`), and the maintenance procedures **nothing else has**: `rewrite_data_files` (bin-pack/sort/zorder), `rewrite_manifests`, `expire_snapshots`, `remove_orphan_files`, `rewrite_position_delete_files`. — high — https://iceberg.apache.org/docs/latest/spark-writes/ ; https://iceberg.apache.org/docs/latest/spark-procedures/ ; https://iceberg.apache.org/docs/latest/spark-ddl/
- R2 Data Catalog documents **7 write engines**: DuckDB, PyIceberg, Snowflake, Spark (PySpark + Scala), StarRocks, Trino. — high — https://developers.cloudflare.com/r2/data-catalog/config-examples/

### Pressure-test of the DuckDB-only write plan
**Verdict: viable for the *write* path, but the compaction gap is the catch — and on self-hosted Lakekeeper OSS that gap is NOT auto-covered.**

- DuckDB 1.5.x has enough DML for silver transforms (dedup, upsert via MERGE, late corrections). It is officially documented writing to R2. So "can DuckDB write?" — yes. — high
- **The linchpin: neither DuckDB nor PyIceberg can compact.** A DuckDB-only writer accumulates small files + positional delete files with no in-engine remediation. On **R2 Data Catalog** this is covered by managed compaction (if enabled). On **self-hosted Lakekeeper OSS** there is no managed compaction — you MUST run compaction from an external engine (Trino/Flink/Spark) against the same REST catalog, or buy Lakekeeper Plus. — high
- DuckDB writes are merge-on-read only (no copy-on-write); heavy update/delete volume relies on downstream compaction to stay performant. — high
- DuckDB-Iceberg writes carry **no GA label** and evolve fast across point releases — pin and test a specific 1.5.x version. — high (no label) / medium (volatility)
- No partition evolution and no full conditional MERGE in DuckDB — if silver partition specs must change over time, that needs Spark. — high

**Recommendation:**
- **DuckDB-only or PyIceberg-only silver writer is fine IF** (a) a compaction mechanism is in place (R2 managed, or an external engine pointed at the Lakekeeper REST catalog), (b) you pin/test a DuckDB 1.5.x version, (c) update/delete volume is moderate.
- Prefer **PyIceberg** for a programmatic Python writer (same write coverage + native `expire_snapshots`); prefer **DuckDB** for SQL-centric transforms inside the query engine you already use.
- **Keep Spark (or Trino/Flink) available as the maintenance/partition-evolution escape hatch** — it is the only path for `rewrite_data_files`, `remove_orphan_files`, `rewrite_manifests`, partition evolution, and full MERGE semantics.
- **Do not run a DuckDB-only plan with no compaction strategy** — that is the failure mode.

---

## 4. Partitioning & maintenance without Spark

### Partitioning for our access patterns
- Transforms are a fixed set: identity, year, month, day, hour, bucket[N], truncate[W], void (epoch-anchored to 1970). — high — https://iceberg.apache.org/spec/#partition-transforms
- **court + date →** `PARTITIONED BY (court, day(event_date))` (Spark DDL: `days(event_date)`). `court` is identity; `day()` gives date pruning; Iceberg prunes on any partition field regardless of order, so both predicates push down. Idiomatic per the canonical docs example. — high — https://iceberg.apache.org/docs/latest/partitioning/ ; https://iceberg.apache.org/docs/latest/spark-ddl/
- **fiscal_year → no built-in transform.** Materialize a real `fiscal_year` column in ETL and `PARTITIONED BY (fiscal_year)` (identity). You own that column's correctness. — high (supported) / medium (that it's "recommended" — docs give no fiscal guidance) — https://iceberg.apache.org/spec/#partition-transforms
- **Partition evolution** is metadata-only ("does not eagerly rewrite files") — start with `(court, day(event_date))` and add `fiscal_year` later; old data keeps its spec via split planning. — high — https://iceberg.apache.org/docs/latest/evolution/

### Who runs maintenance without Spark
- **Compaction:** Not in DuckDB, not in PyIceberg (planned, issue #1092). Without Spark, run it from **Trino** (`ALTER TABLE ... EXECUTE optimize`), **Flink** (`RewriteDataFiles`), **Athena** (`OPTIMIZE ... REWRITE DATA USING BIN_PACK`), or **Dremio** (`OPTIMIZE TABLE`) — any of which can connect to the same REST catalog (`iceberg.catalog.type=rest`). On R2 Data Catalog, use its managed compaction instead. — high — https://trino.io/docs/current/connector/iceberg.html ; https://iceberg.apache.org/docs/latest/flink-maintenance/ ; https://developers.cloudflare.com/r2/data-catalog/table-maintenance/
- **Snapshot expiry:** **PyIceberg supports it natively** — `table.maintenance.expire_snapshots().older_than(...).commit()` (age or explicit ID; no `retain_last(N)`). Also R2 managed (free). — high — https://py.iceberg.apache.org/reference/pyiceberg/table/maintenance/
- **Orphan-file removal:** the real gap. **Neither PyIceberg nor R2 Data Catalog** does true orphan-file cleanup (R2's only removes files unreferenced by retained snapshots — narrower; misses files from aborted/failed commits). Without Spark, use **Trino `remove_orphan_files`**, Athena `VACUUM`, Flink `DeleteOrphanFiles`, or Dremio `VACUUM TABLE`. — high / medium (R2 orphan scope) — https://py.iceberg.apache.org/api/ ; https://trino.io/docs/current/connector/iceberg.html

### Maintenance recommendation
For self-hosted Lakekeeper + R2 storage with a DuckDB/PyIceberg writer: use **PyIceberg `expire_snapshots`** for snapshot retention, and stand up a lightweight **Trino** (or Flink) job against the Lakekeeper REST catalog to run `optimize` (compaction) + `remove_orphan_files` on a schedule. This avoids a full Spark cluster while covering the two gaps DuckDB/PyIceberg leave. (For comparison: AWS S3 Tables and R2 Data Catalog both do compaction+expiry automatically — that convenience is exactly what OSS Lakekeeper trades away for authz/control.) — https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-tables-maintenance.html

---

## 5. Kubernetes: Lakekeeper Helm + Postgres + Zitadel OIDC

### Helm
```bash
helm repo add lakekeeper https://lakekeeper.github.io/lakekeeper-charts/
helm install my-lakekeeper lakekeeper/lakekeeper
```
- Chart repo `lakekeeper/lakekeeper-charts`, path `charts/lakekeeper`, published as a GitHub Pages Helm repo (also on ArtifactHub). Canonical `Chart.yaml` at HEAD: `version: 0.11.0`, `appVersion: 0.12.2` (README's version *table* is stale — trust Chart.yaml). No OCI distribution documented. — high / medium (OCI absence) — https://github.com/lakekeeper/lakekeeper-charts ; https://artifacthub.io/packages/helm/lakekeeper/lakekeeper
- Chart is **nested** (not flat): main service under `catalog.*`; bundles `openfga` + a `postgres` subchart (aliased `postgresql`) as dependencies. Key values: `catalog.image.{repository,tag,pullPolicy}`, `catalog.replicas` (1; stateless — **no top-level `replicaCount`**), `catalog.service.{type,externalPort:8181}`, `catalog.ingress.{enabled,host,ingressClassName,tls}`. — high — chart `values.yaml` HEAD
- **Production caveats:** bundled Postgres subchart is "not production ready" — set `postgresql.enabled: false` + `externalDatabase.*`. A K8s **Operator exists but is in development (not GA)**. — high — https://docs.lakekeeper.io/getting-started/

### Postgres backend
- Postgres **>= 15** is the only catalog backend (and default secret store). — high — https://github.com/lakekeeper/lakekeeper
- Env vars (prefix `LAKEKEEPER__PG_`): `PG_DATABASE_URL_WRITE` (required), `PG_DATABASE_URL_READ` (defaults to write), `PG_ENCRYPTION_KEY` (**must be a long random value in prod**), plus component forms (`PG_HOST_R/W`, `PG_PORT`, `PG_USER`, `PG_PASSWORD`, `PG_DATABASE`), SSL (`PG_SSL_MODE`, `PG_SSL_ROOT_CERT`), and pool tuning. — high — https://docs.lakekeeper.io/docs/nightly/configuration/
- **Migrations are explicit**: run CLI `lakekeeper migrate` before each upgrade and before `lakekeeper serve` (server refuses to start otherwise; dev-only `LAKEKEEPER__DEBUG__MIGRATE_BEFORE_SERVE`). Requires PG extensions `uuid-ossp`, `pgcrypto`, `pg_trgm`, `btree_gin`, `btree_gist` (auto-created if the role has CREATE). — high — https://docs.lakekeeper.io/docs/nightly/concepts/
- **Bootstrap** (one-time, distinct from migrate): UI or `POST /management/v1/bootstrap {"accept-terms-of-use": true}` — grants admin, sets Server ID, creates default project. — high — https://docs.lakekeeper.io/docs/nightly/bootstrap/

### OIDC + OpenFGA
- OIDC AuthN is **fully supported (not beta)**: JWTs validated locally against the provider `jwks_uri` (opaque tokens unsupported); provider must expose `.well-known/openid-configuration`. Env: `LAKEKEEPER__OPENID_PROVIDER_URI`, `OPENID_AUDIENCE` (token `aud` must match — recommended always), `OPENID_ADDITIONAL_ISSUERS`, `OPENID_SCOPE`, `OPENID_SUBJECT_CLAIM`, `OPENID_ROLES_CLAIM`; multi-IdP via `OPENID_PROVIDERS__<ID>__*`; UI via `UI__OPENID_CLIENT_ID`. User IDs become `oidc~<id>`. — high — https://docs.lakekeeper.io/docs/nightly/authentication/ ; https://docs.lakekeeper.io/docs/nightly/configuration/
- K8s auth can run alongside OIDC: `LAKEKEEPER__ENABLE_KUBERNETES_AUTHENTICATION`, `KUBERNETES_AUTHENTICATION_AUDIENCE` (identities `k8s~<ns>~<sa>`). — high
- Authorization via **OpenFGA** (default authorizer; `LAKEKEEPER__AUTHZ_BACKEND ∈ {openfga, allowall, cedar}`, default `allowall` = no authz). OpenFGA env: `OPENFGA__ENDPOINT` (gRPC), `__STORE_NAME` (default `lakekeeper`), `__API_KEY` or client-creds, `__AUTHORIZATION_MODEL_PREFIX`. Requires a **separately deployed OpenFGA** ("v1.11+ required, tested against v1.14"). `LAKEKEEPER__INSTANCE_ADMINS` sets instance admins. Cedar backend is paid (Lakekeeper+). — high / medium (exact OpenFGA reconcile CLI string) — https://docs.lakekeeper.io/docs/nightly/authorization-openfga/

### Zitadel specifically
- **Zitadel is NOT an officially documented Lakekeeper provider** (first-class guides exist only for Keycloak, Entra-ID, Google; Okta in multi-provider examples; the `examples/` folder uses Keycloak). It works via Lakekeeper's **generic OIDC** support. — high — https://docs.lakekeeper.io/docs/nightly/authentication/ ; https://github.com/lakekeeper/lakekeeper/tree/main/examples
- Concept mapping:
  - **Issuer URL** (Zitadel instance domain, discovery at `${DOMAIN}/.well-known/openid-configuration`) → `LAKEKEEPER__OPENID_PROVIDER_URI`. — high (path) / medium (exact `*.zitadel.cloud` subdomain — **VERIFY** in Console) — https://zitadel.com/docs/apis/openidoauth/endpoints
  - **Audience:** Zitadel populates `aud` via requested scope `urn:zitadel:iam:org:project:id:{projectId}:aud` → set `LAKEKEEPER__OPENID_AUDIENCE` to the Zitadel **project ID**. — medium-high — https://zitadel.com/docs/guides/integrate/retrieve-user-roles
  - **Project** = app/role container; **Application** = the OIDC client; **Service User (machine user)** = non-human account (JWT-profile/client-credentials), registered in Lakekeeper via `POST /management/v1/user {"subject":"oidc~<id>"}`.
  - **Roles** asserted via Zitadel "Assert Roles on Authentication" / scope `urn:zitadel:iam:org:project:{projectId}:roles` → map to `OPENID_ROLES_CLAIM`.
- Community (unofficial) Terraform references: `datamindedbe/eu-data-platform` (Zitadel project + OIDC app for Lakekeeper, redirect `https://lakekeeper.<domain>/oauth2/callback`, auth-code grant) and `datamindedbe/demo-upcloud-data-platform`. — high (they exist; not official) — https://github.com/datamindedbe/eu-data-platform

### R2 storage config in Lakekeeper warehouses
- Storage profile `type: s3`; for R2 use `credential-type: cloudflare-r2` with `account-id`, `access-key-id`, `secret-access-key`, `token` (token fetches downscoped temp creds for credential vending). Lakekeeper auto-normalizes `flavor → s3-compat`, `sts-enabled → true`, `assume-role-arn → None`. — high — https://docs.lakekeeper.io/docs/nightly/storage/
- A custom `endpoint` is **required** (docs example `https://<account-id>.eu.r2.cloudflarestorage.com`; Cloudflare base `https://<ACCOUNT_ID>.r2.cloudflarestorage.com`). The R2 token must have **"Admin Read & Write"** (the temp-credentials endpoint accepts no lesser token). Region: use a Data Location Hint (e.g. `weur`); field accepts any string for s3-compat. — high / medium (region hint) — https://docs.lakekeeper.io/docs/nightly/storage/ ; https://developers.cloudflare.com/r2/api/s3/api/
- Both remote signing and vended credentials are supported (vended preferred when STS available), selected via `X-Iceberg-Access-Delegation` header. R2 vended creds route through R2's own `/accounts/{account_id}/r2/temp-access-credentials` endpoint. — high
- **VERIFY (low/medium):** R2 `path-style-access` is not explicitly addressed; default `false` (virtual-host) matches R2's `<account-id>.r2.cloudflarestorage.com` style.

### Operational order for a secured K8s deploy
1. External Postgres ≥15 (`postgresql.enabled: false`, set `PG_DATABASE_URL_WRITE`, strong `PG_ENCRYPTION_KEY`).
2. `lakekeeper migrate`.
3. Deploy OpenFGA service, set `AUTHZ_BACKEND=openfga` + `OPENFGA__*`.
4. Configure OIDC env (`OPENID_PROVIDER_URI` = Zitadel issuer, `OPENID_AUDIENCE` = Zitadel project ID).
5. One-time bootstrap.
6. Create R2-backed warehouse with `credential-type: cloudflare-r2`.

---

## Open items to confirm before relying on them
- **DuckDB R2 storage secret form** (`TYPE s3 ... ENDPOINT ... URL_STYLE 'path'`): not part of the REST-catalog attach (catalog vends creds). Only needed for catalog-less reads or non-vended Lakekeeper credential modes — confirm exact spelling on the httpfs S3 secret docs.
- **R2 Data Catalog literal Catalog URI/Warehouse format**: docs show placeholders only — copy runtime values, don't construct.
- **R2 Data Catalog pricing**: billing waived now; rate card published; expect ≥30 days' notice before charges — re-check before GA.
- **Lakekeeper Zitadel**: generic-OIDC only (no official guide); confirm Zitadel issuer subdomain + the `...:aud` scope → `OPENID_AUDIENCE` mapping in the Zitadel Console.
- **Lakekeeper R2 `path-style-access`** and the exact **OpenFGA reconcile CLI/version**: verify against live docs if load-bearing.
- **DuckDB write-feature volatility**: pin a specific 1.5.x and run `UPDATE EXTENSIONS;`; test partitioned UPDATE/DELETE against R2 with that exact version (Cloudflare's "no DELETE on partitioned tables" note is a stale 1.4.0 artifact).

## Sources (primary)
- DuckDB Iceberg: https://duckdb.org/docs/current/core_extensions/iceberg/overview.html · /iceberg_rest_catalogs.html · /writing · /amazon_s3_tables · blogs 2025/09/16, 2026/05/29, 2026/06/17
- Cloudflare R2 Data Catalog: https://developers.cloudflare.com/r2/data-catalog/ · /get-started/ · /manage-catalogs/ · /table-maintenance/ · /config-examples/{duckdb,pyiceberg} · /platform/pricing/ · /api/tokens/ · changelog/rss/r2.xml
- Lakekeeper: https://github.com/lakekeeper/lakekeeper · /releases · /lakekeeper-charts · https://docs.lakekeeper.io/docs/nightly/{configuration,authentication,authorization-openfga,storage,concepts,bootstrap}
- Apache Iceberg: https://iceberg.apache.org/spec/#partition-transforms · /docs/latest/{partitioning,evolution,spark-writes,spark-ddl,spark-procedures} · /releases
- PyIceberg: https://py.iceberg.apache.org/{api,configuration} · /reference/pyiceberg/table/maintenance/ · https://github.com/apache/iceberg-python/issues/1092
- Zitadel: https://zitadel.com/docs/apis/openidoauth/endpoints · /guides/integrate/retrieve-user-roles
- Comparison: https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-tables-maintenance.html
