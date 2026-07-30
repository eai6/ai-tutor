# Data Backup & Recovery — Plan (2026-07-30)

## Problem

Production holds real student data for the Seychelles pilot — tutoring
sessions, turns, competency records, exit-ticket attempts — plus the entire
curriculum and its media. Its backup posture has never been audited. This
documents what is actually configured today, what it does and does not protect
against, and what to change.

**Headline: production is not currently protected against its most likely
catastrophic failure.** Azure's automated backups live *inside* the database
server resource. Deleting the server — via `pulumi destroy`, an `az group
delete`, or a portal misclick — permanently destroys the database **and every
backup of it simultaneously**. There are no resource locks anywhere in the
subscription, and no backup exists outside Azure's own retention.

## Measured current state (2026-07-30, `az` against Pixel Design Labs LLC)

**PostgreSQL — `aitutor-pixel-pg` (prod) and `aitutor-staging-pg` (staging):**

| setting | value |
|---|---|
| Point-in-time retention | **7 days** (earliest restore 2026-07-24) |
| Geo-redundant backup | **Disabled** |
| High availability | **Disabled** |
| SKU / storage | Standard_B1ms / 32 GB |
| Version | PostgreSQL 16 |

**Media — storage account `aitutorpixelsa`:**

| setting | value |
|---|---|
| Redundancy | **Standard_LRS** (single region, single zone) |
| Blob soft delete | **Disabled** |
| Container soft delete | **Disabled** |
| Blob versioning | **Disabled** |
| Point-in-time restore | **Not configured** |
| Change feed | Disabled |
| Layout | an SMB file share `media` **and** a blob container `media` |

**Subscription-wide:**

| setting | value |
|---|---|
| Resource locks | **None, on any resource group** |
| Logical dumps (`pg_dump`) | none found, no schedule |
| Documented/tested restore | none |

**In the IaC:** `infra/__main__.py:475-494` creates the server with no `backup`
block at all, so every value above is an Azure default rather than a decision.

## Risk assessment

| # | Scenario | Protected today? | Consequence |
|---|---|---|---|
| 1 | Server or resource group deleted | **No** | Total, permanent loss of DB *and* backups. No lock, no external copy. |
| 2 | Bad migration / bad bulk update, caught within 7 days | Yes | PITR restore. Untested — RTO unknown. |
| 3 | Data corruption discovered after 7 days | **No** | Unrecoverable. |
| 4 | Azure region outage | **No** | LRS storage + non-geo backups. Down until the region returns; lost if it doesn't. |
| 5 | Media file deleted or overwritten | **No** | No soft delete, no versioning. Gone. |
| 6 | Ransomware / credential compromise | **No** | Attacker with subscription access can delete server and backups together. |
| 7 | Need a dev copy of prod data | Partial | No dump pipeline; ad-hoc firewall rules instead. |

Scenarios 1, 3, 5 and 6 all have the same root cause: **every copy of the data
lives inside one Azure resource group, in one region, with nothing preventing
its deletion.**

## Target design

Three independent layers, because each covers what the others cannot:

1. **Prevention** — resource locks so deletion cannot happen by accident.
2. **In-platform recovery** — Azure PITR at maximum retention, plus blob soft
   delete and versioning. Covers operator error, fast, no extra machinery.
3. **Out-of-platform recovery** — a nightly `pg_dump` plus a media copy written
   to storage in a *different* resource group with an immutability policy.
   This is the only layer that survives scenario 1 or 6, and the only one that
   gives a portable artifact for dev/staging refreshes.

## EXECUTED 2026-07-30

Done, verified against `az`:

| change | prod | staging |
|---|---|---|
| PITR retention 7 → 35 days | ✅ | ✅ |
| Blob soft delete | ✅ 30 d | ✅ 14 d |
| Container soft delete | ✅ 30 d | ✅ 14 d |
| Blob versioning + change feed | ✅ | ✅ (versioning) |
| Backup vault, separate RG | ✅ `aitutor-backup-vault` in `aitutor-backup-rg` | ❌ not yet |
| Weekly full, 1-year retention | ✅ policy `aitutor-pg-weekly-1y` | ❌ |
| Server attached to vault | ✅ `aitutor-pixel-pg` | ❌ |

**Retention is a recovery window, not a data lifetime.** Worth stating plainly
because it caused real alarm when first reported: the live database keeps every
row forever. The 7-day setting never deleted anything — it meant a mistake had
to be *noticed* within 7 days to be reversible. At 35 days there are five weeks
to catch it.

**Gotchas hit, for whoever repeats this:**

1. The vault's managed identity needs **two** grants, not one:
   `PostgreSQL Flexible Server Long Term Retention Backup Role` on the server
   *and* `Reader` on the resource group. With only the first, attaching fails
   with `AuthorizationFailed` on
   `Microsoft.Resources/subscriptions/resourcegroups/read` — an error that
   names the RG, not the missing role, so it reads like the wrong scope.
2. `az dataprotection backup-policy create` does **not** overwrite an existing
   policy of the same name, and does not say so. Delete first.
3. RBAC propagation is not instant; `validate-for-backup` is the cheap way to
   poll for it rather than retrying the create.

Still NOT done — see Phase 1 step 1 and Phase 2:
resource locks, staging vault, and **a tested restore**.

## Phase 1 — today, reversible, no downtime

Every command here is additive and can be undone. None interrupts the running
app. Ordered by risk-reduction per minute of work.

**1. Lock the production resource groups (5 min, free).** The single highest-
value change. `CanNotDelete` still permits normal writes; it blocks deletion of
the group and everything in it.

```bash
az lock create --name prod-no-delete --lock-type CanNotDelete \
  --resource-group aitutor-pixel-rg \
  --notes "Student data. Remove deliberately, never to unblock a deploy."
az lock create --name staging-no-delete --lock-type CanNotDelete \
  --resource-group aitutor-staging-rg
```

Caveat to plan around: a lock makes `pulumi destroy` and some `pulumi up`
replacements fail. That is the point — but it means anyone doing intentional
infra work must remove the lock first, consciously. Note it in `infra/README`.

**2. Raise PITR retention 7 → 35 days (2 min, minor cost).** Turns scenario 3
from "unrecoverable" into "recoverable for five weeks". Backup storage is free
up to 100% of provisioned storage (32 GB); beyond that it is a few dollars a
month at this data volume.

```bash
az postgres flexible-server update -g aitutor-pixel-rg -n aitutor-pixel-pg \
  --backup-retention 35
az postgres flexible-server update -g aitutor-staging-rg -n aitutor-staging-pg \
  --backup-retention 35
```

**3. Enable blob soft delete + versioning (5 min, minor cost).** Media
currently has zero protection against deletion or overwrite.

```bash
az storage account blob-service-properties update \
  --account-name aitutorpixelsa --resource-group aitutor-pixel-rg \
  --enable-delete-retention true   --delete-retention-days 30 \
  --enable-container-delete-retention true --container-delete-retention-days 30 \
  --enable-versioning true --enable-change-feed true
```

Note this protects the **blob container** only. The SMB file share `media` is a
different service and needs share snapshots (Phase 2) — and the app currently
uses both, per `memory/blob_media_hosting_plan.md`, which is mid-migration.
Confirm which one prod actually serves from before relying on this.

**4. Take one manual dump today, off Azure (30 min).** Proof that a portable
copy can be produced at all, and an immediate floor under scenario 1. Needs a
temporary firewall rule for the current IP — the existing `allow-local` rule
points at 108.31.132.119, which is stale.

```bash
MYIP=$(curl -s https://api.ipify.org)
az postgres flexible-server firewall-rule create -g aitutor-pixel-rg \
  -n aitutor-pixel-pg --rule-name tmp-dump --start-ip-address $MYIP --end-ip-address $MYIP
pg_dump "$PROD_DATABASE_URL" -Fc -f aitutor-prod-$(date +%F).dump
az postgres flexible-server firewall-rule delete -g aitutor-pixel-rg \
  -n aitutor-pixel-pg --rule-name tmp-dump --yes    # always remove it
```

Store the dump encrypted and off the laptop. It contains student PII — treat it
under the pilot consent scope, not as a casual file.

## Phase 2 — this week

**5. Automated nightly logical backup.** A container-apps job (or GitHub Action
with an OIDC federated credential) that runs `pg_dump -Fc`, writes to a storage
account in a **separate resource group**, and prunes to a retention schedule
(e.g. 14 daily, 8 weekly, 12 monthly). Separate RG is the whole point: it must
not die with the app's RG.

**6. Immutability on the backup container.** A time-based retention policy so
the dumps cannot be deleted or altered inside their window, even with
subscription credentials. This is what actually covers scenario 6.

**7. File-share snapshots** for the SMB `media` share, if prod still serves
from it.

**8. Restore drill — the item most likely to be skipped, and the one that
makes the rest real.** Restore the latest dump into a scratch server, run
`manage.py migrate --check`, and confirm row counts for `TutorSession`,
`SessionTurn`, `StudentCompetencyRecord`, `CurriculumChunk`. Record the wall
time; that number is the real RTO. An untested backup is a hypothesis.

**9. Codify in Pulumi.** Fold retention and lock settings into
`infra/__main__.py` so a future `pulumi up` cannot quietly revert them. Note
that `pulumi up` against a locked RG needs the lock lifted first.

## Phase 3 — needs a maintenance window

**10. Geo-redundant backup requires a new server.** Verified 2026-07-30:
`az postgres flexible-server update` has **no** `--geo-redundant-backup` flag;
only `create` does. Geo-redundancy therefore cannot be switched on for
`aitutor-pixel-pg` — it needs a new server created with it enabled and a
migration (dump/restore, or a replica cutover), with the app pointed at the new
host. Schedule deliberately; do not attempt alongside other changes.

**11. Consider zone-redundant HA** at the same time. Doubles compute cost and
needs a General Purpose SKU (B-series burstable does not support HA), so it is
a real budget decision, not a checkbox.

## Out of scope

- Anything touching the other subscriptions' databases (`beemonitor-pg`,
  `pan-apa-workshop-db`, `psql-ecomorph-dev`) — same weak posture, different
  projects, not this plan's call.
- Application-level soft delete. `memory/soft_delete_architecture_plan.md`
  already owns "never hard-delete student data" and is unimplemented; it is a
  complement to this, not a substitute.
- GDPR/consent handling for the dumps beyond "encrypt and don't leave them on a
  laptop". `apps/safety` owns consent.

## Cost

Phase 1 is effectively free: locks cost nothing, extra PITR is within the free
allowance at 32 GB, blob soft delete/versioning bills only for retained deleted
data. Phase 2 adds one small storage account plus job minutes — single-digit
dollars monthly. Phase 3 (geo-redundancy, HA) is the only material spend, since
HA needs a non-burstable SKU.

## Open questions

1. **Where should off-Azure dumps live?** Recommend a dedicated storage account
   in a new `aitutor-backup-rg`, same subscription, with immutability. Reason:
   separate blast radius without adding a second cloud vendor. A second
   provider is stronger against subscription-wide compromise but adds
   credentials and egress to manage.
2. **Retention schedule?** Recommend 14 daily / 8 weekly / 12 monthly. Reason:
   covers a full school term, and monthly checkpoints let you answer "what did
   this student's record look like at the start of term".
3. **Is prod serving media from the blob container or the SMB share?**
   `memory/blob_media_hosting_plan.md` is mid-migration; the answer changes
   which protection in step 3/7 actually matters. Must be checked before
   trusting either.
4. **Who else has subscription write access?** Locks stop accidents, not a
   determined authorised user. Worth an RBAC review alongside this.

## Next step

Run Phase 1 step 1 — the two `az lock create` commands. It takes five minutes,
costs nothing, is trivially reversible, and closes the one gap that is
currently unrecoverable.

Refs: memory/soft_delete_architecture_plan.md, memory/blob_media_hosting_plan.md,
auto-memory/deployment_pixel_prod.md, auto-memory/staging_mozambique_env.md
