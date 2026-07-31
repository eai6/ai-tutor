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

## Part 3 — Sharing with government partners

**Decision (2026-07-31): government partners receive a full-fidelity copy —
the exact artifact needed to restore production. No anonymisation, no
redaction, no subsetting.**

They are authorised to access everything. They are also, for their own
students, a data controller in their own right rather than a third party
receiving someone else's data. Anonymising would defeat the purpose twice
over: a masked dump cannot restore a working system, and the whole point of
handing it over is that they can stand the platform up independently of us.

This is the primary sharing path. The anonymised path further down exists for
audiences who are *not* in that position.

### What "restore-grade" requires

A dump only counts as restore-grade if someone can rebuild a working tutor
from it without asking us anything. That means:

| requirement | why |
|---|---|
| **Custom format** (`-Fc`) or the vault's native output | lets `pg_restore` do selective restore, parallel restore, and reordering. A plain SQL file is all-or-nothing. |
| **Complete schema + data**, no `--exclude-table` | a missing table is only discovered when the app crashes on it |
| **`pgvector` extension** included | `curriculum_curriculumchunk` uses `vector(384)`; without the extension the KB will not load and the tutor runs ungrounded — the exact failure this project spent 2026-07-30 fixing |
| **Roles/globals** (`roles.sql`) | the vault emits this separately; without it, ownership and grants must be reconstructed by hand |
| **Extension list verified** | see the check below |

The database is self-contained apart from two things a dump cannot carry, and
both must be communicated alongside it:

1. **Media files** — lesson figures live in blob/file storage, not the
   database. `LessonStep.media` holds URLs. A restore without the media
   produces lessons with missing images. Ship the `media` container contents
   too, or the copy is incomplete in a way that is not obvious until a student
   hits a diagram.
2. **The tutor model** — `qwen3-4b` / cloud model config. The DB stores which
   model to use, not the model. See `memory/desktop_offline_app_plan.md`.

### Producing the handover artifact

Either route from Part 2 yields a restore-grade dump. Route A (vault) is
preferable for a scheduled handover — it does not touch production, and the
recovery point is a defined moment rather than "whenever the dump happened to
run".

```bash
# 1. Produce the dump (Route A or B from Part 2).

# 2. Verify it is restore-grade BEFORE sending. The TOC check is cheap and
#    catches a truncated or wrong-format file immediately.
pg_restore --list aitutor-YYYY-MM-DD.sql > toc.txt
grep -c ';' toc.txt                       # expect ~700 entries
grep -i 'EXTENSION - vector' toc.txt      # MUST match — no output means no KB
grep -c 'TABLE DATA' toc.txt              # expect ~70

# 3. Prove it restores, do not assume. Same procedure as the drill.
pg_restore -h 127.0.0.1 -p 5460 -U <admin> -d aitutor --no-owner --no-acl -j 4 \
  aitutor-YYYY-MM-DD.sql
psql ... -c "SELECT count(*) FROM tutoring_sessionturn;"   # compare to prod

# 4. Checksum, so the partner can confirm what they received is intact.
shasum -a 256 aitutor-YYYY-MM-DD.sql > aitutor-YYYY-MM-DD.sql.sha256
```

Step 3 is not optional ceremony. A dump that fails to restore is discovered
either by us in ten minutes or by the partner in a meeting.

### What to send with it

A dump alone is not a handover. Include:

- The dump + its `.sha256`
- `roles.sql` if using the vault output
- The media container contents (see above)
- Postgres major version (**16**) and the required extension (**pgvector**)
- Expected row counts at time of export, so they can verify their own restore
- A pointer to this runbook's Part 2 "Loading a dump locally"

### Transfer

Full-fidelity means every student record and every credential in one file.
Authorisation to receive it does not make the transit safe.

- **Azure SAS URL with a short expiry** (days, not months), or a
  government-designated secure transfer channel if they have one.
- **Never email it**, and never put it in a shared drive with broad access.
- **Log the handover**: who, when, which recovery point, what checksum. If the
  question "who has a copy of the student database?" is ever asked, the answer
  should be a list rather than a recollection.
- **Rotate the LLM API keys afterwards** if the dump included
  `llm_modelconfig.api_key_encrypted`. This is not about trusting the partner —
  it is that a credential which has been copied to another organisation's
  storage is no longer a credential only we control. Cheap to rotate, awkward
  to explain later. Alternatively, blank that one column before export: it is
  the only field whose removal does **not** impair a restore, since the
  restoring party supplies their own provider keys anyway.

### Withdrawn consent still applies

`safety_consentrecord.withdrawn_at` is honoured in the app. A full-fidelity
export by definition carries those rows too. That is defensible when the
recipient is the controller for those same students — they hold the
withdrawal record alongside the data and can act on it. It is worth confirming
explicitly with them that withdrawals will be honoured downstream, and noting
the confirmation in the handover log.

---

## Part 3b — Anonymised sharing (non-government audiences)

For anyone who is *not* an authorised controller — research partners,
vendors, conference datasets, demos.

The database carries, measured on the restored copy: `safety_consentrecord`
(`parent_name`, `parent_email`, `ip_address` — contact details for the parents
of minors), `auth_user` (names, emails, password hashes), live
`django_session.session_key` values, live invitation and verification tokens,
`llm_modelconfig.api_key_encrypted`, and `tutoring_sessionturn` free text where
students disclose personal things unprompted.

Prefer an **aggregate report** — sessions per school, completion rates, mastery
distributions — which answers most questions with no personal data at all.

Where row-level data is genuinely needed, anonymise a restored copy (never
production) and verify before sending:

```sql
TRUNCATE django_session, accounts_emailverificationtoken, accounts_staffinvitation;
UPDATE llm_modelconfig SET api_key_encrypted = '', api_key_env_var = '';
UPDATE auth_user SET
  username = 'student' || id, first_name = 'Student', last_name = id::text,
  email = 'student' || id || '@example.invalid', password = '!';
UPDATE safety_consentrecord SET
  parent_name = 'redacted', parent_email = NULL, ip_address = NULL;
UPDATE safety_safetyauditlog SET ip_address = NULL;
DELETE FROM auth_user WHERE id IN (
  SELECT user_id FROM safety_consentrecord WHERE withdrawn_at IS NOT NULL);
```

All four must return zero before the file leaves:

```sql
SELECT count(*) FROM auth_user WHERE email NOT LIKE '%@example.invalid';
SELECT count(*) FROM safety_consentrecord WHERE parent_email IS NOT NULL;
SELECT count(*) FROM django_session;
SELECT count(*) FROM llm_modelconfig WHERE api_key_encrypted <> '';
```

Free text resists all of this — no query finds a student naming their sister.
If transcripts are in scope, someone reads them first; if the volume makes that
impossible, send aggregates instead.

[PostgreSQL Anonymizer](https://postgresql-anonymizer.readthedocs.io/en/latest/anonymous_dumps/)
(available on Azure Flexible Server, v1.3.2) can automate the column rules via
a dumper role with `anon.transparent_dynamic_masking`, if this becomes routine.

---

## Quick runbook

**"A government partner needs a copy."** Route A → verify restore-grade (TOC
has `EXTENSION - vector`, ~700 entries) → prove it restores → checksum → send
via short-expiry SAS with the media contents, PG version, and expected row
counts → log the handover → rotate LLM keys. Full fidelity, no redaction.

**"Someone else wants data."** Ask what question they are answering; an
aggregate report usually answers it. If not: Route A → load locally →
anonymise (Part 3b) → verify 4 queries return zero → expiring link.

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
- **No handover log exists yet.** Part 3 says to record who received which
  recovery point and when. There is nowhere to record it. A file in this repo
  would do; the requirement is that the answer to "who holds a copy of the
  student database?" is a list, not a memory.
- **Media handover is unsolved.** The dump carries `LessonStep.media` URLs but
  not the files. Nobody has yet exported the blob container alongside a dump,
  so "ship the media too" is an instruction without a tested procedure.
  `memory/blob_media_hosting_plan.md` is mid-migration and affects this.
- **The Part 3b anonymisation SQL is written but untested.** It should be run
  against a restored copy and the verification queries confirmed before anyone
  relies on it in a hurry. Lower priority now that government handover is the
  primary path and does not use it.
- **Stale firewall rule** `allow-local` (108.31.132.119) on the production
  server should be reviewed and probably removed.
- **No scripted export.** Both routes are manual. A
  `manage.py export_anonymised_dump` command would make this repeatable and
  much harder to get wrong — recommended before the first real stakeholder
  request.
- **Staging is not in the backup vault**, so none of Route A applies to it.

Refs: memory/data_backup_and_recovery_plan.md,
auto-memory/project_pilot_consent_scope.md, auto-memory/deployment_pixel_prod.md
