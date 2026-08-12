# AI Tutor on AWS — platform, cost, and self-hosting

For a ministry, school network, or partner that wants to run AI Tutor in its own
AWS account, under its own control, with student data inside its own borders.

---

## 1. What actually runs

The platform is a single Django application in a container, a Postgres
database.


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


---

## 2. What it costs

### Fixed 

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

- **≈ $259/month is the floor** for this shape. About $58 of it (NAT + ALB +
  WAF) is *plumbing rather than capacity* — you pay it for a safe network and a
  firewall, not for the ability to serve one more student.

### Variable — scales with use

| Item | Driver | Rough shape |
|---|---|---|
| **LLM API calls** | Tutoring turns | $600 for 4 schools

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

