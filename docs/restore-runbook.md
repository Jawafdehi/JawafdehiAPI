# Restore Runbook — Jawafdehi Infrastructure

**Last updated**: 2026-05-26
**Scope**: Cloud SQL PostgreSQL 18 instance `nepal-entity-service-db` (us-west1, project `newnepal2`) plus R2 off-site dumps.

## Database Inventory

All databases run on the single Cloud SQL instance `nepal-entity-service-db` (db-f1-micro, 15 GB PD_SSD, us-west1).

| Database | Application | CI Secret | Local Dump Path |
|----------|------------|-----------|-----------------|
| `nes_db` | JawafdehiAPI (default) | `DATABASE_URL_PROXY` | — |
| `ngm_v1` | JawafdehiAPI (NGM) | `NGM_DATABASE_URL_PROXY` | — |
| `paperclip1` | Paperclip control plane | `PAPERCLIP_DATABASE_URL_PROXY` | `/paperspace/state/instances/default/data/backups/paperclip-*.sql.gz` |
| `hindsight1` | Hindsight memory platform | `HINDSIGHT_DATABASE_URL_PROXY` | Managed by Hindsight internally |

**Backup layers**:
1. Cloud SQL automated daily backups (instance-level, 7-day retention, us-west1)
2. R2 daily dumps via GitHub Actions (`jawafdehi-api/.github/workflows/db-backup.yml`, 02:00 UTC) — per-DB custom-format archives
3. Paperclip internal hourly dumps (local to host, `paperclip-YYYYMMDD-HHMMSS.sql.gz`, 48-hour rolling retention)
4. `/paperspace` host-level tarball snapshots every 6 hours (8-snapshot retention)

## Connection Details

All connections go through Cloud SQL Proxy (systemd unit `cloudsql-proxy.service`) on port 5432 of the host. GitHub Actions workflows start their own proxy.

```
Instance:  newnepal2:us-west1:nepal-entity-service-db
Proxy:     127.0.0.1:5432 (systemd) / ephemeral (GitHub Actions)
User:      nes_user (nes_db, ngm_v1, paperclip1, hindsight1)
```

Secrets live in GitHub Environment "DB backup" and in `/home/ubuntu/.config/jawafdehi/.paperspace-secrets/paper-secrets.env`.

---

## Scenario A: Restore from Cloud SQL Automated Backup (instance-level)

**Use when**: Data corruption, accidental table drop, or need a point-in-time clone. Covers all 4 databases at once.

**RTO**: 30–60 min

1. List available backups:
```bash
gcloud sql backups list \
  --instance=nepal-entity-service-db \
  --project=newnepal2
```

2. Restore to a NEW instance (never overwrite production):
```bash
gcloud sql backups restore <BACKUP_ID> \
  --backup-instance=nepal-entity-service-db \
  --restore-instance=nepal-entity-service-db-restored \
  --project=newnepal2
```

3. Wait for restore completion:
```bash
gcloud sql operations list \
  --instance=nepal-entity-service-db-restored \
  --project=newnepal2
```

4. Get the restored instance IP:
```bash
gcloud sql instances describe nepal-entity-service-db-restored \
  --project=newnepal2 \
  --format='value(ipAddresses[0].ipAddress)'
```

5. Verify all 4 databases are present and accessible:
```bash
# Connect to restored instance
psql "postgresql://nes_user:<PASSWORD>@<RESTORED_IP>/postgres" -c "\l"
# Expected output should list: nes_db, ngm_v1, paperclip1, hindsight1
```

6. Run row-count sanity checks against each DB:
```bash
# nes_db — entity count
psql "postgresql://nes_user:<PASSWORD>@<RESTORED_IP>/nes_db" \
  -c "SELECT relname, n_live_tup FROM pg_stat_user_tables ORDER BY n_live_tup DESC LIMIT 10;"

# ngm_v1 — case count
psql "postgresql://nes_user:<PASSWORD>@<RESTORED_IP>/ngm_v1" \
  -c "SELECT relname, n_live_tup FROM pg_stat_user_tables ORDER BY n_live_tup DESC LIMIT 10;"

# paperclip1 — issue count
psql "postgresql://nes_user:<PASSWORD>@<RESTORED_IP>/paperclip1" \
  -c "SELECT count(*) FROM issues;"

# hindsight1 — fact count
psql "postgresql://nes_user:<PASSWORD>@<RESTORED_IP>/hindsight1" \
  -c "SELECT count(*) FROM facts;"
```

7. Cutover (if replacing primary):
```bash
# Stop dependent services
sudo systemctl --user stop paperclip hindsight

# Swap DATABASE_URL references to point at restored instance

# Start services
sudo systemctl --user start paperclip hindsight
```

8. Delete the clone after verification:
```bash
gcloud sql instances delete nepal-entity-service-db-restored --project=newnepal2
```

---

## Scenario B: Point-in-Time Recovery (instance-level clone)

**Use when**: Bad migration, accidental UPDATE without WHERE. Requires PITR enabled.

**RTO**: 30–60 min

```bash
# Determine recovery timestamp in UTC (just before the incident)
# e.g., 2026-05-25T14:00:00.000Z

gcloud sql instances clone nepal-entity-service-db \
  nepal-entity-service-db-pitr \
  --point-in-time="2026-05-25T14:00:00.000Z" \
  --project=newnepal2
```

Verify and cutover per Scenario A steps 4–8.

---

## Scenario C: Restore Single Database from R2 Dump

**Use when**: Only one database is affected, other DBs are healthy. Restore from the R2 custom-format archive.

**RTO**: 10–30 min (single database)

1. Download the latest backup from R2:
```bash
BACKUP_DATE="2026-05-25"  # adjust
DB="nes_db"               # nes_db | ngm_v1 | paperclip1 | hindsight1

aws s3 cp \
  "s3://jawafdehi-admin/backups/${DB}-${BACKUP_DATE}.dump" \
  "./${DB}-${BACKUP_DATE}.dump" \
  --endpoint-url="https://<ACCOUNT_ID>.r2.cloudflarestorage.com" \
  --region=auto
```

2. Create a fresh empty database on the target (if needed):
```bash
psql "postgresql://nes_user:<PASSWORD>@127.0.0.1/postgres" \
  -c "CREATE DATABASE ${DB}_restored OWNER nes_user;"
```

3. Restore with pg_restore in parallel (4 jobs for db-f1-micro):
```bash
pg_restore \
  --dbname="postgresql://nes_user:<PASSWORD>@127.0.0.1/${DB}_restored" \
  --jobs=4 \
  --no-owner \
  --no-privileges \
  --clean \
  --if-exists \
  "${DB}-${BACKUP_DATE}.dump"
```

4. Verify data integrity (row counts, recent records):
```bash
psql "postgresql://nes_user:<PASSWORD>@127.0.0.1/${DB}_restored" \
  -c "SELECT count(*) FROM ...;"
```

5. If replacing the live DB, rename in a transaction:
```sql
-- Disconnect all other clients first
SELECT pg_terminate_backend(pg_stat_activity.pid)
  FROM pg_stat_activity
  WHERE pg_stat_activity.datname = 'nes_db'
    AND pid <> pg_backend_pid();

ALTER DATABASE nes_db RENAME TO nes_db_old;
ALTER DATABASE nes_db_restored RENAME TO nes_db;
```

6. Clean up:
```bash
psql "postgresql://nes_user:<PASSWORD>@127.0.0.1/postgres" \
  -c "DROP DATABASE IF EXISTS nes_db_old;"
```

---

## Scenario D: Restore All Databases from R2 Dumps

**Use when**: Cloud SQL automated backup is unavailable, or need to restore to a different PostgreSQL instance (e.g., region failure DR).

**RTO**: 1–3 hours

1. Download all DB dumps from R2 for the desired date:
```bash
BACKUP_DATE="2026-05-25"
for DB in nes_db ngm_v1 paperclip1 hindsight1; do
  aws s3 cp \
    "s3://jawafdehi-admin/backups/${DB}-${BACKUP_DATE}.dump" \
    "./${DB}-${BACKUP_DATE}.dump" \
    --endpoint-url="https://<ACCOUNT_ID>.r2.cloudflarestorage.com" \
    --region=auto
done
```

2. Create a new Cloud SQL instance (DR scenario):
```bash
gcloud sql instances create nepal-entity-service-db-dr \
  --database-version=POSTGRES_18 \
  --tier=db-f1-micro \
  --region=us-central1 \
  --storage-size=15 \
  --storage-type=PD_SSD \
  --project=newnepal2
```

3. Create all 4 databases on the new instance:
```bash
for DB in nes_db ngm_v1 paperclip1 hindsight1; do
  psql "postgresql://nes_user:<PASSWORD>@<DR_INSTANCE_IP>/postgres" \
    -c "CREATE DATABASE ${DB} OWNER nes_user;"
done
```

4. Restore each database in parallel:
```bash
for DB in nes_db ngm_v1 paperclip1 hindsight1; do
  pg_restore \
    --dbname="postgresql://nes_user:<PASSWORD>@<DR_INSTANCE_IP>/${DB}" \
    --jobs=4 --no-owner --no-privileges --clean --if-exists \
    "${DB}-${BACKUP_DATE}.dump" &
done
wait
```

5. Verify all databases, deploy services, update DNS (Cloudflare).

---

## Scenario E: Restore from Paperclip Local Dumps

**Use when**: Only Paperclip DB is affected, and its own hourly dumps are more recent than R2.

**RTO**: 5–15 min

```bash
LATEST=$(ls -t /paperspace/state/instances/default/data/backups/paperclip-*.sql.gz | head -1)
gunzip -c "$LATEST" | psql "postgresql://nes_user:<PASSWORD>@127.0.0.1/paperclip1_restored"
```

Verify, then swap database names as in Scenario C.

---

## Restore Drill Checklist

Run this drill against a CLONE instance only. Delete the clone after verification.

1. [ ] Create a clone: `gcloud sql instances clone nepal-entity-service-db nepal-entity-service-db-drill --project=newnepal2`
2. [ ] Note the clone start time (`date -u +%s`) for RTO measurement
3. [ ] Wait for clone to become RUNNABLE
4. [ ] Connect and verify all 4 databases (`\l` in psql)
5. [ ] Run row-count checks on each DB
6. [ ] Verify Hindsight is reachable and queryable
7. [ ] Verify Paperclip API health endpoint responds
8. [ ] Note the end time, compute RTO
9. [ ] Record results in the issue thread
10. [ ] Delete the clone: `gcloud sql instances delete nepal-entity-service-db-drill --project=newnepal2`

---

## Alerting

Backup failure is detected via:
- GitHub Actions CI failure on `db-backup.yml` (notifies via GitHub)
- Cloud SQL backup failure logged to Cloud Logging (`cloudsql.googleapis.com/postgres.log`)
- Missing R2 backups: verify with `aws s3 ls s3://jawafdehi-admin/backups/ --endpoint-url=... | tail`

Paperclip internal backup health is visible at `/paperspace/var/logs/paperclip.log` (hourly dump entries).

## References

- [JAWA-263 Research Memo](/JAW/issues/JAWA-263#document-research-memo)
- Primary instance: `newnepal2:us-west1:nepal-entity-service-db`
- Terraform: `infra/terraform/main.tf` (Cloud SQL block)
- DB backup workflow: `.github/workflows/db-backup.yml`
- Cloud SQL proxy: `cloudsql-proxy.service`
- Paperclip backup path: `/paperspace/state/instances/default/data/backups/`
- Host backup: `/paperspace/services/backup/backup-paperspace.sh`
