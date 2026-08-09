# AI Tutor on AWS — platform, cost, and self-hosting

For a ministry, school network, or partner that wants to run AI Tutor in its own
AWS account, under its own control, with student data inside its own borders.

Everything here is measured against a real running deployment
(`migration.edwardamoah.com`, AWS account `968025288404`, `us-east-1`) as of
**2026-08-09**, not estimated from a diagram.

---

## 1. What actually runs

The platform is a single Django application in a container, a Postgres
database, and the network plumbing that puts one in front of the other safely.
There are no microservices and no message brokers — deliberately, because the
whole thing has to be operable by whoever is on duty, not by a platform team.

```
                    Internet
                        │
                   Route 53  (DNS)
                        │
                     AWS WAF  (firewall — rate limit + managed rules)
                        │
         Application Load Balancer  (TLS terminates here, ACM certificate)
                        │  HTTP, inside the VPC
                        ▼
     ┌──────────── ECS Fargate service ────────────┐
     │  Django + Gunicorn, 4 vCPU / 8 GB, 1 task    │
     │  private subnets, no public IP               │
     └───────┬──────────────────┬───────────────────┘
             │                  │
             ▼                  ▼
   RDS PostgreSQL 16      S3 (media)          ──►  NAT Gateway ──► Internet
   db.t4g.medium          private, served           (outbound only:
   50 GB gp3              through Django            LLM APIs, ECR)
   private, no public IP
```

### The pieces, and why each exists

| Resource | What it is | Why |
|---|---|---|
| **ECS Fargate** | Runs the container. 4 vCPU / 8 GB, 1 task, autoscales to 4. | No servers to patch. The image also carries a CPU-only PyTorch and two speech models, which is why the task is large. |
| **RDS PostgreSQL 16** | `db.t4g.medium`, 50 GB gp3, single-AZ, 14-day backups. | All platform data. **pgvector** lives here too, so the knowledge base is in Postgres rather than a separate vector database. |
| **Application Load Balancer** | Public entry point, TLS termination. | The only thing with a public address. |
| **AWS WAF** | Web ACL: rate limit (2000 req / 5 min per IP) + three AWS managed rule groups. | The only rate limit on unauthenticated traffic — the app's own limiter keys on user id and cannot see a request that has not logged in. |
| **NAT Gateway** | Outbound-only internet for the private subnets. | The app calls LLM APIs and pulls container images. Nothing can dial *in*. |
| **S3 — media** | Lesson images and figures. Private. | Served *through Django* at `/media/<path>`, never a bucket URL. |
| **S3 — ops** | Database dumps during restores. Private, 7-day expiry. | Separate from media on purpose: media is served over the internet, dumps must never be. |
| **S3 — downloads** | Desktop installers. **Public.** | The only intentionally world-readable bucket. |
| **Secrets Manager** | 6 secrets: DB URL, Django key, 4 LLM provider keys. | Injected at container start; never in the image or the repo. |
| **ECR** | Container registry, keeps the last 20 images. | Rollback targets. |
| **Route 53 + ACM** | DNS and the TLS certificate. | Certificate renewal is automatic *because* the validation record is managed here. |

### Deliberate omissions

- **No Multi-AZ database.** Halves the bill; costs you a restore-from-backup on
  an AZ failure. Reconsider once real classes depend on it.
- **No CloudFront.** Media is small and served through the app.
- **No Elasticache / queue / search cluster.** Background work runs in threads.
- **No bastion host.** Database access is via a one-off ECS task inside the VPC.

---

## 2. What it costs

Two things make published "AWS cost" figures misleading, and both apply here.

**First: the account's historical bill is not the platform's bill.** This
account also holds unrelated projects — a 1.2 TB bucket and a 279 GB bucket
belonging to other work. They account for nearly all of the $33.51/month S3
line. AI Tutor's own buckets total **~11 GB**.

**Second: the platform only came up this month.** May–July show $44–61/month,
but that was DNS, a domain registration and other projects' storage — no
database, no containers, no load balancer.

So the figures below are computed from the **measured resource sizes above** at
`us-east-1` list prices, and are what a country should budget.

### Fixed — you pay this whether or not anyone logs in

| Item | Basis | Monthly (USD) |
|---|---|---|
| ECS Fargate, 1 task | 4 vCPU + 8 GB, 730 h | **~144** |
| RDS `db.t4g.medium` | 730 h | **~47** |
| NAT Gateway | 730 h (before data charges) | **~33** |
| Application Load Balancer | 730 h (before LCU) | **~16** |
| AWS WAF | 1 web ACL + 4 rules | **~9** |
| RDS storage | 50 GB gp3 | **~6** |
| Secrets Manager | 6 secrets | **~2.40** |
| S3 storage | ~11 GB | **~0.30** |
| Route 53 | 1 hosted zone | **~0.50** |
| ECR | ~5 GB of images | **~0.50** |
| **Fixed total** | | **≈ $259 / month** |

### Variable — scales with use

| Item | Driver | Rough shape |
|---|---|---|
| **LLM API calls** | Tutoring turns | **Usually the largest line, and it is not an AWS charge.** Billed by Anthropic / OpenAI / Google. Budget this first. |
| NAT data processing | Outbound bytes (mostly LLM traffic) | ~$0.045/GB |
| ALB LCUs | Connections, new requests | Single digits at pilot scale |
| WAF requests | ~$0.60 per million | Cents at pilot scale |
| S3 requests + egress | Media views, installer downloads | Small; installers are ~280 MB each and the download bucket is public — a widely shared link is the one line that can surprise you |
| CloudWatch Logs | Log volume, 30-day retention | Single digits |
| Extra Fargate tasks | Autoscaling to 4 | +$144/month per additional task |

### Honest notes on the total

- **≈ $259/month is the floor** for this shape. About $58 of it (NAT + ALB +
  WAF) is *plumbing rather than capacity* — you pay it for a safe network and a
  firewall, not for the ability to serve one more student.
- **The tutoring bill lives outside AWS.** Model choice dominates: the platform
  routes per purpose (`llm.ModelConfig`), and a frontier tutoring model is
  worth more than the entire AWS bill at modest volume. Decide the model before
  the instance size.
- **Cheaper is available if you accept trade-offs.** Fargate at 2 vCPU / 4 GB
  halves the largest line; dropping the NAT gateway for VPC endpoints saves
  ~$33 with more configuration; a `db.t4g.small` saves ~$24.
- The **offline desktop build** exists precisely so classroom use does not
  depend on any of this.

---

## 3. Self-hosting: deploying to your own AWS account

### What you need first

- An AWS account, and an IAM user or role that can create VPC, ECS, RDS, S3,
  IAM, WAF and Route 53 resources.
- A domain you control (for HTTPS — a certificate cannot be issued for an
  AWS-owned load balancer hostname).
- At least one **LLM provider API key** (Anthropic, OpenAI or Google).
- Installed locally: `git`, `aws` CLI v2, `pulumi`, `docker`, Python 3.12.

### Step 1 — Clone and configure the stack

```bash
git clone <this repo> ai-tutor
cd ai-tutor/infra/aws

python -m venv venv && ./venv/bin/pip install -r requirements.txt

aws configure                 # or aws sso login
pulumi login --local          # state in ~/.pulumi; back this up (see below)
pulumi stack init prod
```

Set the configuration. Every key below is read by `infra/aws/__main__.py`:

```bash
pulumi config set aws:region eu-west-1          # pick your region
pulumi config set aitutor-aws:db-instance-class db.t4g.medium
pulumi config set aitutor-aws:db-storage-gb 50
pulumi config set aitutor-aws:min-tasks 1
pulumi config set aitutor-aws:max-tasks 4

# Database password. Set it yourself — do NOT let it be generated.
pulumi config set --secret aitutor-aws:db-password "<a long random password>"

# Application secrets
pulumi config set --secret aitutor-aws:django-secret-key "<50+ random chars>"
pulumi config set --secret aitutor-aws:anthropic-api-key "sk-ant-..."
pulumi config set --secret aitutor-aws:openai-api-key    "sk-..."
pulumi config set --secret aitutor-aws:google-api-key    "..."
```

> **Why you set the database password rather than generating it.** An earlier
> version used a generated random password. That works exactly once: when the
> Pulumi state was lost, the password could not be read back, and the next
> `pulumi up` wanted to *rotate the live database password* while the running
> container still held the old one. A value you control is a value you can
> recover. See the comment in `infra/aws/components/data.py`.

### Step 2 — Bring up the infrastructure

```bash
pulumi up                     # review the plan, then confirm
```

This creates everything except the ECS service — that is gated so the
foundation can be verified first. Then:

```bash
pulumi config set aitutor-aws:enable-ecs true
pulumi up
```

### Step 3 — Domain and HTTPS

Create a Route 53 hosted zone for your domain (or use an existing one), point
your registrar's nameservers at it, then:

```bash
pulumi config set aitutor-aws:domain-name tutor.education.gov.xx
pulumi config set aitutor-aws:hosted-zone-id Z0123456789ABCDEFGHIJ
pulumi up
```

Pulumi requests the certificate, writes the validation record, waits for
issuance, adds the HTTPS listener and redirects port 80. Expect it to sit
waiting during validation; if it times out nothing has changed and the site is
still served on port 80.

**Let Pulumi manage the validation record.** ACM re-validates on renewal using
that record. A hand-added one that someone later tidies up breaks the renewal
about eleven months later, long after anyone connects the two events.

### Step 4 — Build and deploy the application image

Either push to your fork's `main` and let `.github/workflows/deploy-aws.yml`
run it (set the repo secret `AWS_DEPLOY_ROLE_ARN` to a role trusting GitHub's
OIDC provider), or build by hand:

```bash
aws ecr get-login-password | docker login --username AWS --password-stdin <acct>.dkr.ecr.<region>.amazonaws.com
docker build --platform linux/amd64 -t <ecr-url>:v1 .    # amd64 matters on Apple Silicon
docker push <ecr-url>:v1
pulumi config set aitutor-aws:image-tag v1 && pulumi up
```

Then run migrations and seed, as a one-off task:

```bash
./ops/migrate_and_seed.sh
```

### Step 5 — Turn the firewall on

The WAF starts in **count mode** so you can see what it *would* block before it
blocks anything real:

```bash
pulumi config set aitutor-aws:waf-block-mode true
pulumi up
```

> **Check your uploads afterwards.** Two managed rules —
> `SizeRestrictions_BODY` (8 KB) and `CrossSiteScripting_BODY` — block ordinary
> file uploads: the first because any real file exceeds 8 KB, the second
> because compressed image bytes resemble XSS signatures. Both are already
> overridden to Count in `infra/aws/components/edge.py`, with the reasoning
> written there. If you re-enable them, uploads stop working across the whole
> product and the failure is a bare 403 that never reaches the application log.

### Step 6 — Verify

```bash
curl -s https://tutor.education.gov.xx/health/          # {"status": "ok", ...}
```

Then load the site, create a staff account, upload curriculum, and take one
tutoring turn. The turn is the real test: it exercises the database, the
knowledge base, the LLM credentials and media in one action.

---

## 4. Operating it

### Data residency

Everything student-related stays in the region you chose: RDS, S3, and the ECS
task. **The exception that matters: LLM API calls leave your region and your
cloud.** If a tutoring turn is processed by Anthropic, OpenAI or Google, the
student's message goes to that provider. If that is unacceptable under your
rules, the options are a regional provider endpoint, a self-hosted model, or
the offline desktop build — which runs a local model and never calls out.

### Backups

RDS keeps 14 days of automated backups. Take a manual snapshot before any
migration you cannot reverse. **Also back up two files that exist in one copy
each**: your Pulumi state (`~/.pulumi/stacks/<project>/<stack>.json`) and the
passphrase that encrypts it. Losing them does not destroy your infrastructure,
but it makes it unmanageable — recovery means importing every resource by hand.

### Routine operations

| Task | How |
|---|---|
| Deploy a change | Push to `main` (CI), or `pulumi up` with a new `image-tag` |
| Roll back | Point the service at the previous task definition revision |
| Scale up | `pulumi config set aitutor-aws:min-tasks 2 && pulumi up` |
| Read logs | CloudWatch log group `/ecs/<prefix>` |
| Restore a dump | `./ops/restore_from_dump.sh <file>` — runs inside the VPC, since the database is not publicly reachable |
| Rotate a key | `pulumi config set --secret ...` then `pulumi up`, then restart the service |

### Two things that will catch you

1. **A deploy does not carry configuration changes.** The CI pipeline copies the
   running task definition and swaps only the image, so environment variables
   changed in Pulumi do not reach the container until `pulumi up` runs *and*
   the service is pointed at the new revision. This silently served a stale
   `CSRF_TRUSTED_ORIGINS` for days.
2. **`/media/<path>` has no authentication.** Anyone with a URL can fetch any
   media file, including student-uploaded screenshots. This is a known,
   deliberate simplification. If your rules do not permit it, gate that route
   before going live.

---

## 5. Reducing the bill

In the order worth trying:

1. **Right-size Fargate.** 2 vCPU / 4 GB halves the largest line. Watch memory —
   the image carries CPU PyTorch and two speech models.
2. **Replace the NAT gateway with VPC endpoints** for ECR, S3, Secrets Manager
   and CloudWatch (~$33/month, more configuration). Only viable if the app does
   not need general outbound internet — it does, for LLM APIs, so this is a
   partial saving.
3. **Smaller database.** `db.t4g.small` saves ~$24/month. It caps connections
   lower, which has caused incidents before: `/health/` opens a connection and
   returns 503 when it cannot, so exhausting the pool marks every replica
   unhealthy at once.
4. **Choose the model deliberately.** Cheaper models for judging and content
   generation, the strong model only for tutoring. This is the biggest lever on
   total cost and it is not an AWS setting.
5. **Use the offline desktop build for classrooms.** No per-turn API cost and no
   connectivity requirement.

---

## Appendix — what a fresh deployment creates

VPC across 2 AZs (public + private subnets, 1 NAT gateway) · security groups
for ALB, tasks and RDS · ALB + target group + listeners · ACM certificate +
Route 53 records · WAF web ACL with 4 rules · ECS cluster + 3 task definitions
(web, migrate, material) + service · RDS PostgreSQL 16 · 3 S3 buckets (media,
ops, downloads) · ECR repository · 6 Secrets Manager secrets · CloudWatch log
group · IAM task and execution roles.

Roughly 70 resources. `pulumi destroy` removes them all — including the
database, so take a final snapshot first.
