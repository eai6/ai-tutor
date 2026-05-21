# Infra (Pulumi)

Pulumi Python program that provisions the full Azure stack for AI Tutor:

- Resource group
- Azure Container Registry (ACR)
- Azure Container App Environment (D4 workload profile)
- Azure Container App (4 vCPU / 8 GiB, scales on HTTP)
- Azure PostgreSQL Flexible Server + database
- Azure Storage Account + File Share (mounted at `/app/media`)
- Managed Identity for ACR pull

For the full forker walkthrough — creating your own stack, wiring CI/CD, seeding data — see the **Fork & deploy to your own Azure** section in the project root `README.md`.

## Quick reference

```bash
cd infra
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# First time on a stack:
pulumi stack init <stack-name>
# Set encrypted config (prompts for a passphrase).
# Note: the Pulumi program reads kebab-case keys (e.g. `anthropic-api-key`, not `anthropicApiKey`).
pulumi config set --secret anthropic-api-key  "sk-ant-..."
pulumi config set --secret google-api-key     "..."
pulumi config set --secret openai-api-key     "sk-..."
pulumi config set --secret elevenlabs-api-key "..."   # optional; TTS only
pulumi config set --secret django-secret-key  "$(python -c 'import secrets; print(secrets.token_urlsafe(64))')"
pulumi config set --secret db-password        "$(openssl rand -base64 32 | tr -d '=+/')"
pulumi config set azure-native:subscriptionId "$(az account show --query id -o tsv)"
pulumi config set azure-native:location centralus

# Plan + apply:
pulumi preview
pulumi up
```

## Existing stacks

- `pixel` — upstream production on Pixel Design Labs subscription. Do not target unless you're on that team.
- `dev` — deprecated old sponsorship subscription.

## Common operations

```bash
# Check what's deployed
pulumi stack output

# Update Container App image (CI does this automatically on push to main)
az containerapp update --name <app> --resource-group <rg> --image <acr>.azurecr.io/aitutor:<tag>

# Rollback to previous revision
az containerapp revision list -n <app> -g <rg> -o table
az containerapp revision activate --revision <prev> --app <app> --resource-group <rg>

# Tail logs
az monitor log-analytics query \
  --workspace <workspace-id> \
  --analytics-query "ContainerAppConsoleLogs_CL | where ContainerAppName_s == '<app>' | order by TimeGenerated desc | take 100"
```

## Don'ts

- Don't run `pulumi destroy` on a live stack without confirming with the team
- Don't commit the Pulumi passphrase to git — store in a password manager
- Don't change subscriptions mid-stack — create a new stack instead
- Don't hardcode the Container App domain (`env.default_domain` from the Pulumi program is the source of truth)
