# Cost Audit — Free-OSS-Only Constraint

**Purpose:** Audit every technology choice across the research docs against the **hard constraint: minimize cost — NO paid licenses, NO pro/enterprise tiers, NO paid managed/SaaS software. Everything self-hosted on free & open-source software (OSS).** Cloud object storage (Cloudflare R2) is acceptable because it is pay-per-use (PAYG) and cheap; everything requiring a paid license or subscription must be avoided.

**Date:** 2026-06-28. Licenses and pricing change — re-verify at decision time. Verified live where flagged (see "Verification notes" + cited URLs).

**Legend:**
- **FREE-OSS** — OSI/Apache/MIT/BSD/AGPL open-source, self-hostable at $0 license cost. OK.
- **PAYG** — pay-per-use cloud (no license/subscription); acceptable, cost noted.
- **PAID** — requires a paid license / pro / enterprise / managed-SaaS tier → **must avoid**; free alternative proposed.
- **WATCH** — free today but a licensing trap or recently-changed terms; pin/monitor.

---

## 1. Component-by-component table

| Component | Chosen in docs | License | Cost model | Verdict | Free alternative / note if PAID |
|---|---|---|---|---|---|
| **Iceberg catalog** | Lakekeeper (OSS core) | Apache-2.0 | Self-host, $0 | **FREE-OSS** | Core covers R2 storage + Postgres + OIDC + OpenFGA authz, all free. See §2 for the paid-feature traps. |
| ↳ Lakekeeper **Plus** | runner-up for compaction/Cedar | Commercial | Paid tier | **PAID — avoid** | Do NOT buy Plus. Replace its two paid features (auto-compaction, Cedar authz) with OSS: external Trino/Flink/Spark for compaction; OpenFGA for authz. |
| ↳ Apache Polaris | catalog runner-up | Apache-2.0 (Apache TLP) | Self-host, $0 | **FREE-OSS** | Fully free, no paid tier; viable free fallback catalog. |
| ↳ R2 Data Catalog | managed catalog (rejected) | Cloudflare service | PAYG (see §4) | **PAYG** (rejected on auth, not cost) | Managed, no license fee; rejected because coarse R2-token auth ≠ IdP model — not a cost issue. |
| ↳ iceberg-rest-fixture | local-dev/CI catalog | Apache-2.0 | $0 | **FREE-OSS** | Part of apache/iceberg. |
| **Compaction engine** | Trino (recommended), Flink, Spark | Apache-2.0 (all) | Self-host, $0 license | **FREE-OSS** | Operational cost only (a pod + schedule). This is the OSS replacement for Lakekeeper Plus's paid compaction. |
| **Query engine** | DuckDB ≥1.4 (target 1.5.x) | MIT | $0 (embedded) | **FREE-OSS** | Embedded in NGM pods, no separate cost. |
| ↳ PyIceberg | programmatic writer | Apache-2.0 | $0 | **FREE-OSS** | |
| **Search** | OpenSearch 2.x | Apache-2.0 | Self-host, $0 | **FREE-OSS** | Governed by OpenSearch Software Foundation (Linux Foundation, 2024). No feature paywall. Avoids the Elastic trap (see WATCH list). |
| **IdP** | Zitadel (self-hosted) | AGPL-3.0 (+ Apache/MIT subdirs) | Self-host, $0 | **FREE-OSS (WATCH)** | Self-hosted runs the same code as Cloud, no feature paywall. AGPL copyleft — fine for unmodified upstream self-host. **Avoid Zitadel Cloud Pro/Enterprise SaaS.** |
| ↳ OpenFGA | Lakekeeper authz backend | Apache-2.0 (CNCF) | Self-host, $0 | **FREE-OSS** | Free fine-grained authz; use this instead of Lakekeeper's paid Cedar backend. |
| **Auth lib (Django)** | PyJWT (`pyjwt[crypto]`) + PyJWKClient | MIT | $0 | **FREE-OSS** | |
| **Auth lib (FastAPI)** | PyJWT + PyJWKClient | MIT | $0 | **FREE-OSS** | Avoid python-jose for *security* reasons (CVEs), not cost. |
| ↳ zitadel-tools / TF provider | key2jwt, terraform-provider-zitadel | Apache-2.0 | $0 | **FREE-OSS** | |
| **Entity resolution** | Splink (on DuckDB) | MIT | $0 | **FREE-OSS** | Actively maintained (MoJ). |
| ↳ recordlinkage | ER fallback | BSD-3 | $0 | **FREE-OSS** | Free; lightly maintained. |
| ↳ dedupe | ER alternative (rejected) | MIT | $0 | **FREE-OSS** | Free; rejected on labeling burden, not cost. |
| ↳ rltk | ER alternative (rejected) | MIT | $0 | **FREE-OSS** | Free but abandoned — not a cost issue. |
| ↳ indic-transliteration / Aksharamukha | name normalization | MIT / open | $0 | **FREE-OSS** | |
| ↳ IndicXlit (AI4Bharat) | messy-romanization xlit | MIT | $0 | **FREE-OSS** | |
| ↳ rapidfuzz / jellyfish / py_stringmatching / textdistance | string similarity | MIT / GPL/BSD | $0 | **FREE-OSS** | All free. |
| **Relational DB** | PostgreSQL 15/16 | PostgreSQL License (BSD-like) | Self-host, $0 | **FREE-OSS** | Database-per-service; self-host or run on a node. If a *managed* Postgres (RDS/Cloud SQL) is ever chosen that becomes PAYG — keep it self-hosted/operator-run to stay at $0 license. |
| **Object storage (local dev)** | MinIO (`minio/minio`) | AGPL-3.0 | $0 | **FREE-OSS (WATCH)** | Free under AGPL, but repo archived/maintenance-mode and admin UI + LDAP/OIDC stripped to paid AIStor in 2025. Fine as an *unmodified local-dev S3 stand-in*. Consider Garage/SeaweedFS if you want a maintained dev server. See WATCH list. |
| **Object storage (prod)** | Cloudflare R2 | Cloudflare service | PAYG (see §4) | **PAYG** | No license fee; egress-free. The one accepted recurring cost. |
| **Orchestration** | Kubernetes (prod), docker-compose (dev) | Apache-2.0 / Apache-2.0 | Self-host, $0 | **FREE-OSS** | Use vanilla k8s or **k3s** (Apache-2.0). Avoid paid managed control planes priced per-cluster (see §5). |
| **App frameworks** | Django/DRF, FastAPI | BSD-3 / MIT | $0 | **FREE-OSS** | (Implicit in the services; listed for completeness.) |

> **Note (topology is stale; license verdicts are not):** The **FastAPI** rows (this
> table + §1's FastAPI auth-lib row) and the "**Database-per-service**" phrasing (Relational
> DB row) are **pre-monolith framing** — they predate the reversal to a single Django
> monolith with three DBs behind one router (no FastAPI ships; see `../../ARCHITECTURE.md`
> §1). Only the **LICENSE findings are load-bearing** here, and every license verdict holds
> regardless of topology: Django/DRF (BSD-3) and FastAPI (MIT) are both FREE-OSS, and the
> three-DB layout uses the same self-hosted PostgreSQL (FREE-OSS) whether it's one router
> or per-service.

---

## 2. RED — paid, must NOT use (swap to free OSS)

These are the only places the docs touch a paid product. Each has a free replacement already named in the research:

1. **Lakekeeper Plus (commercial tier)** — referenced as the way to get **automated compaction** and the **Cedar authorization backend**.
   - **Swap (compaction):** run compaction from a self-hosted **Trino** (`ALTER TABLE … EXECUTE optimize` + `remove_orphan_files`), or Flink/Spark, against the same Lakekeeper REST catalog. All Apache-2.0, $0 license. This is the explicitly-recommended OSS path in `duckdb-iceberg-r2-wiring.md §4`.
   - **Swap (authz):** use the **OpenFGA** backend (Apache-2.0, default authorizer) — NOT the paid Cedar backend. The docs already plan OpenFGA; just never enable Cedar.
   - **Net:** the Lakekeeper *OSS core* (Apache-2.0) fully covers our needs. Plus is never required.

2. **Zitadel Cloud (Pro $100/mo, Enterprise)** — the managed SaaS tiers.
   - **Swap:** **self-host Zitadel** (AGPL-3.0, same codebase, no feature paywall). Deployment-architecture already locks "real Zitadel container" self-hosted. Decision Q4 ("self-hosted vs Cloud") must be resolved to **self-hosted** to satisfy the constraint.

3. **R2 Data Catalog** — *not* paid in the license sense (it's PAYG), and it was already **rejected** on auth grounds. No action needed beyond confirming we stay on self-hosted Lakekeeper. Listed here only so nobody re-introduces it as a "free" zero-ops option — it has a rate card now (see §4).

**Nothing else in the docs requires a paid license.** Every other named technology is FREE-OSS.

---

## 3. WATCH — licensing traps / recently-changed terms

1. **MinIO (AGPLv3)** — *free today but degraded.* In **May 2025** MinIO stripped the embedded web console + external IdP (LDAP/OIDC) login from the community edition into the paid **AIStor** product; the OSS repo went **maintenance-mode (Dec 2025) and was archived (~early/mid 2026)**, with no pre-built binaries (build from source).
   - **Impact for us:** LOW. MinIO is only a **local-dev S3 stand-in**; prod uses R2. An unmodified AGPL build for local dev is free and compliant.
   - **Recommendation:** acceptable as-is, but since it's unmaintained consider a maintained free alternative for the dev S3 server: **Garage** (AGPL-3.0, single binary, actively maintained) or **SeaweedFS** (Apache-2.0). LocalStack S3 (Apache-2.0 core) is another option. Pin the MinIO image tag you use today.
   - **AGPL note:** AGPL is genuinely free/OSS but strong copyleft — only an issue if you *modify and network-expose* it. We don't; we run upstream unmodified.

2. **Elastic / Elasticsearch — the trap we are correctly avoiding.** Elasticsearch is **source-available**, not free OSS: Elastic License v2 + SSPL (and an AGPL option added 2024) — restricts offering it as a managed service and is not OSI-free in the v2/SSPL form. The docs choose **OpenSearch (Apache-2.0)** instead — correct. **Action:** never substitute Elasticsearch, Kibana, or Elastic-licensed plugins for OpenSearch/OpenSearch Dashboards.

3. **Zitadel AGPL-3.0** — free to self-host unmodified; just don't fork-and-modify-and-expose without meeting AGPL source-disclosure, and don't drift onto the Cloud paid tiers. WATCH-level only.

4. **Lakekeeper pre-1.0 + open-core model** — the OSS/Plus split means features can move behind the paid line in future releases. Pin a known-good OSS version; re-check release notes that compaction/authz you rely on stays in the free core.

5. **Managed Postgres / managed OpenSearch** — the deployment doc mentions "managed/operator-run Postgres" and "managed OpenSearch" for prod. *Managed* services are PAYG, not a license fee, but they add recurring cost. To honor "minimize cost," prefer **self-hosted (operator-run) Postgres and OpenSearch on the k8s cluster** over a paid managed service. No license trap, just a spend choice.

---

## 4. Estimated monthly $ floor — unavoidable PAYG bits

The only license-free-but-pay-per-use dependency is **Cloudflare R2** (object storage + optionally Data Catalog). Everything else is $0 license, self-hosted on compute you already run.

**R2 object storage** (current rate card, egress always free):
- Storage: **$0.015 / GB-month** (Standard); $0.010 (Infrequent Access).
- Class A ops (writes/lists): **$4.50 / million** (1M free/mo).
- Class B ops (reads): **$0.36 / million** (10M free/mo).
- Free tier: 10 GB-month + 1M Class A + 10M Class B per month.

**Illustrative floors (R2 only):**

| Scenario | Stored data | Ops/mo | Est. $/month |
|---|---|---|---|
| Dev / tiny (within free tier) | ≤10 GB | ≤1M A / ≤10M B | **~$0** |
| Small prod | 100 GB | ~2M A, ~20M B | storage $1.50 + A ~$4.50 + B ~$3.60 ≈ **~$10/mo** |
| Medium prod | 1 TB (1000 GB) | ~10M A, ~100M B | storage $15 + A ~$40 + B ~$32 ≈ **~$90/mo** |

**R2 Data Catalog** — **only if** you ever switch to the managed catalog (we don't; we self-host Lakekeeper). Note: per a live verification on 2026-06-25 (three days before this audit's 2026-06-28 date — the check predates the audit, so the dates are consistent) the **billing waiver appears to have ended** and the rate card is now active — catalog ops $9/M (1M free), compaction data $0.005/GB (10 GB free), compaction objects $2/M (1M free), snapshot expiry free. **Verify before relying on "free."** Since we self-host Lakekeeper + run our own Trino compaction, **we incur $0 of these** — only the underlying R2 storage/ops above.

**Practical monthly floor for the whole platform:** essentially **$0 in software licenses**, plus **R2 at roughly $0 (dev) to ~$10–90/month (small→medium prod)** for storage + operations. Compute (k8s nodes for the services, Postgres, OpenSearch, Zitadel, Lakekeeper, Trino) is infrastructure you provision; it carries no software-license cost under this design.

---

## 5. Concrete recommendations to keep everything free

1. **Catalog:** ship **Lakekeeper OSS core (Apache-2.0)**. Never enable Cedar authz (paid) — use **OpenFGA** (free). Never buy Lakekeeper Plus.
2. **Compaction:** stand up a small self-hosted **Trino** (or Flink) job against the Lakekeeper REST catalog for `optimize` + `remove_orphan_files`; use **PyIceberg `expire_snapshots`** for snapshot retention. This is the free replacement for Plus/R2-managed compaction.
3. **IdP:** resolve OPEN-QUESTION **Q4 → self-host Zitadel** (AGPL, free). Do not adopt Zitadel Cloud paid tiers.
4. **Search:** stay on **OpenSearch (Apache-2.0)**. Forbid any Elasticsearch/Kibana/Elastic-licensed substitution in code review.
5. **Object storage (dev):** keep MinIO unmodified, **or** migrate the local dev S3 server to maintained free OSS (**Garage** AGPL-3.0 / **SeaweedFS** Apache-2.0). Prod stays on **R2** (PAYG, accepted).
6. **Datastores/infra self-host:** prefer **self-hosted/operator-run Postgres and OpenSearch** over paid managed services to keep recurring cost minimal (PAYG-free).
7. **Kubernetes:** use **vanilla k8s or k3s (Apache-2.0)**; avoid paid per-cluster managed control planes and paid k8s distributions (OpenShift subscriptions, Rancher Prime, etc.). Self-managed or a free control plane only.
8. **Pin & monitor open-core projects** (Lakekeeper, MinIO): pin versions, and check each upgrade's release notes for features migrating from free core into a paid tier.
9. **R2 cost hygiene:** batch writes to minimize Class A ops, use Iceberg compaction to reduce small-file op counts, and exploit free egress; keep an eye on the R2 Data Catalog billing change (now active) in case anyone wires it in.

---

## Verification notes / cited sources (2026)

- **MinIO** AGPLv3, community feature-stripping (May 2025), repo archived: https://github.com/minio/minio · https://github.com/minio/minio/releases · https://min.io/pricing — free OSS dev alternatives: **Garage** (AGPL-3.0), **SeaweedFS** (Apache-2.0), **LocalStack** (Apache-2.0).
- **Lakekeeper** Apache-2.0 core; Plus = paid compaction + Cedar; steward Vakamo: https://github.com/lakekeeper/lakekeeper/blob/main/LICENSE · https://docs.lakekeeper.io/about/enterprise-release-notes/
- **Apache Polaris** Apache-2.0 TLP, fully free: https://github.com/apache/polaris
- **OpenSearch** Apache-2.0, OpenSearch Software Foundation (LF, 2024): https://opensearch.org — contrast Elastic License v2 / SSPL (source-available).
- **Zitadel** AGPL-3.0 self-host (free) vs Cloud paid tiers: https://github.com/zitadel/zitadel/blob/main/LICENSING.md · https://zitadel.com/pricing
- **OpenFGA** Apache-2.0, CNCF: https://github.com/openfga/openfga
- **DuckDB** MIT; **PyIceberg** Apache-2.0; **Splink** MIT; **PyJWT** MIT — established OSI licenses (per project repos / PyPI, cross-referenced in source research docs).
- **Cloudflare R2** pricing ($0.015/GB-mo, free egress, Class A $4.50/M, Class B $0.36/M): https://developers.cloudflare.com/r2/pricing/
- **R2 Data Catalog** pricing (rate card; billing now active per the 2026-06-25 check, which predates this 2026-06-28 audit): https://developers.cloudflare.com/r2/data-catalog/platform/pricing/

> Note: source doc `duckdb-iceberg-r2-wiring.md §2` recorded R2 Data Catalog billing as "WAIVED as of 2026-06-27." The live verification on 2026-06-25 (which predates both that note and this 2026-06-28 audit) indicates the rate card is now **active**. We don't depend on it (self-hosted Lakekeeper), but flag this discrepancy and re-confirm before any use.
