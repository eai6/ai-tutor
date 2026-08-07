# Infra — AWS (Pulumi)

AWS deployment of AI Tutor, running **alongside** the Azure stack in `../`.

> Azure serves live users and is untouched by anything here. This is a second,
> parallel deployment of the same container image — not a cutover. See
> `docs/superpowers/specs/2026-08-07-aws-migration-design.md`.

Project `aitutor-aws`, stack `dev`, region `us-east-1`.

## Current state

**ECS is gated off.** `iam:PassRole` is denied for this account's SSO role, and
a Fargate task definition cannot omit an execution role. The code is written and
validated (`pulumi preview` plans it cleanly); it is simply not enabled.

| | Resources | Status |
| --- | --- | --- |
| Foundation | 49 | Deployable now |
| With ECS | 61 | Blocked on IAM |

Unblocking, once the grant in `docs/aws-iam-access-request.md` lands:

```bash
pulumi config set enable-ecs true
pulumi up
```

## Quick start

```bash
cd infra/aws
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt

# `pixeldesignlabs` is the AZURE org. AWS runs in a separate account, so the
# profile here must NOT be reused from infra/ (the Azure Pulumi program).
export AWS_PROFILE=aitutor
aws sso login --profile aitutor           # SSO tokens expire; omit for a
                                          # plain IAM-user profile

pulumi stack select dev
pulumi preview
pulumi up
```

## Layout

```text
__main__.py            orchestration, config, exports
components/
  network.py           VPC, subnets, NAT, S3 endpoint, security groups
  storage.py           S3 media bucket, ECR + lifecycle policy
  data.py              RDS Postgres, Secrets Manager
  edge.py              ALB, target group, listener, WAF
  compute.py           IAM roles, task definitions, service, autoscaling (gated)
```

Split by concern deliberately. The Azure program is a single 1180-line file and
answering basic questions about it meant reading all of it.

## Config

| Key | Default | Notes |
| --- | --- | --- |
| `aws:region` | — | `us-east-1` |
| `enable-ecs` | `false` | Flip when the IAM grant lands |
| `db-instance-class` | `db.t4g.medium` | A step up from Azure's B1ms |
| `db-storage-gb` | `50` | Autoscales to 4× |
| `waf-block-mode` | `false` | Ships in COUNT; see below |
| `min-tasks` / `max-tasks` | `1` / `4` | Matches Azure |
| `image-tag` | `latest` | |
| `django-secret-key` etc. | placeholder | `pulumi config set --secret <key> <value>` |

Secrets left unset are stored as an obvious `PLACEHOLDER-…` string so the
foundation can be deployed before every key is in hand. Set the real values
before running ECS.

## Decisions that will bite if reverted

- **VPC is `10.30.0.0/16`.** Azure's VNet is `10.20.0.0/16`, and the two run in
  parallel — they must not collide if ever peered.
- **ALB idle timeout is 120s**, not the 60s default. Gunicorn runs
  `--timeout 120` and tutoring turns wait on an LLM; the default would sever
  long requests with a 504 while the worker kept going.
- **Task security group accepts 8000 from the ALB security group only.** This
  is what makes `apps/safety/client_ip.py` trusting the last `X-Forwarded-For`
  hop safe. Widen it and client IPs become attacker-controlled.
- **WAF ships in COUNT mode.** Azure runs OWASP in Prevention, so this is
  briefly weaker on purpose — the common rule set false-positives on rich-text
  teacher dashboard input, and finding that out by blocking real teachers is
  the wrong way to find out. Flip `waf-block-mode` once sampled logs are clean.
- **RDS enhanced monitoring is off.** It needs `iam:PassRole` on
  `rds-monitoring-role`. Standard CloudWatch metrics are unaffected.
- **One NAT gateway, not one per AZ.** A single point of failure for egress;
  halves the ~$33/mo standing cost. Revisit before real traffic.
- **The web task definition overrides `command` to gunicorn alone.** The
  Dockerfile `CMD` also runs migrate plus six seed commands, which Azure relies
  on because single-revision mode serialises it. ECS starts every task at once,
  so that chain runs as the separate `migrate` task definition instead. **Do not
  "fix" this by editing the Dockerfile** — that would break Azure.

## No TLS yet

The ALB serves plain **HTTP on port 80** on its own hostname. AWS has no domain
and serves no users, and a public ACM certificate cannot be issued for an
AWS-owned ELB domain.

The task environment therefore sets `HTTPS_EDGE=false`. Without it, `DEBUG=False`
marks session and CSRF cookies `Secure`, browsers refuse to send them over HTTP,
and **every login silently bounces back to the login page with no error**.

When a hostname arrives: request an ACM cert, add the validation CNAME, add a
443 listener, redirect 80 → 443, drop `HTTPS_EDGE`. Nothing above needs redoing.

## Cost

Roughly **$270–330/month** for the foundation: Fargate ~$144 (one always-on
4 vCPU / 8 GiB task, once ECS is enabled), RDS `db.t4g.medium` ~$53, NAT ~$33,
ALB ~$16 plus LCUs, WAF ~$8, and S3/ECR/Secrets/CloudWatch ~$12.

`pulumi destroy` tears the whole stack down; nothing here is shared with Azure.
