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
    authorization,
)

config = Config("aitutor")
az_config = Config("azure-native")
stack = pulumi.get_stack()
location = az_config.require("location")

# Custom domains bound to the Container App. Two config keys are
# accepted for backwards compatibility:
#   - `custom-domains` (list)  — preferred. JSON-encoded list of
#     hostnames, e.g. '["ai-tutor.wbg.edwardamoah.com", "www.seselai.sc"]'
#   - `custom-domain` (string) — legacy single-value form. Auto-promoted
#     to a one-element list if the list form isn't set.
# Both empty = no custom domain (Azure-generated URL only).
custom_domains: list[str] = list(
    config.get_object("custom-domains") or []
)
_legacy_single = config.get("custom-domain")
if _legacy_single and _legacy_single not in custom_domains:
    custom_domains.append(_legacy_single)
# Order is meaningful: the first entry is the "primary" domain used to
# derive the email subdomain default.
primary_custom_domain = custom_domains[0] if custom_domains else None

# Email custom domain — must be a subdomain you control DNS for.
# Default: derived from the primary custom domain ("mail.<primary>") if set.
email_domain = config.get("email-domain") or (
    f"mail.{primary_custom_domain}" if primary_custom_domain else None
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

# ── Sizing knobs (override per-stack for cheaper non-prod) ────────────────
# Each knob falls back to the current prod default so the `pixel` stack
# keeps its existing shape without any new config keys. Override in
# Pulumi.<stack>.yaml to right-size staging or dev:
#   pulumi config set aitutor:workload-profile-type D2
#   pulumi config set aitutor:container-cpu 2.0
#   pulumi config set aitutor:container-memory 4Gi
#   pulumi config set aitutor:max-replicas 1
#   pulumi config set aitutor:file-share-quota-gb 20
#   pulumi config set aitutor:postgres-sku Standard_B1ms
workload_profile_type = config.get("workload-profile-type") or "D4"
# Naming convention: "Consumption" for serverless; "dedicated-d4" / etc.
# for reserved-capacity profiles. The name must match what's used as
# `workload_profile_name` on the ContainerApp + Job.
workload_profile_name = (
    "Consumption"
    if workload_profile_type == "Consumption"
    else f"dedicated-{workload_profile_type.lower()}"
)
container_cpu = float(config.get("container-cpu") or "4.0")
container_memory = config.get("container-memory") or "8Gi"
max_replicas = int(config.get("max-replicas") or "4")
min_replicas = int(config.get("min-replicas") or "1")
file_share_quota_gb = int(config.get("file-share-quota-gb") or "100")
postgres_sku = config.get("postgres-sku") or "Standard_B1ms"
postgres_storage_gb = int(config.get("postgres-storage-gb") or "32")

# Material-processor Job sizing — usually the same as the main app's
# burst capacity, but a non-prod stack may not want to run heavy
# textbook OCR at all (set material-job-cpu=0 to skip the Job entirely).
material_job_cpu = float(config.get("material-job-cpu") or "2.0")
material_job_memory = config.get("material-job-memory") or "4Gi"
provision_material_job = material_job_cpu > 0

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
        # Consumption profile = serverless (true scale-to-zero, $0
        # when idle). Dedicated D* profiles reserve capacity and need
        # minimum_count/maximum_count. The fields are NOT accepted by
        # the Consumption SKU, so omit them in that branch.
        app.WorkloadProfileArgs(
            name=workload_profile_name,
            workload_profile_type=workload_profile_type,
            **(
                {"minimum_count": 1, "maximum_count": 2}
                if workload_profile_type != "Consumption"
                else {}
            ),
        ),
    ],
)

# ── 4b. Managed Certificates (one per custom domain) ─────────────────────
# Free auto-renewing certs from Azure. The Azure-resource-name (the
# `managed_certificate_name` arg) needs to be unique within the
# environment AND stable per-domain so Pulumi doesn't churn the cert
# on every run. For the original single-domain setup we used a
# timestamped suffix; preserving it here for that one domain so the
# existing cert isn't recreated. New domains get a clean
# `<sanitized-domain>-cert` name.
_LEGACY_CERT_NAMES = {
    "ai-tutor.wbg.edwardamoah.com": "ai-tutor.wbg.edwardamoah.com-aitutor--260302081454",
}


def _cert_name(domain: str) -> str:
    if domain in _LEGACY_CERT_NAMES:
        return _LEGACY_CERT_NAMES[domain]
    # Azure resource names allow dots; using the raw domain keeps the
    # name readable in the portal.
    return f"{domain}-cert"


managed_certs: dict[str, app.ManagedCertificate] = {}
for domain in custom_domains:
    managed_certs[domain] = app.ManagedCertificate(
        f"{domain}-cert",
        managed_certificate_name=_cert_name(domain),
        environment_name=env.name,
        resource_group_name=rg.name,
        location=rg.location,
        properties=app.ManagedCertificatePropertiesArgs(
            subject_name=domain,
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
    share_quota=file_share_quota_gb,
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
    storage=dbforpostgresql.StorageArgs(storage_size_gb=postgres_storage_gb),
    sku=dbforpostgresql.SkuArgs(
        name=postgres_sku,
        # Burstable for B-series, GeneralPurpose for D-series; tier
        # inferred from the SKU name prefix.
        tier=(
            dbforpostgresql.SkuTier.BURSTABLE
            if postgres_sku.startswith("Standard_B")
            else dbforpostgresql.SkuTier.GENERAL_PURPOSE
        ),
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

# Shared secret + env config — used by BOTH the main Container App and the
# material-processor Container Apps Job. Keeping the lists factored out
# means a new env var or secret only has to be added once. Conditional
# ACS-email blocks are appended inline at the call site (so we don't carry
# a stale subset when ACS isn't configured).
shared_secrets = [
    app.SecretArgs(name="acr-password", value=acr_credentials.apply(lambda c: c.passwords[0].value)),
    app.SecretArgs(name="database-url", value=database_url),
    app.SecretArgs(name="django-secret-key", value=django_secret_key),
    app.SecretArgs(name="anthropic-api-key", value=anthropic_api_key),
    app.SecretArgs(name="openai-api-key", value=openai_api_key),
    app.SecretArgs(name="google-api-key", value=google_api_key),
    app.SecretArgs(name="elevenlabs-api-key", value=elevenlabs_api_key),
] + ([
    app.SecretArgs(name="acs-connection-string", value=acs_connection_string),
] if acs_connection_string is not None else [])

shared_registries = [
    app.RegistryCredentialsArgs(
        server=acr.login_server,
        username=acr_credentials.apply(lambda c: c.username),
        password_secret_ref="acr-password",
    ),
]


def _build_app_env_vars(*, csrf_origins=None, include_job_dispatch_env=False):
    """Return the env-var list shared between the main app and material job.

    csrf_origins: only meaningful for the main app (the Job has no ingress).
        Pass None for the Job and the var is omitted.
    include_job_dispatch_env: only the main app needs Azure SDK env vars to
        dispatch jobs (apps/dashboard/job_dispatch.py reads these).
        The Job itself doesn't dispatch other jobs, so omit there.
    """
    env_vars = [
        app.EnvironmentVarArgs(name="DATABASE_URL", secret_ref="database-url"),
        app.EnvironmentVarArgs(name="SECRET_KEY", secret_ref="django-secret-key"),
        app.EnvironmentVarArgs(name="ANTHROPIC_API_KEY", secret_ref="anthropic-api-key"),
        app.EnvironmentVarArgs(name="OPENAI_API_KEY", secret_ref="openai-api-key"),
        app.EnvironmentVarArgs(name="GOOGLE_API_KEY", secret_ref="google-api-key"),
        app.EnvironmentVarArgs(name="DEBUG", value="False"),
        app.EnvironmentVarArgs(name="EMBEDDING_BACKEND", value="local"),
        app.EnvironmentVarArgs(name="VECTORDB_ROOT", value="/tmp/vectordb"),
        app.EnvironmentVarArgs(name="ALLOWED_HOSTS", value="*"),
        app.EnvironmentVarArgs(name="TTS_BACKEND", value="elevenlabs"),
        app.EnvironmentVarArgs(name="STT_BACKEND", value="elevenlabs"),
        app.EnvironmentVarArgs(name="ELEVENLABS_API_KEY", secret_ref="elevenlabs-api-key"),
        app.EnvironmentVarArgs(name="POSTHOG_DISABLED", value="true"),
        app.EnvironmentVarArgs(name="INSTRUCTOR_TELEMETRY", value="false"),
    ]
    # Simple-tutor engine + exit-ticket tunables — opt-in via Pulumi
    # config so prod stays on the legacy ConversationalTutor until
    # explicitly flipped. Staging sets these in Pulumi.staging.yaml
    # so `pulumi up` doesn't silently wipe the manual `az containerapp
    # update --set-env-vars` we used during M12 rollout.
    _simple_tutor = config.get("simple-tutor-engine")
    if _simple_tutor:
        env_vars.append(
            app.EnvironmentVarArgs(name="SIMPLE_TUTOR_ENGINE", value=_simple_tutor),
        )
    _exit_ticket_types = config.get("exit-ticket-types")
    if _exit_ticket_types:
        env_vars.append(
            app.EnvironmentVarArgs(name="EXIT_TICKET_TYPES", value=_exit_ticket_types),
        )
    _exit_ticket_max = config.get("exit-ticket-max-questions")
    if _exit_ticket_max:
        env_vars.append(
            app.EnvironmentVarArgs(
                name="EXIT_TICKET_MAX_QUESTIONS", value=_exit_ticket_max,
            ),
        )
    _tutoring_q_types = config.get("tutoring-question-types")
    if _tutoring_q_types:
        env_vars.append(
            app.EnvironmentVarArgs(
                name="TUTORING_QUESTION_TYPES", value=_tutoring_q_types,
            ),
        )
    # NEW_TUTOR — flips new TutorSessions to Roy's v2 engine
    # (apps/tutoring/v2/routing.py::ensure_engine_version_set). Set on
    # the preview stack to default sessions there to v2; leave unset on
    # staging / prod so they stay on legacy or simple_tutor.
    _new_tutor = config.get("new-tutor")
    if _new_tutor:
        env_vars.append(
            app.EnvironmentVarArgs(name="NEW_TUTOR", value=_new_tutor),
        )
    if include_job_dispatch_env:
        env_vars.extend([
            app.EnvironmentVarArgs(name="AZURE_RESOURCE_GROUP", value=rg.name),
            app.EnvironmentVarArgs(
                name="AZURE_MATERIAL_JOB_NAME",
                value=f"aitutor-{stack}-material-job",
            ),
            app.EnvironmentVarArgs(
                name="AZURE_SUBSCRIPTION_ID",
                value=az_config.require("subscriptionId"),
            ),
        ])
    if csrf_origins is not None:
        env_vars.append(app.EnvironmentVarArgs(name="CSRF_TRUSTED_ORIGINS", value=csrf_origins))
    if acs_connection_string is not None:
        env_vars.extend([
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
        ])
    return env_vars

container_app = app.ContainerApp(
    container_app_name,
    container_app_name=container_app_name,
    resource_group_name=rg.name,
    managed_environment_id=env.id,
    workload_profile_name=workload_profile_name,
    # System-assigned managed identity — used by apps/dashboard/job_dispatch.py
    # to call ARM and start material-processor Job executions. Role assignment
    # below grants this identity the right to start the specific Job.
    identity=app.ManagedServiceIdentityArgs(
        type=app.ManagedServiceIdentityType.SYSTEM_ASSIGNED,
    ),
    configuration=app.ConfigurationArgs(
        ingress=app.IngressArgs(
            external=True,
            target_port=8000,
            transport=app.IngressTransportMethod.AUTO,
            custom_domains=[
                app.CustomDomainArgs(
                    name=domain,
                    certificate_id=managed_certs[domain].id,
                    binding_type=app.BindingType.SNI_ENABLED,
                )
                for domain in custom_domains
            ] if custom_domains else None,
        ),
        registries=shared_registries,
        secrets=shared_secrets,
    ),
    template=app.TemplateArgs(
        containers=[
            app.ContainerArgs(
                name="aitutor",
                image=image,
                resources=app.ContainerResourcesArgs(
                    cpu=container_cpu,
                    memory=container_memory,
                ),
                env=_build_app_env_vars(
                    csrf_origins=Output.concat(
                        "https://", container_app_name, ".", env.default_domain,
                        "".join(f",https://{d}" for d in custom_domains),
                    ),
                    include_job_dispatch_env=True,
                ),
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
            min_replicas=min_replicas,
            max_replicas=max_replicas,
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
    opts=pulumi.ResourceOptions(
        depends_on=[env_storage, pg_firewall] + list(managed_certs.values()),
    ),
)

# ── 9. Container Apps Job — material processor ────────────────────────────
# Runs `python manage.py process_material <upload_id>` for materials with
# >=50 pages (the routing threshold lives in apps.dashboard.material_routing).
# Same image as the main app — every code deploy automatically updates the
# Job's image too, since both reference `aitutor:latest` in ACR. The
# main web app dispatches via `az containerapp job start` (or the Azure
# Management SDK from Python).
#
# Why a Job instead of a daemon thread inside gunicorn:
#   - Survives main-app restarts AND code deploys mid-run
#   - On-demand only — zero idle cost
#   - 24 h replica timeout means we never have to bump the limit for a
#     larger textbook or a temporary rate-limit slowdown
#
# Set aitutor:material-job-cpu = 0 in Pulumi config to skip provisioning
# (e.g. a non-prod stack that doesn't need to handle large textbook OCR).
material_job_name = f"aitutor-{stack}-material-job"
material_job = None
if provision_material_job:
    material_job = app.Job(
        material_job_name,
        job_name=material_job_name,
        resource_group_name=rg.name,
        environment_id=env.id,
        workload_profile_name=workload_profile_name,
        configuration=app.JobConfigurationArgs(
            replica_timeout=86400,         # 24 h hard ceiling — never revisit
            replica_retry_limit=2,         # auto-retry on container restart
            trigger_type=app.TriggerType.MANUAL,
            manual_trigger_config=app.JobConfigurationManualTriggerConfigArgs(
                parallelism=1,
                replica_completion_count=1,
            ),
            registries=shared_registries,
            secrets=shared_secrets,
        ),
        template=app.JobTemplateArgs(
            containers=[
                app.ContainerArgs(
                    name="material-processor",
                    image=image,
                    # Smaller than main app — vision LLM is mostly outbound HTTP
                    # wait, not compute. 2 CPU / 4 Gi is plenty for 5-way
                    # concurrent vision requests on prod; staging can shrink.
                    resources=app.ContainerResourcesArgs(
                        cpu=material_job_cpu,
                        memory=material_job_memory,
                    ),
                    # Default command. Per-execution `args` override (the upload
                    # id) is supplied by the dispatcher (`az containerapp job
                    # start --args ...` or SDK equivalent).
                    command=["python", "manage.py", "process_material"],
                    env=_build_app_env_vars(csrf_origins=None),
                    volume_mounts=[
                        app.VolumeMountArgs(
                            volume_name="media-volume",
                            mount_path="/app/media",
                        ),
                    ],
                ),
            ],
            volumes=[
                app.VolumeArgs(
                    name="media-volume",
                    storage_type=app.StorageType.AZURE_FILE,
                    storage_name="mediastorage",
                ),
            ],
        ),
        opts=pulumi.ResourceOptions(depends_on=[env_storage]),
    )

# Grant the main Container App's managed identity permission to start
# executions of this Job. "Container Apps Jobs Operator" role:
#   roleDefinitionId = b9a307c4-5aa3-4b52-ba60-2b17c136cd7b
# Includes Microsoft.App/jobs/*/read and Microsoft.App/jobs/*/action — the
# /action permission is what allows `jobs/start/action`. Least-privilege
# alternative to "Container Apps Jobs Contributor" which also grants write
# + delete on the Job itself.
#
# NOTE: do NOT use "Container Apps Contributor" (358470bc...) — that role
# only covers `Microsoft.App/containerApps/*`, which is a DIFFERENT
# resource type than `Microsoft.App/jobs/*`. We tripped this on the first
# pulumi up (2026-05-14) — the AuthorizationFailed error told us exactly
# which action wasn't permitted.
#
# Scope is the Job itself (the identity can only touch this specific Job,
# not any other resource in the RG). Skipped when the Job isn't provisioned
# (set aitutor:material-job-cpu = 0 in stack config).
if material_job is not None:
    material_job_role_assignment = authorization.RoleAssignment(
        f"aitutor-{stack}-app-material-job-operator",
        role_assignment_name=Output.all(container_app.id, material_job.id).apply(
            # Stable UUID derived from (principal, scope, role) so re-runs
            # find the same assignment instead of creating duplicates. Bumping
            # the role suffix on intentional role changes (e.g. operator →
            # contributor) forces a new assignment.
            lambda args: __import__("uuid").uuid5(
                __import__("uuid").NAMESPACE_URL,
                f"{args[0]}:{args[1]}:jobs-operator",
            ).hex
        ),
        scope=material_job.id,
        principal_id=container_app.identity.apply(
            lambda i: i.principal_id if i and i.principal_id else ""
        ),
        principal_type=authorization.PrincipalType.SERVICE_PRINCIPAL,
        role_definition_id=Output.concat(
            "/subscriptions/",
            az_config.require("subscriptionId"),
            "/providers/Microsoft.Authorization/roleDefinitions/"
            "b9a307c4-5aa3-4b52-ba60-2b17c136cd7b",   # Container Apps Jobs Operator
        ),
    )

# ── Exports ─────────────────────────────────────────────────────────────────
pulumi.export("app_url", container_app.configuration.apply(
    lambda c: f"https://{c.ingress.fqdn}" if c and c.ingress and c.ingress.fqdn else "pending"
))
pulumi.export("acr_login_server", acr.login_server)
pulumi.export("resource_group", rg.name)

# Custom-domain DNS bootstrap. To bind a new custom domain to the
# Container App, you need TWO DNS records at the domain's authoritative
# zone (BEFORE running `pulumi up` on the new domain):
#
#   1. CNAME  <domain>           → <container app default fqdn>
#   2. TXT    asuid.<domain>    → <custom_domain_verification_id below>
#
# `pulumi up` then provisions the managed cert via DNS validation.
# Without these records the cert never validates and the deploy stalls.
pulumi.export(
    "custom_domain_verification_id",
    env.custom_domain_configuration.apply(
        lambda c: (c.custom_domain_verification_id if c else "(env not provisioned)")
    ),
)
pulumi.export("custom_domains_bound", custom_domains)
pulumi.export("container_app_default_fqdn", container_app.configuration.apply(
    lambda c: c.ingress.fqdn if c and c.ingress and c.ingress.fqdn else "pending"
))
if material_job is not None:
    pulumi.export("material_job_name", material_job.name)

# Email outputs — see DNS records the user must add at their registrar.
for key, value in email_dns_outputs.items():
    pulumi.export(key, value)
