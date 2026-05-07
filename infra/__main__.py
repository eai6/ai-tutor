"""
AI Tutor – Pulumi infrastructure for Azure Container Apps.

Resources created:
  1. Resource Group
  2. Log Analytics Workspace
  3. Azure Container Registry (ACR)
  4. Container Apps Environment
  5. Storage Account + File Share (media / ChromaDB)
  6. PostgreSQL Flexible Server + Database
  7. Container App (Django)
  8. Email Communication Service + Domain + Communication Service
     (transactional email — password reset, etc.)
"""

import pulumi
from pulumi import Config, Output
import pulumi_azure_native as azure_native
from pulumi_azure_native import (
    resources,
    operationalinsights,
    containerregistry,
    app,
    storage,
    dbforpostgresql,
    communication,
)

config = Config("aitutor")
az_config = Config("azure-native")
stack = pulumi.get_stack()
location = az_config.require("location")
custom_domain = config.get("custom-domain")  # e.g. "ai-tutor.wbg.edwardamoah.com"

# Email custom domain — must be a subdomain you control DNS for.
# Default: derived from custom-domain ("mail.<custom-domain>") if set.
email_domain = config.get("email-domain") or (
    f"mail.{custom_domain}" if custom_domain else None
)
# Sender address (the From: of outgoing transactional mail).
email_sender_username = config.get("email-sender-username") or "noreply"
email_from_display_name = config.get("email-from-display-name") or "AI Tutor"

# ── Secrets from Pulumi config ──────────────────────────────────────────────
db_password = config.require_secret("db-password")
django_secret_key = config.require_secret("django-secret-key")
anthropic_api_key = config.require_secret("anthropic-api-key")
openai_api_key = config.require_secret("openai-api-key")
google_api_key = config.require_secret("google-api-key")
elevenlabs_api_key = config.require_secret("elevenlabs-api-key")

# ── 1. Resource Group ───────────────────────────────────────────────────────
rg = resources.ResourceGroup(
    f"aitutor-{stack}-rg",
    resource_group_name=f"aitutor-{stack}-rg",
    location=location,
)

# ── 2. Log Analytics Workspace ──────────────────────────────────────────────
log_workspace = operationalinsights.Workspace(
    f"aitutor-{stack}-logs",
    workspace_name=f"aitutor-{stack}-logs",
    resource_group_name=rg.name,
    location=rg.location,
    sku=operationalinsights.WorkspaceSkuArgs(name="PerGB2018"),
    retention_in_days=30,
)

log_shared_keys = pulumi.Output.all(rg.name, log_workspace.name).apply(
    lambda args: operationalinsights.get_shared_keys(
        resource_group_name=args[0],
        workspace_name=args[1],
    )
)

# ── 3. Azure Container Registry ────────────────────────────────────────────
acr_name = f"aitutor{stack}acr"
acr = containerregistry.Registry(
    acr_name,
    registry_name=acr_name,
    resource_group_name=rg.name,
    location=rg.location,
    sku=containerregistry.SkuArgs(name="Basic"),
    admin_user_enabled=True,
)

acr_credentials = pulumi.Output.all(rg.name, acr.name).apply(
    lambda args: containerregistry.list_registry_credentials(
        resource_group_name=args[0],
        registry_name=args[1],
    )
)

# ── 4. Container Apps Environment ───────────────────────────────────────────
env = app.ManagedEnvironment(
    f"aitutor-{stack}-env",
    environment_name=f"aitutor-{stack}-env",
    resource_group_name=rg.name,
    location=rg.location,
    app_logs_configuration=app.AppLogsConfigurationArgs(
        destination="log-analytics",
        log_analytics_configuration=app.LogAnalyticsConfigurationArgs(
            customer_id=log_workspace.customer_id,
            shared_key=log_shared_keys.apply(lambda k: k.primary_shared_key),
        ),
    ),
    workload_profiles=[
        app.WorkloadProfileArgs(
            name="dedicated-d4",
            workload_profile_type="D4",
            minimum_count=1,
            maximum_count=2,
        ),
    ],
)

# ── 4b. Managed Certificate (for custom domain) ──────────────────────────
managed_cert = None
if custom_domain:
    managed_cert = app.ManagedCertificate(
        f"{custom_domain}-cert",
        managed_certificate_name=f"{custom_domain}-aitutor--260302081454",
        environment_name=env.name,
        resource_group_name=rg.name,
        location=rg.location,
        properties=app.ManagedCertificatePropertiesArgs(
            subject_name=custom_domain,
            domain_control_validation=app.ManagedCertificateDomainControlValidation.CNAME,
        ),
    )

# ── 5. Storage Account + File Share ─────────────────────────────────────────
storage_account_name = f"aitutor{stack}sa"
sa = storage.StorageAccount(
    storage_account_name,
    account_name=storage_account_name,
    resource_group_name=rg.name,
    location=rg.location,
    sku=storage.SkuArgs(name=storage.SkuName.STANDARD_LRS),
    kind=storage.Kind.STORAGE_V2,
)

file_share = storage.FileShare(
    "media",
    share_name="media",
    account_name=sa.name,
    resource_group_name=rg.name,
    # 2026-05-05: bumped 5 → 100 GiB after generated images filled
    # the share and image generation started failing with errno 28.
    # Long-term plan: migrate generated images to Azure Blob (see
    # memory/azure_blob_storage_plan.md) — keeps the platform
    # portable to other clouds + decouples from filesystem quotas.
    share_quota=100,  # 100 GiB
)

storage_keys = pulumi.Output.all(rg.name, sa.name).apply(
    lambda args: storage.list_storage_account_keys(
        resource_group_name=args[0],
        account_name=args[1],
    )
)
storage_key = storage_keys.apply(lambda k: k.keys[0].value)

# Link storage to Container Apps Environment
env_storage = app.ManagedEnvironmentsStorage(
    f"aitutor-{stack}-env-storage",
    storage_name="mediastorage",
    environment_name=env.name,
    resource_group_name=rg.name,
    properties=app.ManagedEnvironmentStoragePropertiesArgs(
        azure_file=app.AzureFilePropertiesArgs(
            account_name=sa.name,
            account_key=storage_key,
            share_name=file_share.name,
            access_mode=app.AccessMode.READ_WRITE,
        ),
    ),
)

# ── 6. PostgreSQL Flexible Server ───────────────────────────────────────────
pg_server_name = f"aitutor-{stack}-pg"
pg_server = dbforpostgresql.Server(
    pg_server_name,
    server_name=pg_server_name,
    resource_group_name=rg.name,
    location=rg.location,
    version=dbforpostgresql.PostgresMajorVersion.POSTGRES_MAJOR_VERSION_16,
    administrator_login="aitutoradmin",
    administrator_login_password=db_password,
    storage=dbforpostgresql.StorageArgs(storage_size_gb=32),
    sku=dbforpostgresql.SkuArgs(
        name="Standard_B1ms",
        tier=dbforpostgresql.SkuTier.BURSTABLE,
    ),
)

pg_db = dbforpostgresql.Database(
    "aitutor",
    database_name="aitutor",
    server_name=pg_server.name,
    resource_group_name=rg.name,
)

# Allow Azure services to connect
pg_firewall = dbforpostgresql.FirewallRule(
    "allow-azure-services",
    firewall_rule_name="AllowAzureServices",
    server_name=pg_server.name,
    resource_group_name=rg.name,
    start_ip_address="0.0.0.0",
    end_ip_address="0.0.0.0",
)

# Build DATABASE_URL from components
database_url = Output.all(db_password, pg_server.fully_qualified_domain_name).apply(
    lambda args: f"postgres://aitutoradmin:{args[0]}@{args[1]}:5432/aitutor?sslmode=require"
)

# ── 7. Email Communication Service ─────────────────────────────────────────
# Three resources cooperate here:
#   - EmailService:        Azure-managed SMTP infrastructure
#   - Domain:              the custom subdomain (e.g. mail.example.com)
#                          Set to CustomerManaged so emails come from
#                          a verified domain you own.
#   - CommunicationService: the operational endpoint — gives us the
#                          connection string Django uses to send.
#
# Two-phase setup:
#   1. `pulumi up` creates the resources. Domain returns its required
#      DNS records as `verification_records` output.
#   2. You add those DNS records at your registrar.
#   3. Run `az communication email domain initiate-verification`
#      for each record kind (Domain, SPF, DKIM, DKIM2, DMARC).
#   4. Once Domain shows verified, sending works.
#
# The Container App env vars are wired in from `pulumi up` step 1 so
# Django boots with the right backend; sends just fail until DNS is
# verified.
acs_connection_string = None  # filled in if ACS is configured
acs_sender_address = None     # filled in if email_domain is set
email_dns_outputs = {}        # surfaced to the user via pulumi stack output

if email_domain:
    email_service = communication.EmailService(
        f"aitutor-{stack}-email",
        email_service_name=f"aitutor-{stack}-email",
        resource_group_name=rg.name,
        # The Microsoft.Communication/EmailServices resource type is only
        # provisioned in `global`. The `data_location` field is what
        # actually controls where the data lives.
        location="global",
        data_location="United States",
    )

    email_domain_resource = communication.Domain(
        f"aitutor-{stack}-email-domain",
        domain_name=email_domain,
        email_service_name=email_service.name,
        resource_group_name=rg.name,
        location="global",  # required by ACS Domain
        domain_management="CustomerManaged",
        # Default user engagement tracking off (we're sending
        # transactional, not marketing).
        user_engagement_tracking="Disabled",
    )

    comm_service = communication.CommunicationService(
        f"aitutor-{stack}-comm",
        communication_service_name=f"aitutor-{stack}-comm",
        resource_group_name=rg.name,
        location="global",
        data_location="United States",
        linked_domains=[email_domain_resource.id],
    )

    # Build connection string from primary key.
    keys = communication.list_communication_service_keys_output(
        communication_service_name=comm_service.name,
        resource_group_name=rg.name,
    )
    acs_connection_string = keys.primary_connection_string
    acs_sender_address = Output.concat(email_sender_username, "@", email_domain)

    # Emit DNS instructions as stack outputs.
    email_dns_outputs = {
        "email_domain": email_domain,
        "email_sender_address": acs_sender_address,
        "email_verification_records": email_domain_resource.verification_records,
        "email_setup_help": (
            "1) Add the DNS records above (Domain TXT, SPF TXT, DKIM CNAMEs, "
            "DMARC TXT) at your DNS provider. "
            "2) Run: az communication email domain initiate-verification "
            f"-g {rg.name} --email-service-name aitutor-{stack}-email "
            f"--name {email_domain} --verification-type {{Domain|SPF|DKIM|DKIM2|DMARC}}. "
            "3) Wait for `az ... show` to show all five Verified, then test."
        ),
    }

# ── 8. Container App ───────────────────────────────────────────────────────
container_app_name = f"aitutor-{stack}-app"
image = acr.login_server.apply(lambda s: f"{s}/aitutor:latest")

container_app = app.ContainerApp(
    container_app_name,
    container_app_name=container_app_name,
    resource_group_name=rg.name,
    managed_environment_id=env.id,
    workload_profile_name="dedicated-d4",
    configuration=app.ConfigurationArgs(
        ingress=app.IngressArgs(
            external=True,
            target_port=8000,
            transport=app.IngressTransportMethod.AUTO,
            custom_domains=[
                app.CustomDomainArgs(
                    name=custom_domain,
                    certificate_id=managed_cert.id,
                    binding_type=app.BindingType.SNI_ENABLED,
                ),
            ] if custom_domain and managed_cert else None,
        ),
        registries=[
            app.RegistryCredentialsArgs(
                server=acr.login_server,
                username=acr_credentials.apply(lambda c: c.username),
                password_secret_ref="acr-password",
            ),
        ],
        secrets=[
            app.SecretArgs(name="acr-password", value=acr_credentials.apply(lambda c: c.passwords[0].value)),
            app.SecretArgs(name="database-url", value=database_url),
            app.SecretArgs(name="django-secret-key", value=django_secret_key),
            app.SecretArgs(name="anthropic-api-key", value=anthropic_api_key),
            app.SecretArgs(name="openai-api-key", value=openai_api_key),
            app.SecretArgs(name="google-api-key", value=google_api_key),
            app.SecretArgs(name="elevenlabs-api-key", value=elevenlabs_api_key),
        ] + ([
            app.SecretArgs(
                name="acs-connection-string",
                value=acs_connection_string,
            ),
        ] if acs_connection_string is not None else []),
    ),
    template=app.TemplateArgs(
        containers=[
            app.ContainerArgs(
                name="aitutor",
                image=image,
                resources=app.ContainerResourcesArgs(
                    cpu=4.0,
                    memory="8Gi",
                ),
                env=[
                    app.EnvironmentVarArgs(name="DATABASE_URL", secret_ref="database-url"),
                    app.EnvironmentVarArgs(name="SECRET_KEY", secret_ref="django-secret-key"),
                    app.EnvironmentVarArgs(name="ANTHROPIC_API_KEY", secret_ref="anthropic-api-key"),
                    app.EnvironmentVarArgs(name="OPENAI_API_KEY", secret_ref="openai-api-key"),
                    app.EnvironmentVarArgs(name="GOOGLE_API_KEY", secret_ref="google-api-key"),
                    app.EnvironmentVarArgs(name="DEBUG", value="False"),
                    app.EnvironmentVarArgs(name="EMBEDDING_BACKEND", value="local"),
                    app.EnvironmentVarArgs(name="VECTORDB_ROOT", value="/tmp/vectordb"),
                    app.EnvironmentVarArgs(
                        name="ALLOWED_HOSTS",
                        value="*",
                    ),
                    app.EnvironmentVarArgs(
                        name="CSRF_TRUSTED_ORIGINS",
                        value=Output.concat(
                            "https://", container_app_name, ".", env.default_domain,
                            f",https://{custom_domain}" if custom_domain else "",
                        ),
                    ),
                    app.EnvironmentVarArgs(name="TTS_BACKEND", value="elevenlabs"),
                    app.EnvironmentVarArgs(name="STT_BACKEND", value="elevenlabs"),
                    app.EnvironmentVarArgs(name="ELEVENLABS_API_KEY", secret_ref="elevenlabs-api-key"),
                    app.EnvironmentVarArgs(name="POSTHOG_DISABLED", value="true"),
                    app.EnvironmentVarArgs(name="INSTRUCTOR_TELEMETRY", value="false"),
                ] + ([
                    app.EnvironmentVarArgs(
                        name="AZURE_COMMUNICATION_CONNECTION_STRING",
                        secret_ref="acs-connection-string",
                    ),
                    app.EnvironmentVarArgs(
                        name="AZURE_COMMUNICATION_SENDER_ADDRESS",
                        value=acs_sender_address,
                    ),
                    app.EnvironmentVarArgs(
                        name="DEFAULT_FROM_EMAIL",
                        value=Output.concat(email_from_display_name, " <", acs_sender_address, ">"),
                    ),
                ] if acs_connection_string is not None else []),
                volume_mounts=[
                    app.VolumeMountArgs(
                        volume_name="media-volume",
                        mount_path="/app/media",
                    ),
                ],
                probes=[
                    app.ContainerAppProbeArgs(
                        type=app.Type.LIVENESS,
                        http_get=app.ContainerAppProbeHttpGetArgs(
                            path="/health/",
                            port=8000,
                        ),
                        period_seconds=60,
                        failure_threshold=5,
                        timeout_seconds=10,
                    ),
                    app.ContainerAppProbeArgs(
                        type=app.Type.READINESS,
                        http_get=app.ContainerAppProbeHttpGetArgs(
                            path="/health/",
                            port=8000,
                        ),
                        period_seconds=30,
                        failure_threshold=3,
                        timeout_seconds=10,
                    ),
                ],
            ),
        ],
        scale=app.ScaleArgs(
            # Pilot training (2026-05-07): scale out for read traffic.
            # ChromaDB lives at VECTORDB_ROOT=/tmp/vectordb — each
            # replica gets its own copy seeded from the Azure Files
            # mount on startup (Dockerfile CMD `cp` step). That makes
            # multi-replica READS safe (every replica has its own
            # SQLite). WRITES to the vectordb (curriculum reindexing,
            # teaching-materials upload) MUST be run from a single-
            # replica mode — bring max_replicas back to 1 before any
            # large reindex, or run the write via a separate one-off
            # job. For the pilot the only writes happen during admin
            # content uploads, which are infrequent and serialisable.
            min_replicas=1,
            max_replicas=4,
            rules=[
                app.ScaleRuleArgs(
                    name="http-concurrency",
                    http=app.HttpScaleRuleArgs(
                        metadata={
                            # Spawn a new replica when each existing
                            # replica is handling more than ~12
                            # concurrent in-flight HTTP requests.
                            # With gunicorn's 4 workers × 4 threads
                            # = 16 concurrent capacity, 12 leaves
                            # headroom before queuing.
                            "concurrentRequests": "12",
                        },
                    ),
                ),
            ],
        ),
        volumes=[
            app.VolumeArgs(
                name="media-volume",
                storage_type=app.StorageType.AZURE_FILE,
                storage_name="mediastorage",
            ),
        ],
    ),
    opts=pulumi.ResourceOptions(depends_on=[env_storage, pg_firewall] + ([managed_cert] if managed_cert else [])),
)

# ── Exports ─────────────────────────────────────────────────────────────────
pulumi.export("app_url", container_app.configuration.apply(
    lambda c: f"https://{c.ingress.fqdn}" if c and c.ingress and c.ingress.fqdn else "pending"
))
pulumi.export("acr_login_server", acr.login_server)
pulumi.export("resource_group", rg.name)

# Email outputs — see DNS records the user must add at their registrar.
for key, value in email_dns_outputs.items():
    pulumi.export(key, value)
