# Database Dump, Restore & Stakeholder Sharing — Runbook (2026-07-31)

Three things in one document, because they are the same pipeline seen from
different ends:

1. **What the restore drill proved** (2026-07-30) and the non-obvious mechanics
   it uncovered.
2. **How to get a database dump** when you need one.
3. **How to share data with stakeholders without leaking a child's home
   contact details.**

Pairs with `memory/data_backup_and_recovery_plan.md`, which covers the backup
configuration itself. This document is the *recovery and egress* half.

---

## Part 1 — Restore drill results (2026-07-30)

**Verdict: the backup is real and restorable.** Every row count matched
production exactly.

| table | production | restored |
|---|---|---|
| `tutoring_sessionturn` | 36,109 | **36,109** |
| `tutoring_tutorsession` | 1,106 | **1,106** |
| `curriculum_lesson` | 354 | **354** |
| `dashboard_teachingmaterialupload` | 482 | **482** |
| `curriculum_lessonstep` | — | 2,826 |
| `tutoring_exitticketattempt` | — | 1,526 |
| `auth_user` | — | 389 |
| tables restored | — | 70 |

`pgvector` is present in the dump (`EXTENSION - vector` in the `pg_restore
--list` TOC), which was the single most likely way a restore could come back
useless here — the KB is unqueryable without it.

### Measured RTO

| stage | time |
|---|---|
| Vault restore → blob | **2 min 15 s** |
| Download 18 MB dump | seconds |
| `pg_restore` into a server | **< 1 s** |
| *(provision a target server, if none exists)* | ~5 min |
| **end to end** | **≈ 10 min** |

That is the number to quote a school. It assumes someone who has read this
document; without it, add an hour of discovery.

### Four things that would have burned an hour mid-incident

1. **Vaulted restore cannot restore to a server.** Both "new server" and
   "existing server" targets fail with `RestoreToTargetServerNotSupported`.
   Azure Backup for PostgreSQL Flexible Server does **restore-as-files only**:
   it writes a dump to blob storage and you load it yourself.
2. **Recovery therefore needs a storage account that does not exist yet.**
   Provision server → restore to blob → download → `pg_restore` → repoint app.
   Pre-creating a permanent restore-target storage account would remove a step
   from the critical path.
3. **`--target-resource-id` must be the CONTAINER's ARM id**, not the storage
   account's, or it fails with the misleading `TargetResourceArmId field is
   invalid or it points to a different resource than the Url field`.
   Correct form: `<storage-account-id>/blobServices/default/containers/<name>`.
4. **The dump is named `.sql` but is PostgreSQL custom format** (`PGDMP`
   magic). `psql -f` fails on it in a confusing way; use `pg_restore`.

Two more, smaller:

- A flexible server created with `--public-access None` **cannot** have
  firewall rules added later ("Firewall rule operations cannot be requested for
  a server that doesn't have public access enabled"). Public vs private access
  is fixed at creation. Create restore targets with `--public-access <your-ip>`.
- The vault's managed identity needs **`Storage Blob Data Contributor`** on the
  restore target, in addition to the roles it already holds on the source.

---

## Part 2 — How to get a dump

### Route A — from the backup vault (no production contact)

Preferred when the dump is for analysis, sharing, or a dev refresh. It reads a
recovery point and never touches the live database, so it cannot slow
production or hold locks.

```bash
VAULT=aitutor-backup-vault; VRG=aitutor-backup-rg
INST=$(az dataprotection backup-instance list --vault-name $VAULT -g $VRG --query "[0].name" -o tsv)
RP=$(az dataprotection recovery-point list --backup-instance-name "$INST" \
      --vault-name $VAULT -g $VRG --query "[0].name" -o tsv)

# Storage account + container to receive the dump (see note in Part 1 item 2).
SA=<storage-account>; SARG=<its-rg>
SAID=$(az storage account show -n $SA -g $SARG --query id -o tsv)
CONTID="$SAID/blobServices/default/containers/restore"

# The vault writes the blob, so its identity needs write access.
az role assignment create --assignee-object-id <vault-msi-object-id> \
  --assignee-principal-type ServicePrincipal \
  --role "Storage Blob Data Contributor" --scope "$SAID"

az dataprotection backup-instance restore initialize-for-data-recovery-as-files \
  --datasource-type AzureDatabaseForPostgreSQLFlexibleServer \
  --restore-location centralus --source-datastore VaultStore \
  --recovery-point-id "$RP" \
  --target-blob-container-url "https://$SA.blob.core.windows.net/restore" \
  --target-file-name "aitutor-$(date +%F)" \
  --target-resource-id "$CONTID" > restore_req.json

# ALWAYS validate first — it catches the ARM-id and permission mistakes
# in seconds instead of after a long-running job.
az dataprotection backup-instance validate-for-restore --backup-instance-name "$INST" \
  --vault-name $VAULT -g $VRG --restore-request-object restore_req.json

az dataprotection backup-instance restore trigger --backup-instance-name "$INST" \
  --vault-name $VAULT -g $VRG --restore-request-object restore_req.json
```

Output blobs: one `<guid>_database_<name>.sql` per database. The one that
matters is `database_aitutor.sql` (~18 MB as of 2026-07-30). The rest
(`postgres`, `template1`, `azure_sys`, `azure_maintenance`, `roles`) are
near-empty.

### Route B — direct `pg_dump` from production

Use when you need *this moment's* data rather than the last recovery point.
It reads the live database, so prefer off-peak.

```bash
MYIP=$(curl -s https://api.ipify.org)
az postgres flexible-server firewall-rule create -g aitutor-pixel-rg -n aitutor-pixel-pg \
  --rule-name tmp-dump --start-ip-address $MYIP --end-ip-address $MYIP

DBURL=$(az containerapp secret show -n aitutor-pixel-app -g aitutor-pixel-rg \
        --secret-name database-url --query value -o tsv)
pg_dump "$DBURL" -Fc -f aitutor-prod-$(date +%F).dump

# Removing the rule requires lifting the resource-group lock first — a
# CanNotDelete lock blocks deleting SUB-resources too, firewall rules included.
az lock delete --name prod-no-delete --resource-group aitutor-pixel-rg
az postgres flexible-server firewall-rule delete -g aitutor-pixel-rg -n aitutor-pixel-pg \
  --rule-name tmp-dump --yes
az lock create --name prod-no-delete --lock-type CanNotDelete \
  --resource-group aitutor-pixel-rg --notes "Student data + backups."
```

**Never leave the firewall rule in place.** The existing `allow-local` rule
points at a stale IP (108.31.132.119) and should be reviewed.

### Loading a dump locally

```bash
export PATH="/opt/homebrew/opt/postgresql@16/bin:$PATH"
initdb -D /tmp/pgd/data -U drilladmin --auth=trust
# -k with a SHORT socket dir: the default path under a long working directory
# exceeds the 103-byte Unix-socket limit and the server refuses to start.
pg_ctl -D /tmp/pgd/data -o "-p 5460 -k /tmp/pgd" -l /tmp/pgd/pg.log start
psql -h 127.0.0.1 -p 5460 -U drilladmin -d postgres -c "CREATE DATABASE aitutor;"
pg_restore -h 127.0.0.1 -p 5460 -U drilladmin -d aitutor --no-owner --no-acl -j 4 dump.sql
```

`pgvector` must be installed for `curriculum_curriculumchunk` to load. Homebrew's
`pgvector` builds against the newest Postgres only (17/18 as of 2026-07), so on
`postgresql@16` the extension is missing and that one table fails while
everything else restores. Either match the Postgres major version to the
available pgvector build, or accept that the KB table is skipped.

---

## Part 3 — Sharing data with stakeholders

### The database is not shareable as-is

Measured on the restored copy, these columns carry personal or secret data:

| table | columns | why it matters |
|---|---|---|
| `safety_consentrecord` | `parent_name`, `parent_email`, `ip_address` | **contact details of the parents of minors** — the most sensitive data in the system |
| `auth_user` | `email`, `first_name`, `last_name`, `username`, `password` | directly identifies students |
| `accounts_staffinvitation` | `email`, `token` | live invitation tokens |
| `accounts_emailverificationtoken` | `token` | live tokens |
| `django_session` | `session_key` | **live session cookies — an attacker can impersonate a logged-in user** |
| `llm_modelconfig` | `api_key_encrypted` | provider credentials |
| `safety_safetyauditlog` | `ip_address`, `details` | location-adjacent |
| `tutoring_sessionturn` | `student_input`, tutor text | **free text — students disclose personal things unprompted** |

That last row is the one that resists automation. Names, schools and family
details appear inside conversation text where no column-level rule finds them.

### Decide what the stakeholder actually needs

Most requests do not need row-level data at all. In rough order of preference:

1. **Aggregate report** — sessions per school, completion rates, mastery
   distributions, time-on-task. Answers most "how is the pilot going?"
   questions with zero personal data. Prefer this.
2. **Anonymised sample** — a few hundred de-identified transcripts for
   pedagogy review. Needs the treatment below.
3. **Anonymised full dump** — for a research partner under agreement.
4. **Raw dump** — only to someone already a data controller for this data
   (e.g. the Ministry for its own students), under a written agreement, and
   never over email or a public link.

### Anonymisation

Two workable approaches.

**Approach A — anonymise a restored copy (recommended).**
Restore per Route A into a scratch server you control, anonymise there, then
re-dump. Production is never touched, and you are not constrained by Azure's
extension configuration limits. It also composes with the drill: the same
restore that proves recoverability produces the shareable artifact.

**Approach B — `anon` on Azure.**
[PostgreSQL Anonymizer](https://postgresql-anonymizer.readthedocs.io/en/latest/anonymous_dumps/)
is available on Azure Flexible Server (v1.3.2). Declare masking rules on
columns, create a dedicated dumper role with
`ALTER ROLE anon_dumper SET anon.transparent_dynamic_masking TO TRUE`, and
`pg_dump` as that role to get a masked export. Caveats: Azure blocks some GUC
changes from the portal, and `anon` is unsupported by in-place major version
upgrades. Verify on staging before relying on it in a workflow.

Minimum treatment before anything leaves the building:

```sql
-- Secrets and live credentials: DROP the rows, do not mask them.
TRUNCATE django_session, accounts_emailverificationtoken, accounts_staffinvitation;
UPDATE llm_modelconfig SET api_key_encrypted = '', api_key_env_var = '';

-- Direct identifiers: replace, keeping referential integrity via the id.
UPDATE auth_user SET
  username   = 'student' || id,
  first_name = 'Student', last_name = id::text,
  email      = 'student' || id || '@example.invalid',
  password   = '!';                       -- unusable-password marker

-- Parent contact details: remove entirely. Consent is provable from
-- given/given_at without knowing who the parent is.
UPDATE safety_consentrecord SET
  parent_name = 'redacted', parent_email = NULL, ip_address = NULL;
UPDATE safety_safetyauditlog SET ip_address = NULL;
```

Then, for free text, **sample and read**. There is no query that reliably finds
a student mentioning their sister by name. If you are sharing transcripts,
someone reads them first. If the volume makes that impossible, share aggregates
instead — that constraint is the point, not an obstacle.

### Verify before sending

```sql
SELECT count(*) FROM auth_user WHERE email NOT LIKE '%@example.invalid';  -- expect 0
SELECT count(*) FROM safety_consentrecord WHERE parent_email IS NOT NULL; -- expect 0
SELECT count(*) FROM django_session;                                      -- expect 0
SELECT count(*) FROM llm_modelconfig WHERE api_key_encrypted <> '';       -- expect 0
```

Re-dump only after all four return zero. Ship the checksum with the file, and
transfer via a link that expires — a SAS URL with a short TTL, not email.

### Consent and legal

Pilot consent covers research use (`auto-memory/project_pilot_consent_scope.md`),
so analysis of pilot data does not need re-consent. That is **not** the same as
permission to hand identifiable records to a third party. Consent covering "we
will study this" does not cover "we will give your child's transcript to a
partner organisation". When in doubt, share aggregates and ask.

`safety_consentrecord` has a `withdrawn_at` column. **Any export must exclude
withdrawn users** — a withdrawal that is honoured in the app but not in exports
is a withdrawal in name only:

```sql
-- Before exporting, drop data for anyone who has withdrawn.
DELETE FROM auth_user WHERE id IN (
  SELECT user_id FROM safety_consentrecord WHERE withdrawn_at IS NOT NULL);
```

---

## Quick runbook

**"I need a dump for analysis."** Route A → load locally → anonymise →
verify 4 queries → share via expiring link.

**"Production is broken, restore it."** Route A into a *new* server →
`pg_restore` → repoint `DATABASE_URL` on the container app → verify row counts.
≈10 min. Do not delete the broken server until the replacement is confirmed —
the lock helps here.

**"A stakeholder wants to see the data."** Ask what question they are answering.
Usually an aggregate report is the correct and faster answer.

---

## Open items

- **No permanent restore-target storage account.** Creating one in
  `aitutor-backup-rg` removes a step from the recovery critical path.
- **The anonymisation SQL above is written but untested.** It should be run
  against a restored copy and the verification queries confirmed before anyone
  relies on it in a hurry.
- **Stale firewall rule** `allow-local` (108.31.132.119) on the production
  server should be reviewed and probably removed.
- **No scripted export.** Both routes are manual. A
  `manage.py export_anonymised_dump` command would make this repeatable and
  much harder to get wrong — recommended before the first real stakeholder
  request.
- **Staging is not in the backup vault**, so none of Route A applies to it.

Refs: memory/data_backup_and_recovery_plan.md,
auto-memory/project_pilot_consent_scope.md, auto-memory/deployment_pixel_prod.md
