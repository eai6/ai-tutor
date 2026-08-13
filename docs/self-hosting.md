# AI Tutor — self-hosting manual

For a ministry, school network, or partner that wants to run AI Tutor itself,
under its own control, with student data inside its own borders.

Three supported paths. All run the same application; they differ in who
operates the machine underneath it, and how much of the surrounding plumbing
comes with it.

---

## 1. Choosing a path

| | **A — your own server** | **B — your own AWS account** | **C — pip, no Docker** |
|---|---|---|---|
| You provide | One Linux server, a domain | An AWS account, a domain | A Linux server, a domain, Python 3.12+ |
| Source needed | No — the published image is enough | Yes, for the Pulumi program | No — the wheel is enough |
| Runs on | Docker Compose | ECS Fargate + RDS, built by Pulumi | systemd + gunicorn |
| Database | Bundled Postgres container | RDS, managed | **You install and run Postgres** |
| TLS | Automatic, via Caddy | Automatic, via ACM | **You run a reverse proxy** |
| Monthly cost | Server + LLM usage | ≈ $259 infrastructure + LLM usage | Server + LLM usage |
| Scales to | A few thousand students on one box | Autoscales; add tasks and database size | A few thousand students on one box |
| Who patches the OS | You | AWS | You |
| If the machine dies | You restore from backup | Redeploy; the database is separate | You restore from backup |
| Set-up time | An afternoon | A day, plus DNS propagation | An afternoon, if Postgres is familiar |

**Choose A** if you have a server or a data-centre requirement, or you want the
whole system inside one machine you can point at. It is the simpler thing to
understand and the cheaper thing to run.

**Choose B** if you already operate in AWS, need it to survive a machine
failure without someone intervening, or expect to grow past one server.

**Choose C only if you cannot run containers** — a policy that forbids Docker,
or a managed Python estate you must fit into. It installs the same application
from a wheel, but the database, TLS and process supervision that Path A hands
you in one command all become yours to assemble. Path A is less work and fewer
things to get wrong; prefer it unless something rules it out.

Neither path changes what the tutor does or where the *model* runs — see
[section 7](#7-what-leaves-your-network).

---

## 2. What runs

### Path A — one server

```
                Internet
                    │
                   Caddy         TLS terminates here (certificate is
                    │            obtained and renewed automatically)
                    ▼
              Django + Gunicorn  ── media volume (uploaded figures)
                    │
                    ▼
            PostgreSQL + pgvector   ── data volume
```

Three containers, two volumes, one server. The knowledge base is stored as
vectors *in Postgres*, so there is no separate vector database to run.

### Path C — pip on your own server

```
                Internet
                    │
             Caddy or nginx      TLS terminates here — YOURS to install,
                    │            configure and renew
                    ▼
       ai-tutor serve (gunicorn)  ── data directory (uploads, static)
                    │
                    ▼
            PostgreSQL + pgvector  ── YOURS to install and back up
```

The same application, assembled by hand instead of by Compose. The two boxes
marked *yours* are the difference between this path and Path A.

### Path B — AWS

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

| Resource | What it is | Why |
|---|---|---|
| **ECS Fargate** | Runs the container. 4 vCPU / 8 GB, 1 task, autoscales to 4. | No servers to patch. The image carries CPU-only PyTorch and two speech models, which is why the task is large. |
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

### What AWS costs

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

≈ $259/month is the floor for this shape. About $58 of it (NAT + ALB + WAF) is
plumbing rather than capacity — you pay it for a safe network and a firewall,
not for the ability to serve one more student.

**Variable cost is dominated by LLM API calls**, which scale with tutoring
turns: roughly **$600/month for four schools** in the Seychelles pilot. Path A
avoids the $259 but not this.

The **offline desktop build** exists precisely so classroom use does not depend
on any of it.

---

## 3. Path A — your own server

### What you need

- A Linux server: **4 vCPU, 8 GB RAM, 60 GB disk**. x86_64 or ARM both work —
  the image builds natively for whatever the server is. A 2 GB VPS will not run
  this: the image is **about 7 GB**, carrying a CPU build of PyTorch, a Whisper
  model and a Piper voice.
- Building needs headroom well beyond the image itself. Budget **25 GB free**
  for the build, or prune afterwards — BuildKit's layer cache is comfortably
  larger than the result.
- **Docker** and the **Compose plugin**.
- A **domain name** pointing at the server's public IP, with ports 80 and 443
  reachable. HTTPS cannot be issued without it.
- At least one **LLM provider API key** — Anthropic, OpenAI, or Google.

### Install

You need five files: the compose definition, the TLS configuration, a
configuration template, and the backup/restore scripts. Take them straight out
of the published image — no source code, no repository access, and they can
never be a version out of step with the image they configure:

```bash
mkdir -p ai-tutor && cd ai-tutor

docker run --rm ghcr.io/eai6/ai-tutor:latest \
  tar -C /app/deploy/compose -c \
      docker-compose.yml Caddyfile env.example backup.sh restore.sh | tar -x

cp env.example .env
chmod 600 .env
```

<details>
<summary>Prefer to work from the source?</summary>

If you have the repository, the same files are in `deploy/compose/`:

```bash
git clone <this repo> ai-tutor
cd ai-tutor/deploy/compose
cp env.example .env && chmod 600 .env
```

Working from source also lets you build the image yourself, which is how an ARM
server gets one — the published image is x86_64.
</details>

Open `.env` and fill in everything marked REQUIRED. Each entry says what it is
and what breaks without it. Generate the two secrets rather than inventing
them:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(64))"   # SECRET_KEY
python3 -c "import secrets; print(secrets.token_urlsafe(32))"   # POSTGRES_PASSWORD
```

Then pull the image and start:

```bash
docker compose pull        # ~7 GB, once
docker compose up -d
docker compose logs -f app
```

**Pull before `up`.** If the image is not already on the machine, `up` builds it
from source instead — which needs the source, about 25 GB of free disk, and a
good deal of time.

The published image is **x86_64**. On an ARM server, clone the repository and
run `docker compose build` instead; nothing is pinned to an architecture.

**No API keys?** The tutor can run on a local model instead, with no account
anywhere:

```bash
docker compose --profile local-llm up -d
```

That adds an Ollama container, pulls a ~2.6 GB model on first start, and points
the tutor at it automatically.

Treat this as a **fallback for getting started without a commercial account**,
not as an equivalent to the cloud models. A small local model is measurably
weaker at tutoring, and we have not yet characterised the gap well enough to put
a number on it here. It also needs roughly 4 GB of RAM beyond the application,
and replies are slower on a server with no GPU.

Add a provider key later and restart; the local configuration steps aside on its
own.

### What you get on first start

The image ships with a **curriculum already loaded** — courses, lessons, steps
and exit tickets — imported automatically the first time the container boots.
No internet is needed for it: the content is inside the image, not downloaded.

**Figures and images are not included.** They are roughly twenty times the size
of the text and would make every clone of this repository carry them forever.
About three quarters of lesson steps reference a figure, so those steps read
with a visible gap until you add media — by uploading your own through the
teacher dashboard, or by importing a pack built with media:

```bash
# on a deployment that has the figures
python manage.py build_curriculum_pack --institution <id> --out dist/
# then, on this one
docker compose exec app python manage.py import_curriculum_pack /path/pack.tar.gz --force
```

You can also start from nothing and upload your own curriculum — the seeded
content is a starting point, not a requirement.

The first start takes several minutes: it builds the image, runs migrations,
seeds reference data and builds the help index. It is ready when the log shows
gunicorn listening and `docker compose ps` reports `app` healthy.

If a required value is missing, Compose refuses to start and names it. That is
deliberate — a half-configured server is worse than one that did not start.

### First run

1. Create your administrator account:

   ```bash
   docker compose exec app python manage.py createsuperuser
   ```

2. Sign in at `https://<your-domain>/admin/` and set which model serves which
   purpose under **LLM → Model configs**. Nothing works until at least
   `tutoring` points at a provider whose key you supplied.
3. Create your institution and upload a curriculum through the teacher
   dashboard.
4. **Take one tutoring turn.** This is the real test — it exercises the
   database, the knowledge base, the LLM credentials and media in one action.
   A health check does not.

### When something is wrong

Symptoms first, because that is how they present.

| What you see | Cause | Fix |
|---|---|---|
| **Login returns 403, no error anywhere.** The form just bounces back. | Django marks session and CSRF cookies `Secure`, and a browser will not send those over plain HTTP. | You are serving HTTP without TLS. Either finish the TLS setup, or set `HTTPS_EDGE=false` in `.env` **and** switch the Caddyfile to its plain-HTTP block. Only do this on an isolated network. |
| **`DisallowedHost` error page.** | The `Host` header does not match `ALLOWED_HOSTS`. | Add the hostname to `ALLOWED_HOSTS` in `.env`, including any alias students use. |
| **Every form POST fails CSRF**, including login. | `CSRF_TRUSTED_ORIGINS` is unset or missing the scheme. | It needs the full origin: `https://tutor.example.gov`, not a bare hostname. |
| **App refuses to start**, complaining about `SECRET_KEY`. | It is still the development default and `DEBUG` is off. | Working as intended. Generate one and put it in `.env`. |
| **Tutor replies cut off or return blank** on long answers. | A proxy timeout shorter than the model's reply. | Both Caddy and gunicorn are set to 300 s here. If you put your own proxy in front, raise its read timeout to match. |
| **Certificate never issues.** | The domain does not resolve to this server, or 80/443 are blocked. | Check DNS and the firewall. Caddy retries; `docker compose logs caddy` says which. |
| **Lessons show broken images** after a restore. | Database and media came from different moments. | Restore both halves from the same backup file. |
| **Caddy returns 502**, but `docker compose ps` says the app is healthy. | The app is listening on loopback inside its container, so nothing else can reach it. A healthcheck that only tests 127.0.0.1 passes anyway. | Check `docker compose logs app` for `Listening at:` — it must say `0.0.0.0:8000`, not `127.0.0.1:8000`. |
| **Build fails: "Multi-platform build is not supported for the docker driver".** | A platform pin put BuildKit into multi-platform mode. | The shipped compose file has no pin — build natively. Only the AWS path needs `--platform linux/amd64`. |
| **Docker fails with `input/output error`** during a rebuild. | The Docker VM's disk filled. Its size limit is separate from your host's free space, and images plus layer cache add up fast. | `docker builder prune -af`, then Docker Desktop → Troubleshoot → Clean/Purge data if it persists. |

### Upgrading

```bash
cd ai-tutor
docker compose pull
docker compose up -d
```

Migrations run automatically on start. **Take a backup first** — see below.

To pin a version rather than track `latest`, set `IMAGE_TAG=v1.2.0` in `.env`.
Pinning is the safer habit: it makes an upgrade something you choose, and makes
rolling back a matter of changing one line.

---

## 4. Path B — your own AWS account

### What you need first

- An AWS account, and an IAM user or role that can create VPC, ECS, RDS, S3,
  IAM, WAF and Route 53 resources.
- A domain you control (for HTTPS — a certificate cannot be issued for an
  AWS-owned load balancer hostname).
- At least one **LLM provider API key**.
- Installed locally: `git`, `aws` CLI v2, `pulumi`, `docker`, Python 3.12.
- **The repository**, for the infrastructure-as-code program in `infra/aws/`.
  Path A can run from the published image alone; this path cannot.

### Step 1 — Clone and configure the stack

```bash
git clone <this repo> ai-tutor
cd ai-tutor/infra/aws

python -m venv venv && ./venv/bin/pip install -r requirements.txt

aws configure                 # or aws sso login
pulumi login --local          # state in ~/.pulumi; back this up
pulumi stack init prod
```

Every key below is read by `infra/aws/__main__.py`:

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

On AWS this must be a one-shot task, not part of container start: ECS launches
every task at once, so migrations in `CMD` would race across replicas.

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

Then load the site, create a staff account, upload curriculum, and **take one
tutoring turn**.

---

---

## 5. Path C — pip install, no Docker

Same application, installed as a Python package. Choose this only if
containers are ruled out; Path A gives you the database, TLS and process
supervision that this path leaves to you.

### What you need first

- A Linux server: 4 vCPU, 8 GB RAM, 60 GB disk.
- **Python 3.12 or newer.** Django 6 requires it; 3.11 will not install.
- **PostgreSQL 14+ with the `pgvector` extension available.** The knowledge
  base is stored as vectors in Postgres.
- A domain pointing at the server, and something to terminate TLS.
- Roughly 3 GB of disk for the dependencies alone — the install pulls
  `onnxruntime`, `sentence-transformers` and `faster-whisper`.

### Step 1 — Install

```bash
sudo apt install python3.12 python3.12-venv postgresql postgresql-16-pgvector

sudo -u postgres createuser --pwprompt aitutor
sudo -u postgres createdb --owner aitutor aitutor

sudo python3.12 -m venv /opt/ai-tutor/venv
sudo /opt/ai-tutor/venv/bin/pip install ai-tutor
```

The extension itself is created by a migration; it only has to be *available*
to Postgres, not enabled by hand.

### Step 2 — Configure

```bash
sudo /opt/ai-tutor/venv/bin/ai-tutor init
```

That writes `/etc/ai-tutor/ai-tutor.env` (mode 600) with a freshly generated
`SECRET_KEY`, and creates `/var/lib/ai-tutor` for the database, uploads and
collected static files. Re-running it never overwrites an existing file —
rotating the key would log every user out and void every outstanding
password-reset link.

Edit that file and set:

| | |
|---|---|
| `ALLOWED_HOSTS` | The hostname students will type. |
| `CSRF_TRUSTED_ORIGINS` | The same, **with the scheme**. Without it every login fails with no useful error. |
| `DATABASE_URL` | `postgres://aitutor:PASSWORD@localhost:5432/aitutor` |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `GOOGLE_API_KEY` | At least one, or the tutor cannot answer. |

Leave `HTTPS_EDGE=true` if a proxy terminates TLS in front, which it should.

### Step 3 — Database, content, administrator

```bash
sudo /opt/ai-tutor/venv/bin/ai-tutor migrate
sudo /opt/ai-tutor/venv/bin/ai-tutor seed
sudo /opt/ai-tutor/venv/bin/ai-tutor collectstatic --noinput
sudo /opt/ai-tutor/venv/bin/ai-tutor createsuperuser
```

`seed` loads the curriculum bundled in the release. If it reports that the pack
was built against a different database schema, it says so and continues with an
empty curriculum rather than refusing to start — upload content through the
dashboard, or import a rebuilt pack.

Any Django management command works: `ai-tutor shell`, `ai-tutor dbshell`,
`ai-tutor check --deploy`.

### Step 4 — Run it as a service

```bash
sudo /opt/ai-tutor/venv/bin/ai-tutor systemd | sudo tee /etc/systemd/system/ai-tutor.service
sudo systemctl daemon-reload
sudo systemctl enable --now ai-tutor
```

The generated unit carries this installation's real paths, restarts on failure,
and confines writes to the data directory. It binds `0.0.0.0:8000` with a 300 s
timeout — long, because a tutoring turn takes 20-90 seconds and a shorter one
kills the worker mid-answer.

### Step 5 — TLS in front

Nothing here obtains a certificate for you. With Caddy:

```
tutor.education.gov.xx {
    encode gzip
    reverse_proxy 127.0.0.1:8000
}
```

Caddy obtains and renews the certificate automatically. With nginx, use
certbot, and set `proxy_read_timeout 300s;` — the default 60 s cuts long tutor
replies off mid-sentence.

Whatever you put in front **must health-check `/health/` over plain HTTP** and
must forward `X-Forwarded-Proto`. `/health/` is deliberately exempt from the
HTTPS redirect for exactly this reason.

### Step 6 — Verify

```bash
systemctl status ai-tutor
curl -sS localhost:8000/health/          # {"status": "ok", ...}
sudo /opt/ai-tutor/venv/bin/ai-tutor check
```

Then sign in at `https://<your-domain>/admin/`, point `tutoring` at a provider
under **LLM → Model configs**, and **take one tutoring turn**. That exercises
the database, the knowledge base, the credentials and media in one action. A
health check does not.

### What this path does not give you

Stated plainly, because each is something Path A does for you:

- **The database.** Installing, tuning, securing and backing up Postgres is
  yours. Nothing here creates a backup schedule.
- **TLS.** No certificate is obtained or renewed for you.
- **Isolation.** The application runs as a normal process against your system
  Python packages, not in a container.
- **SQLite is allowed but limited.** Leave `DATABASE_URL` unset and a SQLite
  file is used in the data directory. Workable for a single small school;
  `pgvector` is unavailable there, so knowledge-base search falls back to a
  slower exact scan and will not hold up under a whole school.

### When something is wrong

| What you see | Cause | Fix |
|---|---|---|
| **`ai-tutor: command not found`** | The venv's `bin` is not on `PATH`. | Call it by full path, `/opt/ai-tutor/venv/bin/ai-tutor`, which is what the systemd unit does. |
| **"Refusing to serve: SECRET_KEY, ALLOWED_HOSTS … not set."** | No config file was found, or it is somewhere `ai-tutor` does not look. | Run `ai-tutor init`, or set `AI_TUTOR_ENV_FILE` to the file you have. |
| **Service starts, then fails reading its configuration.** | `ProtectHome=yes` hides `/home`, and the config or data lives there. | Regenerate the unit — `ai-tutor systemd` relaxes it automatically when it detects this. Better: keep config in `/etc` and data in `/var/lib`. |
| **Config changes have no effect.** | Something in the real environment overrides the file — that is deliberate precedence. | Check the unit's `Environment=` lines and the shell you ran from. |
| **Everything works as root, nothing works as the service user.** | `/var/lib/ai-tutor` is owned by root. | `chown -R` it to the user in the unit's `User=` line. |
| **Upgrade appears to do nothing.** | The service is still running the old code. | `systemctl restart ai-tutor` after every upgrade; unlike Compose, pip does not restart anything. |
| **Login returns 403, no error anywhere.** | Session and CSRF cookies are marked `Secure` and the browser will not send them over plain HTTP. | Finish the TLS setup, or set `HTTPS_EDGE=false` — only on an isolated network. |

### Upgrading

```bash
sudo /opt/ai-tutor/venv/bin/pip install --upgrade ai-tutor
sudo /opt/ai-tutor/venv/bin/ai-tutor migrate
sudo /opt/ai-tutor/venv/bin/ai-tutor collectstatic --noinput
sudo systemctl restart ai-tutor
```

**Back up the database first.** Nothing runs migrations or restarts the service
for you on this path. Your data directory is untouched by an upgrade — that is
why it lives outside the installed package.

## 6. Operating it

### Backup and restore

**Path A.** Scripts ship alongside the compose file:

```bash
cd ai-tutor/deploy/compose
./backup.sh                      # → ./backups/aitutor-YYYY-MM-DD-HHMM.tar.gz
./backup.sh /mnt/backup-drive    # or somewhere else
./restore.sh backups/aitutor-2026-08-12-1830.tar.gz
```

The archive contains the database *and* the uploaded media, taken together. It
has to be both: the database holds the reference to every figure and the volume
holds the file, so a database restored against older media leaves lessons with
broken images and reports no error.

**Path B.** RDS takes automated backups on a 14-day window; restore through the
AWS console or `ops/restore_from_dump.sh`. Media lives in S3 and is versioned
separately.

**Path C.** Nothing is scheduled for you. Both halves, together, into one file:

```bash
pg_dump -Fc aitutor > /mnt/backup/aitutor-$(date +%F).dump
tar czf /mnt/backup/media-$(date +%F).tar.gz -C /var/lib/ai-tutor media
```

Restore is `pg_restore -d aitutor` and untarring the media back into the data
directory. Put both in a cron job or a systemd timer on the day you install —
"we'll add backups later" is how deployments lose a term of work.

Three rules that apply to all three:

- A backup that only exists on the machine it backs up is not a backup.
- Backups contain **student records**. Store them encrypted, with access
  restricted to the same people who can reach the server.
- **Test a restore.** An untested backup is a guess. Do it once on a spare
  machine before you need it.

### Logs

```bash
docker compose logs -f app          # Path A
journalctl -u ai-tutor -f           # Path C
```

Path B streams to CloudWatch Logs under the cluster's log group.

### Keeping it current

Back up first, and read the release notes before upgrading across a major
version.

On Paths A and B, migrations run on start; pull and restart is the whole
procedure. **Path C is the exception** — pip replaces the code and nothing
else, so `ai-tutor migrate`, `ai-tutor collectstatic` and
`systemctl restart ai-tutor` are yours to run, in that order.

---

## 7. What leaves your network

Everything student-related stays where you put it: the database, uploaded
media, and the application itself.

**The exception that matters: LLM API calls.** When a student sends a message,
that message goes to Anthropic, OpenAI or Google to be answered. It leaves your
server, your country, and your cloud. No configuration in this manual changes
that, because it is what makes the tutor work.

If that is unacceptable under your rules, there are three options:

1. A **regional endpoint** from a provider that offers one in your jurisdiction.
2. A **self-hosted model**. The platform speaks to Ollama, so a local model can
   serve the tutoring role and nothing leaves your network. Expect weaker
   tutoring than a frontier cloud model — how much weaker depends on the model
   and on your hardware, and is worth measuring on your own content before
   committing a cohort to it.
3. The **offline desktop build**, which runs a local model on the classroom
   machine and never calls out at all.

Also leaving your network, and worth knowing: certificate issuance contacts
Let's Encrypt, and container image pulls contact the registry. Neither carries
student data.
