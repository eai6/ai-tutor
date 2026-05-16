# Database Backup Hardening — Plan (2026-05-15)

Production Postgres (Azure Flexible Server) currently runs with
default backup config — only ~7 days of point-in-time recovery,
single-region, no second-destination. A credential compromise could
drop the DB AND its backups in the same Azure account. Need
defense-in-depth.

## Problem

User concern (2026-05-15): "I am worried about losing data to an
error from our side or a cyber attack."

Single-account, single-region backup gives zero protection against:
  - Subscription-level credential compromise (attacker drops DB +
    purges backups)
  - Insider error past the 7-day PITR window
  - Regional Azure outage (unlikely but possible)
  - Botched migration that corrupts data and goes unnoticed for
    weeks

The Pilot data (Seychelles + Tanzania planning) is irreplaceable —
real student session transcripts, teacher edits, judge verdicts that
took months to accumulate. Losing it would set the project back to
zero.

## Current state (from audit)

- `infra/__main__.py:218-232` — `dbforpostgresql.Server` created
  with NO explicit backup args:
  - `backup_retention_days` defaults to **7 days** for Flex Server
  - `geo_redundant_backup` defaults to **Disabled** (single-region)
  - `auto_grow_enabled` defaults to True
- No `pg_dump` cron job, no GitHub Action, no second-destination
  backup. Azure Storage Account exists (`infra/__main__.py:171`)
  for media file share but is NOT used for DB dumps.
- No DR drill — backups have never been tested for restoreability.

## Target design — defense in depth

Three independent layers, each plugging a different failure mode:

### Layer 1 — Hardened Azure-native PITR (cheap, immediate)

Update `infra/__main__.py` Pulumi config:

```python
pg_server = dbforpostgresql.Server(
    pg_server_name,
    ...
    backup=dbforpostgresql.BackupArgs(
        backup_retention_days=35,                  # MAX for Flex Server
        geo_redundant_backup=dbforpostgresql.GeoRedundantBackupEnum.ENABLED,
    ),
    high_availability=dbforpostgresql.HighAvailabilityArgs(
        mode=dbforpostgresql.HighAvailabilityMode.ZONE_REDUNDANT,
    ),
    ...
)
```

Effect: PITR window grows from 7 → 35 days; geo-redundant means
backups replicated to a paired region; zone-redundant HA gives
sub-minute failover within the region.

Trade-off: zone-redundant HA + geo-redundant backup roughly doubles
the DB cost (Standard_B1ms → maybe Standard_D2s_v3 since HA needs
General Purpose tier). Cost worth the resilience.

**Defends against:** Azure regional outage, accidental DROP within
35 days, single-zone failure. Does NOT defend against subscription
credential compromise (attacker can disable backups in same
subscription).

### Layer 2 — Daily `pg_dump` to immutable Blob (defends against compromise)

Independent of Azure-native backups. Daily GitHub Action runs
`pg_dump` and writes to a SEPARATE blob container with **immutability
policy** so the bytes can't be deleted or overwritten until the
retention period expires.

```yaml
# .github/workflows/db-backup-daily.yml
name: Daily Postgres backup
on:
  schedule:
    - cron: '0 3 * * *'  # 03:00 UTC daily
  workflow_dispatch: {}
jobs:
  backup:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Install pg_dump
        run: sudo apt-get update && sudo apt-get install -y postgresql-client-16
      - name: Azure login
        uses: azure/login@v2
        with:
          creds: ${{ secrets.AZURE_CREDENTIALS }}
      - name: Run pg_dump
        run: |
          pg_dump --format=custom --compress=9 \
            "${{ secrets.PG_BACKUP_DSN }}" > backup.dump
      - name: Upload to immutable Blob
        run: |
          DATE=$(date -u +%Y-%m-%d)
          az storage blob upload \
            --account-name aitutorpixelacr \
            --container-name db-backups \
            --name "pg/${DATE}/aitutor.dump" \
            --file backup.dump \
            --auth-mode login
```

Pulumi adds:
  - New blob container `db-backups` with immutability policy:
    `time_based_retention_in_days=30` and `protected_append_writes_only=False`
    (legal hold mode would be even stronger — admin can't release
    until a court order)
  - Lifecycle rule: move blobs older than 90 days to Archive tier
    (5x cheaper); delete after 1 year

**Defends against:** subscription compromise (attacker still can't
delete immutable blobs within retention window), insider error past
35-day PITR, ransomware. The pg_dump runs OUTSIDE Azure (GitHub
runner), so even if Azure is compromised the historical dumps
survive.

### Layer 3 — Quarterly DR drill (verifies restorability)

Untested backups are no backups. A scripted quarterly drill:

1. Spin up a sandbox Postgres (Pulumi `dr-test` stack — temporary,
   cheapest tier)
2. Pick a random dump from the immutable blob (last 30 days)
3. `pg_restore` into the sandbox
4. Run a smoke check: `python manage.py check`, `python manage.py
   migrate --check`, count rows on key tables (`accounts_user`,
   `tutoring_sessionturn`, `curriculum_lessonstep`), random sample
   integrity check on a few sessions
5. Tear down the sandbox
6. Post the drill result to a tracking doc

Cadence: quarterly + after every Postgres major version bump +
after every schema migration that touches > 3 tables.

**Defends against:** "we have backups but they don't actually
restore" — the most common backup failure mode.

## Data model changes

None. Postgres backups operate at the storage layer.

## Backend changes

None — application code is unchanged.

## Infrastructure changes

| File | Change |
|---|---|
| `infra/__main__.py:218-232` | Add `BackupArgs`, `HighAvailabilityArgs`. Probably bump SKU from Standard_B1ms (Burstable) → Standard_D2s_v3 (General Purpose) since HA requires GP tier |
| `infra/__main__.py:171` (Storage Account) | Add new blob container `db-backups` with immutability + lifecycle policy |
| `.github/workflows/db-backup-daily.yml` | NEW — daily pg_dump → blob upload |
| `.github/workflows/db-restore-drill.yml` | NEW — manual-trigger quarterly drill: restore + smoke + report |
| `infra/Pulumi.pixel.yaml` (encrypted) | Add `pg_backup_dsn` secret (read-only DB user creds preferred over admin) |

## Out of scope (deferred)

- **Cross-subscription backup destination** — true defense-in-depth
  would write dumps to a SEPARATE Azure subscription that the prod
  service principal can't access. Adds significant complexity (two
  service principals, cross-tenant trust). Defer until Layer 1+2 are
  rock-solid in production.
- **Per-tenant backup isolation** — useful when multi-tenant becomes
  a hard requirement (e.g. Tanzania Pilot wants their own data
  separate). Not relevant for the single-tenant Seychelles pilot.
- **Real-time CDC / replication** — overkill at current scale.
- **Backup of ChromaDB / vector DB** — vectordb is regenerable from
  source materials (re-index on demand). Not in DR scope.
- **Backup of Azure File Share / Blob media** — separate plan
  (`memory/azure_blob_migration_plan.md`).

## Phased delivery

| Phase | Work | Days | Risk |
|---|---|---|---|
| **B1** Pulumi config update | Add BackupArgs + HA + bump SKU. `pulumi preview` carefully — SKU change MAY trigger DB recreate (need to confirm and migrate data first) | 0.5 | High — recreate is catastrophic if not handled. Snapshot before. |
| **B2** Pulumi blob container with immutability | `db-backups` container + immutability policy + lifecycle rule | 0.25 | Low |
| **B3** GitHub Action daily pg_dump | `.github/workflows/db-backup-daily.yml` + secrets wiring + first manual run | 0.5 | Low — failure just means no backup that day, alerted via Actions |
| **B4** Restore drill workflow | `.github/workflows/db-restore-drill.yml` + first manual drill | 0.5 | Low |
| **B5** Document the runbook | One short doc: how to restore, how to trigger a drill, what to do if a backup fails | 0.25 | Trivial |

Total: ~2 days. Ship B1 + B3 first as the highest-impact pair.

## Risks

- **B1 SKU change is destructive on Burstable tier** — moving to
  General Purpose may force a recreate. MUST take a manual `pg_dump`
  before the Pulumi up. Ideally: pre-build the new server, dual-write
  briefly, swap. Or: schedule a maintenance window and accept ~30
  min downtime.
- **GitHub Action credentials sprawl** — the workflow needs prod DB
  creds. Use a READ-ONLY DB user just for backups, not the admin.
  Store in GitHub Secrets, never log.
- **Immutability is hard to undo** — once the policy is set with
  time-based retention, those bytes literally cannot be deleted by
  anyone (including us) until expiry. Start with 30-day retention,
  not 1-year, so a misconfiguration doesn't lock us out of storage
  for a year.
- **DR drill cost** — each quarterly restore spins up a sandbox DB
  (~$5/run). Tear down immediately after.

## Open questions

1. **Backup retention period for the daily dumps?** Recommend 30
   days at hot tier, 1 year at archive tier. Total cost: tiny.
2. **DR drill cadence — quarterly enough?** Recommend quarterly +
   after every schema migration touching > 3 tables. Anything more
   frequent is operational toil for marginal value.
3. **Immutability policy mode — time-based or legal-hold?**
   Recommend time-based for most blobs (auto-expires); legal-hold
   for one quarterly snapshot (stays until manually released — the
   "in case of emergency" copy).
4. **Should the backup workflow include vectordb / blob media?**
   Recommend NO for v1. ChromaDB is regenerable; media is a
   separate concern handled by the blob migration plan.
5. **Cross-subscription destination — when?** Defer until pilot
   data exceeds 100k transcripts OR a partner explicitly requires
   it for compliance. Today's threat model is well-handled by
   immutable blobs in same subscription.

## Next step

Confirm open questions (especially #3 — locking ourselves into a
1-year immutability policy on day one is the kind of mistake that
takes a year to undo). Then start B1 (Pulumi backup args), but
ONLY after taking a manual `pg_dump` snapshot — the SKU change can
recreate the server.

Refs: memory/azure_blob_migration_plan.md (sibling — they share
the storage account but have different retention + access patterns)
