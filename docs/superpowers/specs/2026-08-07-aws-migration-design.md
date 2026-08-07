# AWS Migration Design — Azure Container Apps → ECS Fargate

**Date:** 2026-08-07
**Branch:** `aws_deployment`
**Status:** Design — awaiting approval

## 1. Decisions taken

| Decision | Choice | Consequence |
| --- | --- | --- |
| Region | `us-east-1` | Cheapest region, full service coverage, and ACM certs live here already if CloudFront is added later. Latency to the Seychelles and Tanzania pilots is the trade accepted. |
| Media storage | S3, with the storage backend rewritten | `apps/media_library/blob_media.py` is rewritten against S3 rather than lifted onto EFS. Removes the shared-mount dependency between web and material-processing tasks. |
| Scope | Full parity in one cut | ECS, RDS, S3, ALB, ACM, AWS WAF, SES, the material-processing job, and a staging environment all land before cutover. No dual-cloud period. |
| DNS | Stays at name.com | One ACM validation CNAME added by hand; a CNAME points `www.seselai.sc` at the ALB. ACM still auto-renews, so `cert-renew.yml` is deleted regardless. |

## 2. What exists on Azure today

Established by reading `infra/__main__.py` (1180 lines), `config/settings.py`, and the three deploy workflows in full.

- **Container App** `aitutor-pixel-app`, 4 vCPU / 8 GiB, 1–4 replicas, KEDA HTTP scaling at 12 concurrent requests.
- **PostgreSQL Flexible Server 16**, `Standard_B1ms` (1 vCore / 2 GiB), 32 GB, public with the `AllowAzureServices` sentinel as the only network control. pgvector is created by a Django migration, not by infrastructure.
- **Azure Files SMB share** mounted at `/app/media`, 100 GiB, shared by the app and the material job.
- **Application Gateway WAF_v2** with OWASP 3.2 in Prevention mode, terminating TLS for `www.seselai.sc` using a Let's Encrypt certificate that `cert-renew.yml` renews weekly through acme.sh and name.com DNS-01, storing it in Key Vault.
- **Azure Communication Services** for transactional email.
- **Container Apps Job** for large material processing, dispatched at runtime by the app's managed identity through ARM.
- **ACR** (Basic) plus Log Analytics.

Three findings that shape this design:

1. **The ChromaDB problem is gone.** Vectors live in Postgres via pgvector. `VECTORDB_ROOT=/tmp/vectordb` is a dead environment variable and must not be carried over.
2. **Azure is on the request path in application code**, not just in infrastructure — `blob_media.py`, `email_backends.py`, and `job_dispatch.py` all import Azure SDKs. This migration is a code change, not only a config change.
3. **The Dockerfile `CMD` runs `migrate` plus six seed commands before Gunicorn.** Container Apps single-revision mode serialises this by accident. On ECS with more than one task it is a migration race, so it moves to a one-shot task.

## 3. Target architecture

```text
                    name.com DNS
                         │  CNAME www.seselai.sc
                         ▼
              ┌──────────────────────┐
              │  AWS WAF v2 Web ACL  │  managed rule groups
              └──────────┬───────────┘
                         │ associated
              ┌──────────▼───────────┐
              │   ALB (public subnets)│  :443 ACM cert, :80 → 301
              │   idle timeout 120s   │
              └──────────┬───────────┘
                         │ HTTP :8000, SG-to-SG only
   ┌─────────────────────▼──────────────────────┐
   │  ECS Fargate service (private subnets)      │
   │  4 vCPU / 8 GiB · 1–4 tasks · amd64         │
   │  gunicorn 4 workers × 4 threads · 120s      │
   └───┬─────────────┬──────────────┬────────────┘
       │             │              │ ecs:RunTask
       ▼             ▼              ▼
 ┌──────────┐  ┌──────────┐  ┌──────────────────┐
 │   RDS    │  │    S3    │  │ material task    │
 │  PG 16   │  │  media   │  │ 2 vCPU / 4 GiB   │
 │ pgvector │  │ private  │  │ (same image)     │
 └──────────┘  └──────────┘  └──────────────────┘
       ▲             ▲
       └─────────────┴── Secrets Manager, CloudWatch Logs, SES
```

### Networking

A new VPC at `10.30.0.0/16` — deliberately not `10.20.0.0/16`, which the Azure VNet uses, so the two can coexist during cutover. Two public subnets carry the ALB and a single NAT Gateway; two private subnets carry the ECS tasks and RDS. One NAT Gateway rather than one per availability zone: it is a single point of failure for egress, but it halves the standing cost and matches the reliability the pilot needs today.

An **S3 gateway endpoint** is included because it is free and media traffic would otherwise be billed as NAT data processing. Interface endpoints for ECR, Secrets Manager, and CloudWatch Logs are deliberately omitted — at roughly $7 per month each they cost more than the NAT traffic they would displace at this scale.

Security groups enforce a strict chain: the ALB accepts 443 and 80 from anywhere; ECS tasks accept 8000 **only** from the ALB security group; RDS accepts 5432 **only** from the ECS task security group. The middle rule is load-bearing for security, not just tidiness — `apps/safety/client_ip.py` trusts the last `X-Forwarded-For` hop, which is only safe because nothing can reach the application except through the load balancer. That property must hold on AWS exactly as it does on Azure.

### Compute

ECS Fargate, 4 vCPU / 8 GiB, `X86_64` to match the current build. The 8 GiB is a floor, not a preference: `EMBEDDING_BACKEND=local` loads all-MiniLM-L6-v2 at roughly 500 MB into each of Gunicorn's four workers, and the staging stack documents a crash loop when this ran on 4 GiB.

Scaling is the one place with no clean equivalent. Azure scales on 12 concurrent requests; ECS has no concurrency metric. Two policies together approximate it:

- Target tracking on `ALBRequestCountPerTarget`, starting at 150 requests per target per minute. Twelve concurrent requests at the multi-second latencies typical of LLM calls works out near this figure, but it is a rate standing in for a concurrency limit and **will need calibration against real traffic**.
- Target tracking on ECS service CPU at 70% as a backstop for the case where requests are slow rather than numerous.

Minimum 1 task, maximum 4, matching Azure. Note that neither platform actually scales to zero today.

Three task definitions share one image:

| Family | Size | Command | Purpose |
| --- | --- | --- | --- |
| `aitutor-<stack>-web` | 4 vCPU / 8 GiB | `gunicorn …` | The service |
| `aitutor-<stack>-migrate` | 2 vCPU / 4 GiB | `migrate` + six seed commands | One-shot, run by CI before the service updates |
| `aitutor-<stack>-material` | 2 vCPU / 4 GiB | `process_material` | Run on demand by the app |

### Edge and TLS

An ACM certificate covering `www.seselai.sc`, and `ai-tutor.wbg.edwardamoah.com` as a subject alternative name **if that zone is reachable**. The second name sits in `CSRF_TRUSTED_ORIGINS` today but has no working TLS path on Azure, because the App Gateway listener hardcodes only `www.seselai.sc`. Including it fixes that — but ACM validates each name independently, and `wbg.edwardamoah.com` is a different zone that may not be under the same control as `seselai.sc`. **Open question:** if a validation CNAME cannot be added there, drop the name from the certificate and from `CSRF_TRUSTED_ORIGINS` rather than shipping a certificate that never issues.

Because DNS stays at name.com, certificate issuance requires adding ACM's validation CNAME records by hand before the HTTPS listener will come up. This is a blocking manual step, and Pulumi will export the exact records to add.

**The ALB idle timeout must be raised to 120 seconds.** The default is 60, and Gunicorn's timeout is 120 — leaving the default would sever long LLM requests at the load balancer with a 504 while the worker keeps running. Target group deregistration delay is likewise set to 120 seconds so in-flight tutoring turns drain rather than drop during a deploy.

Health checks hit `/health/` with matcher 200, interval 30s, timeout 10s, 2 healthy / 3 unhealthy. Worth recording: `/health/` calls `connection.ensure_connection()` and returns 503 when the database is unreachable, so a database problem marks every target unhealthy simultaneously. Azure had the same coupling and it caused false 503s when the B1ms instance exhausted its 50-connection limit. Moving to `db.t4g.medium` raises the ceiling to roughly 450 connections, which should remove the trigger. Splitting liveness from readiness is a sensible follow-up but is out of scope here, since it changes behaviour rather than porting it.

### WAF

An AWS WAF v2 Web ACL on the ALB with three managed rule groups: `AWSManagedRulesCommonRuleSet` (the closest analogue to OWASP CRS 3.2), `AWSManagedRulesKnownBadInputsRuleSet`, and `AWSManagedRulesSQLiRuleSet`.

Two deliberate differences from Azure:

- **Deploy in `COUNT` mode, then flip to `BLOCK`.** Azure runs Prevention mode today, so this is briefly weaker. The reason is that the managed common rule set produces false positives against rich-text teacher dashboard input, and discovering that by blocking real teachers during a cutover is the wrong way to find out. The flip to `BLOCK` is a tracked follow-up, not an optional one.
- **Body inspection differs.** Azure inspects 128 KB of request body; AWS WAF on an ALB inspects 8 KB by default, raisable to 64 KB. Body rules are configured with `oversizeHandling: CONTINUE` so that large curriculum uploads pass rather than being rejected outright.

### Database

RDS PostgreSQL 16, `db.t4g.medium` (2 vCPU / 4 GiB), 50 GB gp3 with storage autoscaling to 200 GB, single-AZ, 14-day automated backups. This is deliberately a size up from Azure's `Standard_B1ms`, which is a documented source of production incidents at its 50-connection ceiling. Single-AZ matches Azure, which has no high availability configured either; Multi-AZ roughly doubles the instance cost and is the obvious first upgrade if the pilot grows.

pgvector needs no parameter-group work — RDS ships `vector` in its available extensions, and the existing migration `apps/curriculum/migrations/0029_curriculumchunk.py` creates it. Migrations run as the master user, which holds `rds_superuser`, so `CREATE EXTENSION` succeeds.

The connection string lands in Secrets Manager as a complete DSN and **must retain `?sslmode=require`**. Nothing in `config/settings.py` enforces TLS; it comes entirely from that query parameter, so dropping it during the port would silently downgrade every connection to plaintext.

### Media on S3

A private bucket with public access blocked, SSE-S3 encryption, and a lifecycle rule expiring incomplete multipart uploads. The ECS task role gets `GetObject`, `PutObject`, `DeleteObject`, and `ListBucket` scoped to that bucket alone.

Media continues to be **served through Django** at `/media/<path>`, not through presigned URLs or a CloudFront distribution. This looks inefficient and is intentional: the existing design keeps media on the application's own domain so that school networks only need one hostname allowlisted. Presigned S3 URLs would break that, so `serve_media` is rewritten to stream from S3 with Range support, mirroring the Azure implementation it replaces.

### Email on SES

A verified domain identity with DKIM, matching the current Azure sender domain (`mail.ai-tutor.wbg.edwardamoah.com` today, derived from the first entry in `custom-domains`). Because DNS stays at name.com, the DKIM CNAMEs, the MAIL FROM records, and DMARC are all added manually; Pulumi exports them.

The backend is a `SESEmailBackend` built on boto3's SESv2 client rather than the `django-ses` package. Three reasons: boto3 is already a dependency for S3 and ECS dispatch, so this adds nothing new; the existing class already encodes `fail_silently` semantics, HTML alternatives, reply-to handling, and display-name stripping that are worth keeping; and a fake boto3 client makes the outgoing payload directly assertable, where mocking a third-party backend's internals would be brittle. Messages carrying attachments are sent as SES `Raw` content, since `Simple` content has no attachment field.

**SES starts every new account in sandbox mode**, where sending is restricted to verified addresses. Production access must be requested early — approval is usually under a day but is not guaranteed, and it gates real email at cutover.

### Material-processing job

`apps/dashboard/job_dispatch.py` is rewritten from `azure.mgmt.appcontainers` to `boto3` `ecs.run_task`. The web task role gets `ecs:RunTask` plus `iam:PassRole`, scoped to the material task definition and its two roles.

Moving media to S3 removes the shared-mount requirement entirely: web and material tasks both reach the same bucket, so the job no longer needs a filesystem in common with the service. This also resolves the standing assumption in `reset_lost_materials` that source PDFs persist on the Azure Files mount.

### Secrets, logging, registry

Six secrets move from Pulumi encrypted config into Secrets Manager — `django-secret-key`, the four provider API keys, and the database DSN — and are injected into task definitions as `secrets` resolved by the execution role.

**`SECRET_KEY` must be carried across unchanged.** Mobile refresh tokens are HS256-signed with it on a 30-day lifetime, so rotating it during the migration logs out every mobile user.

CloudWatch Logs via the `awslogs` driver, log group `/ecs/aitutor-<stack>`, 30-day retention to match Log Analytics. Note that the `apps` logger runs at DEBUG in production, which is a real ingestion cost at $0.50/GB; making the level environment-driven is a cheap follow-up.

ECR with `scanOnPush` and a lifecycle policy retaining 20 images. That policy replaces the `acr purge` CI step outright — the Azure registry reached 2.3 TB and roughly $180/month before manual pruning was added, and a declarative lifecycle rule removes the failure mode rather than scripting around it.

## 4. Application code changes

| File | Change |
| --- | --- |
| `apps/media_library/blob_media.py` | `AzureStorage` → `S3Storage`; `serve_media` rewritten to stream from S3 with Range support |
| `apps/safety/email_backends.py` | `AzureCommunicationEmailBackend` → `SESEmailBackend` on boto3 SESv2, keeping the class shape |
| `apps/dashboard/job_dispatch.py` | `azure.mgmt.appcontainers` → `boto3` `ecs.run_task` |
| `apps/safety/client_ip.py` | Docstring only — the last-hop logic is correct for ALB. ALB omits the `:port` suffix by default, which `_normalize_ip` already tolerates |
| `config/settings.py` | `AZURE_BLOB_MEDIA_*` → `AWS_MEDIA_*`; ACS variables → SES; Azure job variables → `ECS_*` |
| `config/urls.py` | Same route shape, pointing at the S3-backed `serve_media` |
| `Dockerfile` | `CMD` reduced to Gunicorn alone; the migrate-and-seed chain moves to the migrate task |
| `requirements*.txt` | Drop `azure-communication-email`, `azure-identity`, `azure-mgmt-appcontainers`, `django-storages[azure]`; add `django-storages[s3]`, `boto3`, `django-ses` |

`azure_openai` remains a valid **LLM provider** choice in `apps/llm/models.py`. That is a model-vendor decision, unrelated to hosting, and is left alone.

## 5. Infrastructure layout

A new Pulumi project at `infra/aws/` with stacks `prod` and `staging`, leaving the Azure program in `infra/` untouched until cutover completes.

```text
infra/aws/
  Pulumi.yaml            # name: aitutor-aws
  Pulumi.prod.yaml
  Pulumi.staging.yaml
  __main__.py            # orchestration only
  components/
    network.py           # VPC, subnets, NAT, endpoints, security groups
    data.py              # RDS, Secrets Manager
    storage.py           # S3 media bucket, IAM policy
    registry.py          # ECR, lifecycle policy
    compute.py           # cluster, three task definitions, service, autoscaling
    edge.py              # ALB, ACM, listeners, WAF
    email.py             # SES identity, DKIM, exported DNS records
    iam.py               # task/execution roles, GitHub OIDC provider, deploy roles
```

The existing Azure program is a single 1180-line file, which is why porting it required a full read to answer basic questions. Splitting the AWS program by concern is a targeted improvement to the thing being rebuilt, not unrelated refactoring.

The Azure program's `ignore_changes` on container env and image encodes a real division — infrastructure owns the shape, CI owns the deploy. That division is preserved: Pulumi owns the service and roles, CI registers new task definition revisions.

## 6. CI/CD

`deploy.yml` and `deploy-staging.yml` are rewritten against AWS; `cert-renew.yml` is **deleted** because ACM auto-renews.

Authentication moves to **GitHub OIDC** with `aws-actions/configure-aws-credentials@v4` and separate prod and staging roles carrying branch trust conditions. This removes the long-lived `AZURE_CREDENTIALS` service-principal JSON, which today is shared across prod, staging, and Key Vault.

The deploy job gains the safety rail the Azure pipeline never had:

1. Checkout, free runner disk, assume role via OIDC, ECR login
2. Build with **buildx and `type=gha` cache** — the current pipeline has no layer caching at all, so every deploy is a cold build of a roughly 4.7 GB image
3. Push tagged with the commit SHA, plus `latest`, plus the version tag on `v*` refs
4. Register all three task definition revisions
5. **RunTask the migrate task and wait for exit code 0** — deploy stops here if migrations fail
6. `update-service`, then `aws ecs wait services-stable`
7. **Smoke test**: `curl -fsS https://<host>/health/` and assert the version field
8. **On failure, roll back** by re-pointing the service at the previous task definition revision

Two jobs are deleted rather than ported: `post_deploy_pgvector_port` in both workflows, which is a one-off Azure-era ChromaDB import that has already served its purpose. `post_deploy_eval` needs **no changes at all** — it runs against a fresh SQLite database on the runner and never touches the cloud.

`regenerate_math_content.yml` and `audit_math_content.yml` currently shell in with `az containerapp exec`. They become ECS `RunTask` invocations, which is strictly better: the Azure form needs an interactive TTY and is documented as awkward in CI.

## 7. Data migration and cutover

1. **Rehearse first.** `pg_dump` from Azure into a scratch RDS instance and run the full application against it before touching production. The dump carries the `vector` extension and the HNSW index; verify both survive.
2. **Sync media** from the Azure Files share and blob container to S3, then re-sync immediately before cutover to capture the delta.
3. **Lower DNS TTL at name.com** to 60 seconds at least 24 hours ahead.
4. **Add the ACM validation CNAME** and confirm the certificate issues. This gates everything and is manual.
5. **Request SES production access.** Not on the critical path for serving traffic, but it is for email.
6. **Cutover window:** stop writes on Azure, take a final `pg_dump`, restore to RDS, run the final media sync, flip the CNAME, watch.
7. **Rollback:** point the CNAME back at Azure. Keep the Azure stack running and intact until AWS has been stable for at least a week.

## 8. Cost

Rough monthly estimate for the production stack in `us-east-1`:

| Item | Estimate |
| --- | --- |
| Fargate, 1 task at 4 vCPU / 8 GiB, always on | $144 |
| ALB | $16 + LCU |
| RDS `db.t4g.medium`, single-AZ, 50 GB gp3 | $53 |
| NAT Gateway | $33 + data |
| WAF, 3 managed rule groups | $8 |
| S3, ECR, Secrets Manager, CloudWatch | $12 |
| **Baseline** | **≈ $270–330** |

This should come in below the current Azure spend, where App Gateway WAF_v2 alone runs roughly $180–250 per month.

Staging costs about $220 in the same shape. Two levers, both recommended: run staging tasks on **Fargate Spot** for roughly a 70% compute discount, and scale the service to zero outside working hours.

## 9. Risks

| Risk | Mitigation |
| --- | --- |
| ACM validation CNAME is manual and gates HTTPS | Do it first; Pulumi exports the exact records |
| SES sandbox blocks real email | Request production access at the start of the work, not at cutover |
| WAF managed rules false-positive on teacher dashboard input | Deploy in `COUNT`, observe, then flip to `BLOCK` |
| ALB 60s default idle timeout severs LLM requests | Explicitly set to 120s; covered by a smoke test that exercises a slow path |
| Rotating `SECRET_KEY` invalidates 30-day mobile JWTs | Carry the existing value across unchanged |
| Scaling metric is a rate standing in for concurrency | Ship with a CPU backstop policy and calibrate against real traffic |
| Migration race across ECS tasks | Migrations move out of `CMD` into a one-shot task that CI waits on |
| Dropping `?sslmode=require` silently disables TLS to the database | Assert it in the DSN construction and cover it with a test |

## 10. Out of scope

Deliberately excluded, and each would be its own piece of work:

- **CloudFront.** Media is intentionally served from the app's own domain for school-network allowlisting.
- **ElastiCache.** No `CACHES` setting exists, so Django uses per-process `LocMemCache` and DRF throttles are per-replica rather than global. Already true on Azure; not a migration concern.
- **RDS Proxy.** `CONN_MAX_AGE` is 0, so every request opens a new connection. Worth revisiting once real connection counts are visible.
- **Re-enabling SSE.** Streaming is disabled at three code sites purely because Azure buffers it. ALB does not, so this becomes possible — but it is a feature change, not a migration.
- **Graviton / arm64.** A one-line `runtimePlatform` change, but it requires revalidating the torch, faster-whisper, and Piper wheels.
- **Multi-AZ RDS and per-AZ NAT.** The obvious reliability upgrades once the pilot justifies the cost.
