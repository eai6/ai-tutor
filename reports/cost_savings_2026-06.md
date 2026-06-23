# AI Tutor — Azure Cost Savings Report
**Prepared:** 2026-06-10 · **Subscription:** Pixel Design Labs LLC · **Scope:** all AI Tutor environments

---

## 1. Executive summary

A cost-optimization sweep on 2026-06-10 reduced the platform's monthly Azure run-rate from a
**May 2026 actual of $1,131.65** to a **projected ~$575/month** — a **~$557/month (~49%) reduction** —
*while also adding ~$334/month of brand-new security infrastructure* (Application Gateway + WAF +
static IP) that did not exist in May.

Excluding that new security investment, the underlying optimization removed roughly **$890/month** of
waste and over-provisioning.

| | Monthly |
|---|---|
| **May 2026 (actual, all environments)** | **$1,131.65** |
| **June 2026 projection (post-optimization)** | **~$575** |
| **Net monthly savings** | **~$557 (~49%)** |
| — of which new security (WAF + static IP) *added* | +$334 |
| **Gross optimization (waste removed)** | **~$890** |

> **Note on estimates.** May figures are *actual* billed costs. June figures are *projections* of the
> post-change run-rate. Two line items settle over the coming days: (a) **Container Apps on the new
> Consumption plan** — actual usage-based billing confirms over ~2 weeks; (b) **Container Registry** —
> image layers were deleted but Azure's storage garbage collection reclaims them within ~24h, after
> which the line drops. Both are conservatively estimated here.

---

## 2. Production — `aitutor-pixel-rg` (Seychelles, live)

Itemized per resource, May actual → June projection:

| Resource | Type | May 2026 (actual) | June (projected) | Change |
|---|---|---:|---:|---|
| Container Apps environment | Compute (was **Dedicated D4**, now **Consumption min-1**) | $780.94 | **~$150** | ⬇️ −$631 |
| Container Registry (`aitutorpixelacr`) | ACR Basic (was 2.3 TB of un-pruned images) | $181.25 | **~$5** | ⬇️ −$176 |
| **Application Gateway + WAF_v2** | Network security (**NEW**) | $0.00 | **~$330** | ⬆️ +$330 (security) |
| PostgreSQL Flexible Server | Database | $18.45 | ~$16 | ⬇️ −$2 |
| **Static Public IP** | Network (**NEW**) | $0.00 | ~$4 | ⬆️ +$4 (security) |
| Storage / File Share | Storage (wiped 9 GB → 40 MB) | $0.52 | ~$0.50 | ⬇️ — |
| Communication (email) | Email | $0.01 | ~$0.01 | — |
| **Production total** | | **$981.18** | **~$505** | **⬇️ −$476 (−49%)** |

**What changed in production:**
- **Compute:** moved off the always-on Dedicated **D4** node (4 vCPU/16 GiB billed 24/7) to the
  serverless **Consumption** plan at 1 warm replica (4 vCPU/8 GiB). Kept warm because the WAF health
  probe polls every 30 s — so no cold starts for users.
- **Registry:** pruned **481 image tags / 463 manifests** (kept the newest 20), reclaiming ~2.3 TB —
  back under the 10 GB Basic allowance (overage was the entire ACR bill).
- **Security investment (new this month):** Application Gateway **WAF_v2** in Prevention mode + a
  dedicated **static public IP** for school allow-listing, behind a fully private VNet. This is the
  single largest line now (~$330/mo) — a deliberate trade for the pen-testing/security posture.
- **Storage:** media migrated to Azure Blob; the SMB File Share wiped from 9 GB to 40 MB.

---

## 3. Development / Staging — `aitutor-staging-rg` (Mozambique pilot)

> This is the environment the **`dev` branch** deploys to (via `deploy-staging.yml`). It is the active
> Mozambique (ExplicadorMoz) pilot environment. May costs were near-zero because the pilot had not yet
> ramped; it grew through June, so the meaningful comparison is **June pre-optimization pace → projection**.

| Resource | Type | May (actual) | June pre-opt (pace) | June projected (post-opt) |
|---|---|---:|---:|---:|
| Container Apps | Compute (Consumption; now **cron-warm + scale-to-zero**) | $5.95 | ~$80 | **~$20** |
| PostgreSQL | Database (**B2ms → B1ms**) | $3.79 | ~$75 | **~$18** |
| Container Registry (`aitutorstagingacr`) | ACR Basic (pruned 76 → 20 tags) | $3.45 | ~$30 | **~$5** |
| Storage | Storage | $0.17 | ~$0.70 | ~$0.70 |
| **Staging total** | | **$13.37** | **~$186** | **~$44** |

**What changed in development/staging:**
- **Compute:** cron-warm schedule — 1 warm replica **Mon–Fri 06:00–18:00 (Africa/Maputo)**, scales to
  **zero** evenings/weekends. No App Gateway in front, so it genuinely idles to $0 off-hours (verified:
  ~35 s cold-start activation on first request).
- **Database:** **B2ms → B1ms** (~$75 → ~$18). ⚠️ *Watch item* — see §6.
- **Registry:** pruned to the newest 20 tags.
- Net: from a ~$186/mo June pace down to **~$44/mo** (~76% off the unoptimized pace).

---

## 4. Decommissioned environments (deleted 2026-06-10)

An audit found three environments costing money with no role in the deployment pipeline. All were
deleted (resources + orphaned Pulumi stacks).

| Environment | What it was | May (actual) | Now |
|---|---|---:|---:|
| `aitutor-pixeldev-rg` | Orphaned "dev" infra (always-on D4, no CI deploys) | $111.72 | **$0** |
| `aitutor-rg` | Original pre-Container-Apps deploy (App Service + Azure SQL) | $18.38 | **$0** |
| `aitutor-preview-rg` | Preview infra, no CI | $7.00 | **$0** |
| **Decommissioned total** | | **$137.10** | **$0** |

> The `aitutor-pixeldev-rg` name collided with the `dev` *branch* (which actually deploys to staging),
> causing confusion. Deployment topology is now unambiguous: **`main` → production (Seychelles)**,
> **`dev` branch → staging (Mozambique)**. Only these two environments remain.

---

## 5. Total roll-up

| Environment | May 2026 (actual) | June 2026 (projected) |
|---|---:|---:|
| Production (Seychelles) | $981.18 | ~$505 |
| Development/Staging (Mozambique) | $13.37 | ~$44 |
| Decommissioned (dev/preview/legacy) | $137.10 | $0 |
| **TOTAL** | **$1,131.65** | **~$549** |

**Net monthly savings ≈ $583 (~51%)** — after absorbing ~$334/mo of new WAF/static-IP security.

*(The §1 headline uses ~$575 as a rounded, conservative total to allow for Consumption metering
settling; this table uses point estimates. Treat the true June figure as landing in the ~$550–600
range, confirmed once a full post-change billing cycle completes.)*

---

## 6. Watch items & follow-ups

- **Staging Postgres B1ms (revert risk).** B1ms is the cost choice; **B2ms was originally a manual bump
  because B1ms throughput was too low for content-generation / bursty operations.** If Mozambique
  content generation slows or times out, revert:
  `az postgres flexible-server update -g aitutor-staging-rg -n aitutor-staging-pg --sku-name Standard_B2ms --yes`
- **ACR garbage collection.** Image manifests are deleted; Azure reclaims the layer storage within ~24h,
  after which both registry lines actually drop to ~$5/mo. Add a recurring `--keep 20` purge to the
  deploy workflows so tags don't re-accumulate (the 2.3 TB built up over ~4 months of un-pruned builds).
- **WAF run-rate.** The Application Gateway WAF_v2 (~$330/mo) is currently under-reported in
  Cost Management due to recency lag; it will appear in full within ~48h. It is the platform's largest
  line by design (security/static-IP for school allow-listing).
- **Consumption metering.** Production + staging compute now bill per-use; the exact figures confirm
  after a full June cycle. Estimates here are deliberately conservative.
- **Pulumi reconciliation (staging).** The staging cron-warm + scale-to-zero were applied live via `az`;
  to make them durable against a future `pulumi up`, the `staging` stack config + `infra/__main__.py`
  should encode `min-replicas=0` and the cron rule.

---

*Source: Azure Cost Management (Actual Cost) for resource groups `aitutor-pixel-rg` and
`aitutor-staging-rg`; May = 2026-05-01→05-31 actuals, June = post-optimization projections.
Optimization detail: `memory/cost_reduction_plan.md`.*
