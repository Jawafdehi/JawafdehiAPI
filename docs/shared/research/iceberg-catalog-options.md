# Iceberg REST Catalog Implementation Options — Decision Research

**Context:** Self-hosted data lakehouse on **Cloudflare R2** (S3-compatible object storage) + **DuckDB** as a query engine, deployed in **Kubernetes**, with **OIDC** auth and a **Postgres-per-service** convention already in the stack.

**Goal:** Both-sides comparison of self-hostable Apache Iceberg REST catalog implementations so a human can pick in bulk review.

**Date of research:** 2026-06-27. Star counts, versions, and chart versions are point-in-time and drift; re-verify at decision time.

---

## TL;DR Recommendation

**Primary pick: Lakekeeper.** Best single-fit for this exact stack. It is the only candidate that **explicitly documents Cloudflare R2 support**, requires **only Postgres** (matches the Postgres-per-service convention), is **OIDC-native** (issues no credentials itself, validates external JWTs), ships an **official Helm chart**, has a **tiny Rust single-binary footprint** (no JVM), and is **named in DuckDB's own docs** as a supported REST catalog with an official DuckDB tutorial.

**Backup / "safe institutional" pick: Apache Polaris.** Now a graduated Apache **Top-Level Project** (not incubating — see correction below), Postgres-backed via Relational JDBC, OIDC via Quarkus, official Helm chart, and **explicitly tested by DuckDB's CI**. The trade-offs vs Lakekeeper: heavier JVM/Quarkus footprint, and **R2 is only inferred via generic S3-compatibility** (endpoint override + path-style + `stsUnavailable`), not a documented R2 recipe.

**The binding constraint is satisfied:** DuckDB's iceberg extension supports **both read and write** against an Iceberg REST catalog as of **DuckDB 1.4.0 (Sept 2025)**, matured through 1.5.x (June 2026) — with caveats (merge-on-read only; S3/S3 Tables/GCS storage only). See the [DuckDB section](#duckdb-iceberg-rest-catalog-support-the-binding-constraint).

---

## DuckDB Iceberg REST Catalog Support (the binding constraint)

This is the gate that decides everything else, so it leads.

| Capability | Status | Since | Notes |
|---|---|---|---|
| Attach to Iceberg REST catalog | **GA** | DuckDB 1.4.0 (also preview in 1.2.1, Mar 2025) | `ATTACH 'wh' AS cat (TYPE iceberg, SECRET s, ENDPOINT '...')` |
| **READ** via REST catalog | **GA** | 1.2.1 preview → stable | `SELECT`, time-travel `AT`, `iceberg_metadata`, `iceberg_snapshots` |
| **WRITE** via REST catalog | **GA-grade (supported)** | **DuckDB 1.4.0 (2025-09-16)** | CREATE TABLE/CTAS, INSERT, UPDATE, DELETE, MERGE INTO, ALTER TABLE, PARTITIONED BY |
| OAuth2 client-credentials auth | Supported | — | `CREATE SECRET (TYPE iceberg, CLIENT_ID, CLIENT_SECRET, OAUTH2_SERVER_URI, OAUTH2_SCOPE)`; also direct bearer `TOKEN` |
| Vended credentials | Default | — | `ACCESS_DELEGATION_MODE = vended_credentials` (or `none`) |
| Custom S3 endpoint (R2) | Supported | — | via httpfs/S3 secret + `ENDPOINT`; SigV4 also selectable |

**Key write caveats (current stable docs):**
- **Writing requires an attached catalog** — the path-based `iceberg_scan` is read-only.
- **Merge-on-read only**: UPDATE/DELETE emit positional deletes; **copy-on-write is not supported**. If a table's `write.update.mode`/`write.delete.mode` isn't `merge-on-read`, the op fails.
- **Storage backends limited to S3, S3 Tables, and GCS** for REST catalogs — R2 qualifies as S3-compatible.
- File-size properties not honored for partitioned tables; schema evolution via ALTER TABLE *is* supported; partition transforms identity/year/month/day/hour/bucket(n)/truncate(n).
- **Documentation vs README discrepancy:** the official DuckDB docs present write as a normal supported feature with no experimental disclaimer, but the `duckdb/duckdb-iceberg` GitHub README still carries a stale "experimental state" banner that appears not to have been updated post-1.4.0. Treat the docs as authoritative; flag the README banner for risk-sensitive sign-off.

**Catalogs DuckDB documents/tests:** Cloudflare **R2 Data Catalog**, Apache **Polaris**, **Lakekeeper**, Google BigLake, Unity Catalog (limited), plus S3 Tables and Glue via `ENDPOINT_TYPE`. CI test fixtures: iceberg-rest-fixture, **Nessie**, **Lakekeeper**, **Polaris**. (Gravitino is acknowledged in Gravitino's own 1.2.0 notes but not documented by DuckDB.)

Sources:
- https://duckdb.org/docs/current/core_extensions/iceberg/iceberg_rest_catalogs.html
- https://duckdb.org/docs/current/core_extensions/iceberg/writing.html
- https://duckdb.org/docs/current/core_extensions/iceberg/overview.html
- https://duckdb.org/2025/09/16/announcing-duckdb-140.html
- https://duckdb.org/2025/03/14/preview-amazon-s3-tables.html
- https://github.com/duckdb/duckdb-iceberg

---

## Comparison Table

| Dimension | **Lakekeeper** | **Apache Polaris** | **Project Nessie** | **iceberg-rest-fixture** | **Apache Gravitino** |
|---|---|---|---|---|---|
| Maturity / governance | Independent OSS, open-core; **pre-1.0 (0.x)** | **Apache TLP** (graduated 2026-02-15; *not* incubating) | Dremio-led, **no foundation**; **stated intent to merge into Polaris & retire** (not yet executed) | **Reference/test fixture** inside `apache/iceberg`; **not production-positioned** | **Apache TLP** (graduated 2025-05-21); broad "metadata lake" |
| License | Apache 2.0 | Apache 2.0 | Apache 2.0 | Apache 2.0 | Apache 2.0 |
| Language / footprint | **Rust, single binary, no JVM** | Java / **Quarkus** (JVM); image ~379 MB | Java / Quarkus; image ~555 MB; min 4 CPU/4 GB | Java / Jetty; image ~333 MB | Java (multi-component: server+UI+CLI+connectors); ~heavy |
| Current release (2026-06-27) | v0.12.4 (2026-06-17) | **1.5.0 (2026-05-18)** | 0.108.1 (2026-06-24) | tracks apache/iceberg | 1.2.1 (2026-05-12) |
| R2 / S3-compatible | **R2 EXPLICITLY supported** (`flavor=s3-compat`, endpoint, remote-signing, vended creds) | Generic S3 (endpoint override, path-style, `stsUnavailable`); **R2 inferred, not named** | Generic S3 + request-signing; **R2 inferred, not named** | Generic S3 via `CATALOG_S3_ENDPOINT`; works with R2 | Generic S3-compatible (`s3-endpoint`, `s3-path-style-access`); **R2 inferred, not named** |
| DuckDB compatibility | **Yes — named in DuckDB docs + official Lakekeeper DuckDB tutorial** | **Yes — DuckDB docs example + DuckDB CI** | Yes via generic REST (not named in DuckDB docs); **open branch-write 404 bug #11828** | Yes (DuckDB CI fixture); use `AUTHORIZATION_TYPE none` | Plausible (spec-compliant); **no documented DuckDB recipe** |
| Auth / OIDC | **OIDC-native**; validates external JWTs (Keycloak, Entra, Google, K8s); pluggable authz (OpenFGA/Cedar) | OAuth2 broker + **external OIDC** (Quarkus OIDC, Keycloak); internal/external/mixed modes | OIDC via Quarkus (Keycloak); bearer tokens; **REST clients can't use OAuth code/device flows** | **None** (no auth filter at all) | OAuth2 + OIDC (JWKS), Keycloak/Azure AD; simple/Basic/Kerberos too |
| Kubernetes | **Official Helm chart**; operator WIP | **Official Helm chart** (Apache Helm repo) | Official Helm chart (`charts.projectnessie.org`); no operator | **No Helm chart** (Docker image only) | Official Helm charts (OCI registry); incl. dedicated iceberg-rest-server chart |
| Backing store | **Postgres ONLY** (≥15); secrets in PG or Vault | Postgres (Relational JDBC, recommended); H2/CockroachDB/Mongo opt | Many: RocksDB/JDBC(Postgres)/Mongo/Bigtable/Dynamo/Cassandra; **prefers distributed KV for high commit concurrency** | In-memory/file SQLite default; Postgres via JDBC | Own store: JDBC (H2/MySQL/**Postgres ≥0.7.0**); IRC backend memory/Hive/JDBC |
| Momentum (stars) | ~1,369 | ~1,982 | ~1,471 | n/a (part of iceberg) | ~3,000 |
| Backers | Lakekeeper team (corporate steward likely "Vakamo" — *inferred, unconfirmed*) | Originated Snowflake → ASF | Dremio | Apache Iceberg project | Datastrato → ASF (adopters: Uber, Pinterest) |

---

## Per-Implementation Detail

### 1. Lakekeeper (formerly `iceberg-catalog`)
- **Rename confirmed** — `hansetag/iceberg-catalog` and `lakekeeper/lakekeeper` resolve to the same GitHub repo id. Pre-1.0 (0.x), so config/feature names are version-dependent.
- **R2:** Direct doc quote: *"Lakekeeper supports Cloudflare R2 storage with all S3 compatible clients, including vended credentials."* Set `sts-enabled=true`, `flavor=s3-compat`, `endpoint`; remote-signing + STS recommended for client compatibility. This is the **only candidate with a first-party R2 statement.**
- **Auth:** Pure external IdP — Lakekeeper issues no credentials; tokens must be **JWTs** (opaque unsupported). Keycloak, Microsoft Entra, Google (limited), Kubernetes TokenReview documented. Authorization pluggable: AllowAll / OpenFGA (separate service) / Cedar (built-in) / custom.
- **Store:** *"Currently Lakekeeper supports only Postgres as a persistence store"* — Postgres ≥15. Secret backend `postgres` (default) or Vault KV v2.
- **K8s:** Official Helm chart (`lakekeeper/lakekeeper-charts`); image `quay.io/lakekeeper/catalog`; operator in development (not yet recommended).
- **DuckDB:** Official Lakekeeper DuckDB + DuckDB-WASM tutorial; named in DuckDB's REST-catalog docs. Tutorial demonstrates read; write depends on DuckDB extension version.
- Sources: https://docs.lakekeeper.io/docs/nightly/storage/ · https://docs.lakekeeper.io/docs/latest/authentication/ · https://docs.lakekeeper.io/docs/nightly/configuration/ · https://github.com/lakekeeper/lakekeeper · https://github.com/lakekeeper/lakekeeper-charts · https://docs.lakekeeper.io/docs/latest/engines/
- **Caveat:** corporate steward identity ("Vakamo") is inferred from a Cargo.toml dependency, **not officially stated**; project is 0.x.

### 2. Apache Polaris
- **Framing correction:** **Not incubating anymore** — graduated to Apache **TLP on 2026-02-15** (incubating 2024-08-09 → 2026-02-15). `-incubating` suffix dropped after 1.3.0.
- **R2:** Generic S3-compatibility added in 1.1.0 — `endpoint`/`endpointInternal`/`stsEndpoint`, `pathStyleAccess`. MinIO/Ceph/Ozone explicitly supported and tested. **R2 not named/tested** (only closed feature-request issue #60); practical path = `storageType: S3` + R2 endpoint + `pathStyleAccess: true` + `stsUnavailable: true` (R2 has no STS, so no vended credentials).
- **Auth:** OAuth2 token broker (bearer) by default; **external OIDC** via Quarkus OIDC added in 1.1.0 (`internal`/`external`/`mixed`; Keycloak). Gotcha: in `external` mode the internal `/v1/oauth/tokens` returns HTTP 501, so DuckDB must use a pre-issued bearer `TOKEN`, not CLIENT_ID/SECRET.
- **Store:** Postgres via `relational-jdbc` (recommended for prod); EclipseLink removed in 1.3.0 — must bootstrap with admin tool before first use.
- **K8s:** Official Helm chart in Apache Helm repo; images `apache/polaris` + `apache/polaris-admin-tool`.
- **DuckDB:** Explicitly supported with a dedicated ATTACH example; in duckdb-iceberg CI. Open issues: multi-tenant `Polaris-Realm` header ignored (#978), OAuth2 commit hang (#955).
- Sources: https://incubator.apache.org/projects/polaris.html · https://github.com/apache/polaris/releases/tag/apache-polaris-1.5.0 · https://github.com/apache/polaris/blob/main/CHANGELOG.md · https://polaris.apache.org/releases/1.1.0/external-idp/ · https://polaris.apache.org/in-dev/unreleased/metastores/ · https://polaris.apache.org/releases/latest/helm-chart/ · https://duckdb.org/docs/current/core_extensions/iceberg/overview.html

### 3. Project Nessie
- **Strategic risk:** Dremio announced (Oct 2024) intent to back Polaris and **merge Nessie into Polaris / retire Nessie** — but Nessie keeps shipping (0.108.1, Jun 2026). Single-vendor (Dremio), no foundation.
- **Differentiator:** Git-like branching/tagging/merging over Iceberg tables, atomic multi-table commits — unique among these options.
- **Iceberg REST:** Exposes an Iceberg REST endpoint (`/iceberg`) alongside its native API since 0.90.2 — but **still officially labeled "experimental,"** never GA'd.
- **R2:** Generic S3 + request-signing (R2 has no STS); R2 not officially tested.
- **DuckDB:** Works for reads via generic REST; **open bug #11828** — branch-targeted INSERT returns commit 404 (DuckDB 1.4.3 vs Nessie 0.106.1) as of Jan 2026. Earlier `lastColumnId` create bug fixed in 0.105.3. REST clients can't use Nessie's OAuth code/device flows (bearer/client-secret only).
- **Store:** JDBC/Postgres is a *production*-marked version store, but docs warn relational DBs bottleneck on concurrent same-branch commits — Nessie "works best with distributed KV" (Bigtable/DynamoDB).
- Sources: https://projectnessie.org/guides/iceberg-rest/ · https://github.com/projectnessie/nessie/issues/11828 · https://projectnessie.org/nessie-latest/configuration/ · https://siliconangle.com/2024/10/29/dremio-throws-support-polaris-data-catalog-expands-deployment-options-iceberg-lakehouse/ · https://github.com/projectnessie/nessie/releases/tag/nessie-0.108.1

### 4. iceberg-rest-fixture (reference image)
- **Verified: reference/testing fixture, NOT production.** Lives in `open-api/src/testFixtures/.../RESTCatalogServer.java` inside `apache/iceberg`; a plain Jetty server used for the REST Compatibility Kit.
- **No authentication at all** — no auth filter, no token validation. (Clients may *send* a token; nothing enforces it.) DuckDB connects with `AUTHORIZATION_TYPE none`.
- **Store:** in-memory/file SQLite via JdbcCatalog by default; Postgres possible via `CATALOG_URI=jdbc:postgresql://...`.
- **R2:** Works via `CATALOG_S3_ENDPOINT` (S3FileIO override).
- **No Helm chart** — Docker image only. The third-party `tabulario/iceberg-rest` was superseded by `apache/iceberg-rest-fixture` ~Dec 2024 (PR #11673).
- **Verdict:** Excellent for local dev / CI / smoke-testing the DuckDB↔catalog path. **Disqualified for production** (no auth, fixture status).
- Sources: https://github.com/apache/iceberg/blob/main/docker/iceberg-rest-fixture/README.md · https://github.com/apache/iceberg/blob/main/open-api/src/testFixtures/java/org/apache/iceberg/rest/RESTCatalogServer.java · https://hub.docker.com/r/apache/iceberg-rest-fixture · https://github.com/apache/iceberg/pull/11673

### 5. Apache Gravitino
- **Scope mismatch:** Heavier, broader "federated metadata lake" (Hive/JDBC/Iceberg/Paimon/Hudi/Kafka/Model/Fileset catalogs + Spark/Trino/Flink connectors + UI + CLI + SDKs). The Iceberg REST service is one component (port 9001, `/iceberg/`). Apache TLP since 2025-05-21; adopters incl. Uber, Pinterest.
- **R2:** Generic S3-compatible (`s3-endpoint`, `s3-path-style-access`); R2 not documented (inferred).
- **Auth:** OAuth2 + OIDC (JWKS), Keycloak/Azure AD; IRC client auth via Basic/OAuth2.
- **Store:** Own persistence is JDBC — Postgres supported (≥0.7.0). IRC backends: memory/Hive/JDBC.
- **K8s:** Official Helm charts via OCI registry, including a dedicated `gravitino-iceberg-rest-server` chart.
- **DuckDB:** Spec-compliant and acknowledged in Gravitino 1.2.0 notes, but **no documented end-to-end DuckDB recipe**; assemble-it-yourself with version-sensitive bearer-token behavior.
- **Verdict:** Compelling if you need multi-source federated metadata governance beyond Iceberg; **over-scoped** for a pure R2+DuckDB Iceberg catalog.
- Sources: https://github.com/apache/gravitino/blob/main/docs/iceberg-rest-service.md · https://incubator.apache.org/projects/ · https://gravitino.apache.org/blog/gravitino-1-2-0-release-notes/ · https://github.com/apache/gravitino/tree/main/dev/charts · https://raw.githubusercontent.com/apache/gravitino/main/docs/how-to-use-relational-backend-storage.md

---

## Recommendation & Trade-offs

**Best pair for {R2 + DuckDB + Kubernetes + OIDC + Postgres-per-service}: Lakekeeper (production) + iceberg-rest-fixture (local dev/CI).**

**Why Lakekeeper wins on this exact stack:**
- Only candidate with a **first-party documented R2 recipe** (s3-compat flavor, vended creds) — removes the single biggest uncertainty.
- **Postgres-only** persistence maps cleanly onto Postgres-per-service — no extra datastore (vs Nessie's KV preference, Gravitino's extra components).
- **OIDC-native, validates external JWTs** — fits an existing IdP with zero credential issuance by the catalog.
- **Rust single binary**, smallest footprint — cheapest to run many small services in K8s.
- **Named in DuckDB docs + official DuckDB tutorial**; in DuckDB CI.
- **Trade-offs / risks:** pre-1.0 (0.x) API churn; corporate steward identity unconfirmed (open-core "Lakekeeper Plus" tier exists); fine-grained authz (OpenFGA) adds a service if you want it; no published resource-sizing guidance; operator still WIP.

**If you prefer maximum institutional/governance safety, swap primary to Apache Polaris:**
- Apache TLP (vendor-neutral foundation), Postgres via Relational JDBC, OIDC via Quarkus, official Helm chart, DuckDB CI coverage.
- **Trade-offs:** heavier JVM/Quarkus footprint; **R2 only inferred** via generic S3 + `stsUnavailable: true` (you lose vended credentials — DuckDB authenticates to R2 directly); the `external` OIDC mode forces DuckDB to use pre-issued bearer tokens (501 on internal token endpoint).

**Why not the others (for this stack):**
- **Nessie** — Iceberg REST still "experimental," Dremio's stated retire-into-Polaris direction, open DuckDB branch-write 404 bug, and a relational-store concurrency caveat. Keep on the radar only if Git-like data branching is a hard requirement.
- **iceberg-rest-fixture** — no auth, fixture status; ideal as the **dev/CI mirror** of whichever production catalog you pick, not production itself.
- **Gravitino** — over-scoped; choose only if you need federated multi-source metadata governance beyond Iceberg.

**Suggested validation before bulk sign-off:** stand up Lakekeeper on R2 + Postgres in a K8s namespace, wire your OIDC IdP, and run a DuckDB 1.4.0+ session through CREATE TABLE / INSERT / MERGE (merge-on-read) to confirm the write path end-to-end, since DuckDB write is the binding constraint and R2 is S3-only-storage-supported.

---

## All Cited Sources

**DuckDB:** https://duckdb.org/docs/current/core_extensions/iceberg/iceberg_rest_catalogs.html · https://duckdb.org/docs/current/core_extensions/iceberg/writing.html · https://duckdb.org/docs/current/core_extensions/iceberg/overview.html · https://duckdb.org/2025/09/16/announcing-duckdb-140.html · https://duckdb.org/2025/03/14/preview-amazon-s3-tables.html · https://github.com/duckdb/duckdb-iceberg

**Lakekeeper:** https://github.com/lakekeeper/lakekeeper · https://docs.lakekeeper.io/docs/nightly/storage/ · https://docs.lakekeeper.io/docs/latest/authentication/ · https://docs.lakekeeper.io/docs/latest/authorization/ · https://docs.lakekeeper.io/docs/nightly/configuration/ · https://github.com/lakekeeper/lakekeeper-charts · https://docs.lakekeeper.io/docs/latest/engines/

**Polaris:** https://incubator.apache.org/projects/polaris.html · https://github.com/apache/polaris · https://github.com/apache/polaris/blob/main/CHANGELOG.md · https://github.com/apache/polaris/releases/tag/apache-polaris-1.5.0 · https://polaris.apache.org/releases/1.1.0/external-idp/ · https://polaris.apache.org/in-dev/unreleased/metastores/ · https://polaris.apache.org/releases/latest/helm-chart/ · https://www.snowflake.com/en/blog/introducing-polaris-catalog/ · https://github.com/apache/polaris/issues/60

**Nessie:** https://projectnessie.org/guides/iceberg-rest/ · https://projectnessie.org/nessie-latest/configuration/ · https://projectnessie.org/guides/kubernetes/ · https://github.com/projectnessie/nessie/issues/11828 · https://github.com/projectnessie/nessie/blob/main/CHANGELOG.md · https://github.com/projectnessie/nessie/releases/tag/nessie-0.108.1 · https://siliconangle.com/2024/10/29/dremio-throws-support-polaris-data-catalog-expands-deployment-options-iceberg-lakehouse/

**iceberg-rest-fixture:** https://github.com/apache/iceberg/blob/main/docker/iceberg-rest-fixture/README.md · https://github.com/apache/iceberg/blob/main/open-api/src/testFixtures/java/org/apache/iceberg/rest/RESTCatalogServer.java · https://hub.docker.com/r/apache/iceberg-rest-fixture · https://hub.docker.com/r/tabulario/iceberg-rest · https://github.com/apache/iceberg/pull/11673

**Gravitino:** https://github.com/apache/gravitino/blob/main/docs/iceberg-rest-service.md · https://incubator.apache.org/projects/ · https://gravitino.apache.org/blog/gravitino-1-2-0-release-notes/ · https://github.com/apache/gravitino/tree/main/dev/charts · https://raw.githubusercontent.com/apache/gravitino/main/docs/how-to-use-relational-backend-storage.md · https://raw.githubusercontent.com/apache/gravitino/main/docs/security/how-to-authenticate.md
