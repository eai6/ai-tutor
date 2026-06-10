# Azure Cost Reduction — Plan (2026-06-09)

## Problem
Production (`aitutor-pixel-rg`, Pixel Design Labs sub) run-rate is climbing. May 2026
was **$981.18**; June forward run-rate ≈ **$1,250–1,300/mo** once the newly-added App
Gateway WAF_v2 starts metering. Goal: cut recurring spend without hurting the pilot.

## Current state (from audit, 2026-06-09)

### Bill — `aitutor-pixel-rg`
| Service | May 2026 | June MTD pace |
|---|---|---|
| Container Apps | $780.94 | ~$720/mo |
| Container Registry | $181.25 | ~$205/mo |
| PostgreSQL | $18.45 | ~$18/mo |
| Storage | $0.52 | <$1/mo (post-wipe) |
| App Gateway WAF_v2 | — (not yet existed) | **~$330/mo (not yet metered — ~24-48h lag)** |
| Static Public IP (Standard) | — | ~$4/mo |
| Email/Bandwidth/Logs | ~$0.01 | ~$0 |

### Infra facts
- **Container App** `aitutor-pixel-app`: workload profile **`dedicated-d4`** (D4 = 4 vCPU/16 GiB
  node, profile min 1 / max 2 nodes — **always-on, billed even at 1 replica**). App sized
  4 CPU/8Gi, scale min 1 / max 4, HTTP rule @ 12 concurrent. Currently 1 replica.
- **Environment `aitutor-pixel-env` ALREADY has a `Consumption` profile** alongside
  `dedicated-d4`. → migrating the app to Consumption needs **no env recreation**.
- **ACR** `aitutorpixelacr`: **Basic** SKU (10 GB included) storing **2,286 GB** across
  **502 `aitutor` tags** — every CI build since Feb, never pruned. Overage
  (2277 GB × $0.003/GB/day × 30) ≈ $205/mo = the whole ACR line.
- **App Gateway** `aitutor-pixel-appgw`: **WAF_v2**, Gen2, autoscale **min 1 / max 3 CU**
  (already at the floor). Static PIP `aitutor-pixel-appgw-pip` (Standard).
- **Material Job** `aitutor-pixel-material-job` uses image `aitutor:latest`; mounts `/app/media`.
- Embeddings: `EMBEDDING_BACKEND=local` (sentence-transformers all-MiniLM-L6-v2, ~500 MB/worker).
  Vectors themselves live in Postgres/pgvector now (not the local model's concern at query time).

## Levers, by impact

### 1. ACR prune — ~$180/mo — DONE 2026-06-09
Server-side `acr purge --filter 'aitutor:.*' --ago 0d --keep 20 --untagged`. Keeps newest
20 tags (covers `latest` + deployed `1d0ce8b…` + 18 rollback targets), deletes ~482 + dangling
manifests. Reclaims ~2.2 TB → ACR back under the 10 GB Basic allowance (~$5/mo).
**Prevent regrowth:** add a recurring purge (ACR scheduled task or a step in `deploy.yml`)
so CI builds don't re-accumulate. *(follow-up task — not yet done)*

### 2. Container Apps: dedicated-d4 → Consumption — ~$500–650/mo — BIGGEST LEVER (needs decision + test)
The D4 dedicated node is a 24/7 floor regardless of traffic. For an intermittent-use pilot
(lab sessions a few hours/few days a week) this is the wrong shape.

**Target:** move `aitutor-pixel-app` to the existing **Consumption** profile, replica sized
**4 vCPU / 8 GiB** (max Consumption allows — same as today, so no sentence-transformers OOM;
the old OOM was at 2 CPU/4 GiB on the deprecated dev plan).

Two sub-options for the idle floor:
- **(2a) Scale-to-zero** (`minReplicas: 0`). Max savings (~$50–120/mo). Cost: a **~30–60 s
  cold start** (Django + model load) for the first request after idle — bad for the first
  student of a session.
- **(2b, recommended) Cron-warmed** — KEDA `cron` scale rule keeping `minReplicas: 1` during
  school hours (e.g. 06:00–18:00 local, weekdays) and 0 otherwise. Warm during use, $0
  overnight/weekends. Est. ~$120–200/mo. Best UX/cost balance.

**Then remove the `dedicated-d4` profile** from the env (dedicated bills min-1-node even idle,
so it must be removed, not just unused) — and move the **material Job** to Consumption too.

**Risks / test before prod:** (a) cold-start duration — measure; (b) confirm no OOM at 4/8 under
real tutoring load; (c) Consumption per-replica cap is 4 vCPU/8 GiB — fine, we don't exceed;
(d) brief revision transition when switching profiles.

### 3. App Gateway WAF_v2 — ~$330/mo — KEEP (at its floor)
Already min 1 CU. The ~$0.44/hr gateway fee is fixed for WAF_v2; the only way down is dropping
WAF, which contradicts the pen-testing/max-security requirement. **Front Door Standard** (cheaper,
bundled managed WAF, adds CDN caching that would also offload Container Apps) was rejected earlier
because it gives an **anycast IP, not a dedicated static IP** the schools allow-list. Decision
stands: keep App Gateway WAF_v2. Revisit only if schools switch to **domain** allow-listing.

### 4. Storage — DONE
9 GB → 40 MB today (File Share wipe; data on Blob). Standard/TransactionOptimized bills on used
GB → already ~$0. Can't fully drop the share yet (upload pipeline + material Job mount it; see
`memory/blob_media_hosting_plan.md`).

### 5. (Off the Azure bill) LLM API spend — review separately
Anthropic/OpenAI/Google usage is billed by those vendors, not Azure, and is likely material:
tutoring = **Opus 4.7** (`memory/project_tutor_model_choice.md`), judges = Gemini, images = OpenAI.
Not in scope of this Azure plan, but the next-biggest cost line overall. Candidate: route
non-tutoring purposes to cheaper models; measure against the eval benchmark first.

## Projected run-rate after changes
| | Now (fwd) | After ACR+CApps(2b) |
|---|---|---|
| Container Apps | ~$720 | ~$150 |
| Container Registry | ~$205 | ~$5 |
| App Gateway WAF_v2 | ~$330 | ~$330 |
| Postgres + IP + misc | ~$25 | ~$25 |
| **Total** | **~$1,280** | **~$515** (~60% cut) |

## Decisions (confirmed 2026-06-09)
1. **Move app to Consumption profile, `minReplicas: 1` (always warm).** Sized 4 vCPU/8 GiB.
2. **Why not cron scale-to-zero (user's first pick):** App Gateway probe `acaProbe` hits
   `/health/` **every 30 s** (interval 30, timeout 30, unhealthy threshold 3). Continuous probe
   traffic never lets Container Apps reach its no-traffic scale-to-zero cooldown (~5 min), so the
   app would stay at 1 replica 24/7 regardless of a cron rule — cron-to-zero is moot behind the
   gateway. min-1 gives the same ~$520/mo saving, zero cold starts, no probe/health conflict.
   (True scale-to-zero only pays off with no gateway in front — conflicts with the static-IP/WAF
   requirement.) Keep the HTTP-concurrency rule (@12) for in-day load bursts.
3. **ACR prune DONE** (481 tags / 463 manifests deleted, kept newest 21; GC reclaims size over
   hours). **Recurring purge** — add a `--keep 20` step to `deploy.yml` (still TODO).

## Implementation order (Container Apps → Consumption)
Execute via **az** (surgical, reversible; avoids Pulumi env-replace risk since the Consumption
profile already exists in `aitutor-pixel-env`). Reconcile Pulumi after.
1. `az containerapp update -n aitutor-pixel-app -g RG --workload-profile-name Consumption
   --min-replicas 1 --max-replicas 4` → new revision on Consumption. **Verify:** App Gateway
   backend Healthy, home 200, a real tutoring turn works, no OOM at 4/8 under load. Reversible
   (set --workload-profile-name dedicated-d4 to roll back).
2. `az containerapp job update -n aitutor-pixel-material-job -g RG --workload-profile-name Consumption`.
3. **Cost step (do after #1 stable):** `az containerapp env workload-profile delete -g RG
   -n aitutor-pixel-env --workload-profile-name dedicated-d4` → stops the D4 node billing.
4. Update `infra/__main__.py` (config `workload-profile-type=Consumption`, `min-replicas=1`),
   `pulumi preview` to confirm no destructive diff (esp. NOT recreating the env), commit.

## Open questions
- None blocking.

## Next step
Confirm Q1/Q2, then: (1) test Consumption profile on a staging revision (measure cold start + OOM),
(2) Pulumi change app→Consumption + cron scale rule + Job→Consumption + remove dedicated-d4 profile,
(3) preview → apply in a maintenance window. ACR prune already done; add the recurring purge.
