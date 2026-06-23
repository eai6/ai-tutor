# WAF + Permanent Static IP (+ Security Roadmap) — Plan (2026-06-09)

## Problem
A pilot school's IT team wants to allow-list the platform by a **fixed IP**. Today the
app is served from Azure Container Apps, whose inbound IP (`20.12.152.191` for
`www.seselai.sc`) is static for the environment's lifetime but Microsoft-owned and not
guaranteed across an environment rebuild. We also have no Web Application Firewall (WAF)
at the edge — the biggest gap vs. industry-standard cloud security. We want a
**permanent, platform-owned static IP behind a WAF**, rolled out safely (review first,
nothing applied to production without sign-off).

Companion documents (already produced, `security/`):
- `AI Tutor_ Website Technical Details for Schools.docx` (allow-listing / domains + IPs)
- `AI Tutor_ Data Protection and Governance Overview.docx`
- `AI Tutor_ System Documentation and Architecture Overview.docx`
- `AI Tutor_ Terms of Service.docx`
- `AI Tutor_ Security Posture and Roadmap.docx`

## Current state (from audit)
- Azure Container Apps **managed environment** with a **WorkloadProfile** (D4),
  external ingress (`external=True`), **no custom VNet** — `infra/__main__.py:155`
  (env), `:172` (workload profile), `:530-533` (ingress).
- Prod: `www.seselai.sc` → CNAME `aitutor-pixel-app.niceground-67d5237f.centralus.azurecontainerapps.io`
  → **20.12.152.191** (Azure, Central US). Apex `seselai.sc` → `75.126.100.2`
  (a domain-forwarding host that redirects to `www`).
- Staging: `staging.seselai.sc` → `aitutor-staging-app.icyplant-cffb8b76...` → 20.112.227.89.
- Container Apps ingress supports **CIDR IP allow/deny only** — no country/geo filtering.
  Geo-filtering requires a WAF.
- Azure CLI is authenticated to **Pixel Design Labs LLC** (`656f4091-...`). Pulumi
  stacks: `pixel` (prod), `staging`. Passphrase in auto-memory (do not echo).

## Decisions (confirmed 2026-06-09)
- **Integration: Option B — fully private VNet rebuild** (max security). Option A kept
  below for reference only.
- **Cost: approved** (~US$300/mo).
- **TLS cert: Azure Key Vault**, populated by an **automated Let's Encrypt (ACME
  DNS-01)** flow.
- **DNS: Path B — keep DNS on name.com (no nameserver change).** DNS-01 automation uses
  the **name.com API**; the production cutover is a single `www` record edit at name.com.
- **Branch: do the work on `main`.**
- **Target stack: `pixel` (PRODUCTION) directly — NOT staging.** The App Gateway cost
  can't be doubled across environments, so we build straight on prod.
- **Still review-first / nothing harsh, and INCREMENTAL:** build in small,
  independently-previewable steps; the user runs `pulumi preview` and reviews the diff
  before every `pulumi up`. Steps 1–2 (Key Vault + cert) are free and non-disruptive.
  Step 3 (App Gateway + environment recreation) is the only paid/disruptive step and is
  done as a single scheduled **maintenance window** (brief outage + IP change).

### DNS facts (name.com, confirmed via dig 2026-06-09)
- Nameservers: `ns{1djs,2fgp,3ckl,4fmx}.name.com` (DNS hosted at name.com).
- `www.seselai.sc` → CNAME → Azure Container Apps prod FQDN → 20.12.152.191 (the app).
- `staging.seselai.sc` → CNAME → Azure staging FQDN → 20.112.227.89.
- Apex + misc names (`ai`,`api`,`app`,`mail`,`autodiscover`) all answer `75.126.100.2`
  = name.com URL-forwarding/wildcard (not real records). **No MX** → email is not on
  this domain, so the cert/cutover work won't affect mail.
- name.com has an ACME-supported API → DNS-01 cert automation without moving DNS.

## Cert automation (Path B)
- An ACME client (**acme.sh** with the `dns_namecom` plugin) issues a Let's Encrypt cert
  for `www.seselai.sc` (and `staging.seselai.sc`) via **DNS-01** and deploys it into
  **Azure Key Vault** (acme.sh has an Azure Key Vault deploy hook).
- Run it as a **scheduled GitHub Actions workflow** (renew ~every 60 days) using a
  name.com API token + Azure creds stored as GitHub secrets — no extra Azure compute.
- App Gateway references the **Key Vault cert by versionless secret ID**, so it picks up
  renewals automatically.
- **Needed from user:** a **name.com API token** (name.com → Account → API settings).
  Keep it secret — store as a GitHub Actions secret / Pulumi secret; do not paste in chat.

## Incremental build steps (all on `pixel`/prod, each independently previewable)
1. **Key Vault + user-assigned managed identity** — FREE, non-disruptive (does not touch
   the running app). Apply on prod now after preview.
2. **Cert automation**: acme.sh GH Actions workflow → Let's Encrypt via name.com DNS-01
   → Key Vault. FREE, non-disruptive. Validate the cert lands in KV.
3. **VNet + recreated internal Container Apps env + static Public IP + App Gateway
   WAF_v2** (HTTPS listener using the KV cert; internal backend; private DNS zone). WAF
   in **Detection** mode. This is the PAID + disruptive step (env recreation) — do it in
   a scheduled maintenance window, `pulumi preview` reviewed first.
4. **Cutover**: edit `www.seselai.sc` at name.com → App Gateway static IP; switch WAF
   Detection → Prevention once validated; update the schools' Website Technical Details
   doc with the final IP.

## Target design
Front the app with **Azure Application Gateway + WAF_v2** on a **dedicated static
Standard Public IP** that we own. Schools allow-list that one IP permanently.

What the WAF adds (industry-standard edge security):
- Permanent platform-owned static inbound IP.
- OWASP Core Rule Set (SQLi, XSS, common exploits) + managed bot protection.
- Edge rate limiting; optional geo / IP custom rules (e.g. Seychelles-only — used
  cautiously; GeoIP is approximate and would block staff abroad / CI / monitoring).
- Azure infrastructure DDoS (option to add DDoS Standard).
- WAF request logging → Log Analytics.

### Integration — CHOSEN: Option B (fully private)
- **Option B — fully private (CHOSEN, gold standard).** Recreate the Container Apps
  environment **inside a VNet with internal ingress**; the App Gateway (with WAF + static
  public IP) is the **only** public door. The app has no direct public endpoint. This is
  the most secure topology. It requires recreating the managed environment (Pulumi
  *replace*), so there is a one-time production maintenance window (brief outage + IP
  change) at cutover — done on staging first, then scheduled for prod.
- **Option A — App Gateway in front of the existing external ingress (reference only,
  not chosen).** Lower disruption (no env rebuild) but leaves the Container Apps public
  endpoint existing (locked down by CIDR). Kept here in case we want a no-downtime
  interim step before B.

### Why App Gateway and not Front Door
Front Door Premium has the same WAF/geo/bot features and auto-managed TLS, but uses
Microsoft **anycast IP ranges** — no single owned IP. The school wants *an IP*, so App
Gateway (dedicated static IP) is the fit. (If the IP requirement is ever dropped, Front
Door is simpler and cheaper for TLS.)

## Prerequisites — all confirmed (2026-06-09)
1. **Cost:** CONFIRMED — ~US$300/month approved.
2. **TLS certificate for `www.seselai.sc`:** DECIDED — option (a), **Azure Key Vault**
   cert referenced by App Gateway. (Need to obtain a cert for www.seselai.sc to import
   into Key Vault — e.g. issue via an ACME/Let's Encrypt flow or a purchased cert.)
3. **DNS control:** CONFIRMED — we manage seselai.sc DNS and can repoint `www`.

## Progress (live — 2026-06-09, prod `pixel`, nothing committed yet)
- ✅ **Step 1 applied to prod**: `aitutorpixelkv` (Key Vault) + `aitutor-pixel-appgw-id`
  (user-assigned identity). Clean apply (`+2 created, 19 unchanged`), zero downtime.
- ✅ **Drift fix applied**: added `ignore_changes=[template.containers[0].env, image]` to
  the ContainerApp + Job so Pulumi no longer fights CI deploys (was about to revert the
  live app + replace a role assignment on any `pulumi up`). Preview is now clean.
- ✅ **KV access policy**: CI SP (`4341d1f9-…`, obj id) granted cert import via config
  `cicd-sp-object-id`. Applied.
- ✅ **Step 2 cert in KV**: Let's Encrypt cert for `www.seselai.sc` issued locally via
  acme.sh + name.com DNS-01, imported as cert `www-seselai-sc`. Versionless secret id:
  `https://aitutorpixelkv.vault.azure.net/secrets/www-seselai-sc`. Expires 2026-09-07.
- ✅ **Renewal automation written**: `.github/workflows/cert-renew.yml` (needs GH secrets
  `NAMECOM_USERNAME`, `NAMECOM_TOKEN` once committed).
- **Uncommitted working-tree changes**: `infra/__main__.py`, `infra/Pulumi.pixel.yaml`
  (enable-waf + cicd-sp-object-id), `.github/workflows/cert-renew.yml`, the `security/`
  docs, `memory/waf_static_ip_security_plan.md`.
- ✅ **Step 3 DONE (2026-06-09, in maintenance window)** — Option B fully-private applied
  to prod. `pulumi up` replaced the env (internal, VNet-injected, private IP 10.20.0.23,
  domain `redbush-f73ec411…`), created VNet + 2 subnets + private DNS zone (wildcard `*`
  → 10.20.0.23) + static Public IP + WAF policy (OWASP, **Detection mode**) + Application
  Gateway. **Static IP: `<App Gateway static IP — see `pulumi stack output waf_appgw_public_ip`>`.** App Gateway backend health = Healthy; TLS =
  Let's Encrypt KV cert; HTTP→HTTPS redirect works. **14 unchanged** — Storage/File Share
  + Postgres untouched (no data loss), confirmed. Forced env replace via
  `replace_on_changes=["vnetConfiguration"] + delete_before_replace` (Azure VNet config
  is immutable).
- ✅ **DNS cutover done** at name.com: `www.seselai.sc` A → <App Gateway static IP — see `pulumi stack output waf_appgw_public_ip`> (was CNAME to
  the old env). Verified end-to-end (health/home 200, cert CN=www.seselai.sc, redirect).

### Follow-ups (post-migration)
- **WAF is in Detection mode** — switch to **Prevention** before/at pentest (one-line
  `mode="Prevention"` + apply); Detection-first avoids false-positive lockouts during
  normal pilot use.
- **CI env vars**: the recreated app came up on `aitutor:latest` + Pulumi env; the next
  normal CI deploy re-applies `TUTOR_PROMPT_VARIANT`/`TUTOR_MODEL_OVERRIDE`.
- **Cert auto-renewal**: add GitHub secrets `NAMECOM_USERNAME` + `NAMECOM_TOKEN` so
  `.github/workflows/cert-renew.yml` renews (cert valid 90 days; expires 2026-09-07).
- Updated `security/AI Tutor_ Website Technical Details for Schools.docx` with the static IP.

## Rollout — Option B, review-first, nothing harsh
Resources to add in `infra/__main__.py` (per stack):
- **VNet** with two subnets: one **delegated to Container Apps** (infrastructure subnet,
  /23 min) and one for the **App Gateway** (/24).
- **Container Apps managed environment** recreated with `vnetConfiguration`
  (infrastructure subnet) + **internal** ingress. (Pulumi *replace* — this is the
  disruptive step.)
- **Azure Key Vault** + the `www.seselai.sc` certificate; a **user-assigned managed
  identity** granted `get` on KV secrets/certs for App Gateway to read it.
- **Static Standard Public IP** + **Application Gateway WAF_v2** (HTTPS listener using
  the KV cert; backend = the environment's internal FQDN; health probe with Host
  header). WAF in **Detection (log-only)** mode initially.
- **Private DNS zone** for the internal Container Apps environment so App Gateway can
  resolve the internal backend.

Sequence:
1. **Write Pulumi** (above) — WAF in Detection mode; geo/IP rules off initially.
2. **`pulumi preview` on `staging`** (dry-run, changes nothing) → review the diff,
   especially every resource marked **replace** (the environment), and the static IP it
   allocates.
3. After review, **`pulumi up` on staging** → import KV cert → validate end-to-end over
   `staging.seselai.sc` (TLS, app loads via App Gateway, internal-only backend, WAF
   logs, health) → repoint staging DNS.
4. **Review together**, then schedule the **prod maintenance window**: `pulumi preview`
   on `pixel` → approve → `pulumi up` → repoint `www.seselai.sc` → the new App Gateway
   static IP. Switch WAF from Detection to **Prevention** once validated.
5. Update `security/AI Tutor_ Website Technical Details for Schools.docx` with the final
   permanent IP.

## Out of scope (this plan)
- The rest of the security roadmap below (tracked separately).
- Switching WAF custom geo/IP rules from log-only to enforce (do after a clean
  detection-mode baseline, to avoid locking out legitimate users).

## Broader security roadmap (from the Security Posture doc)
- **Now/in progress:** WAF + static IP (this plan); confirm Postgres backups / PITR + DR.
- **Next:** MFA for teacher/admin accounts; centralised monitoring/alerting (SIEM, e.g.
  Microsoft Sentinel); documented data-retention schedule.
- **Then:** independent vulnerability scan / pentest; DPIA + incident-response plan;
  Data Processing Agreements with AI/cloud sub-processors.

## Risks
- App Gateway provisioning is ~20–30 min; the DNS cutover is the only user-visible moment
  (mitigated by validating on staging first and keeping TTL low before cutover).
- Editing `infra/__main__.py` can recreate resources on breaking changes — always
  `pulumi preview` and read the diff before `pulumi up` (per CLAUDE.md + azure-cloud-expert).
- TLS misconfiguration would break HTTPS — validate on staging with the real cert first.
- Cost is ongoing (~$300/mo) — confirmed before build.

## Next step
Prerequisites are confirmed. On `main`: write the Option B Pulumi, then run a **staging
`pulumi preview`** (dry-run — changes nothing) and review the diff together. The first
real apply happens on **staging only**; production is a later, scheduled maintenance
window after the staging build is validated and reviewed.

One input still needed to actually issue TLS: a certificate for `www.seselai.sc` to put
in Key Vault (we can generate one via Let's Encrypt/ACME or import a purchased cert).
