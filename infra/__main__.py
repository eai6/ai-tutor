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
)

config = Config("aitutor")
az_config = Config("azure-native")
stack = pulumi.get_stack()
location = az_config.require("location")

# ── Secrets from Pulumi config ──────────────────────────────────────────────
db_password = config.require_secret("db-password")
django_secret_key = config.require_secret("django-secret-key")
anthropic_api_key = config.require_secret("anthropic-api-key")
openai_api_key = config.require_secret("openai-api-key")
google_api_key = config.require_secret("google-api-key")

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
            maximum_count=1,
        ),
    ],
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
    share_quota=5,  # 5 GiB – increase as needed
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

# ── 7. Container App ───────────────────────────────────────────────────────
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
        ],
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
                        value=Output.concat("https://", container_app_name, ".", env.default_domain),
                    ),
                ],
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
            min_replicas=1,
            max_replicas=1,  # Keep at 1 for ChromaDB file-based storage
        ),
        volumes=[
            app.VolumeArgs(
                name="media-volume",
                storage_type=app.StorageType.AZURE_FILE,
                storage_name="mediastorage",
            ),
        ],
    ),
    opts=pulumi.ResourceOptions(depends_on=[env_storage, pg_firewall]),
)

# ── Exports ─────────────────────────────────────────────────────────────────
pulumi.export("app_url", container_app.configuration.apply(
    lambda c: f"https://{c.ingress.fqdn}" if c and c.ingress and c.ingress.fqdn else "pending"
))
pulumi.export("acr_login_server", acr.login_server)
pulumi.export("resource_group", rg.name)
