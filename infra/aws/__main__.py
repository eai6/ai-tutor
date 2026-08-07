"""AI Tutor — AWS infrastructure.

Runs ALONGSIDE the Azure stack in ../. Azure serves live users and is not
touched by anything here; this builds a second, parallel deployment of the same
container image.

ECS is deliberately absent. `iam:PassRole` is denied for this account's SSO
role, and a Fargate task definition cannot omit an execution role — so task
definitions and services cannot be created until the grant in
docs/aws-iam-access-request.md lands. Everything below deploys without it, so
the foundation can be stood up and verified in the meantime.

    cd infra/aws
    python -m venv venv && ./venv/bin/pip install -r requirements.txt
    pulumi stack init dev
    pulumi config set aws:region us-east-1
    pulumi up
"""
from __future__ import annotations

import pulumi
import pulumi_aws as aws

from components.network import create_network
from components.storage import create_storage
from components.data import create_data
from components.edge import create_edge

config = pulumi.Config()
stack = pulumi.get_stack()
prefix = f"aitutor-{stack}"

region = aws.config.region or "us-east-1"
account_id = aws.get_caller_identity().account_id

tags = {
    "Project": "ai-tutor",
    "Stack": stack,
    "ManagedBy": "pulumi",
    "Repo": "ai-tutor/infra/aws",
}

# Two AZs: the ALB requires at least two subnets in different zones.
azs = config.get_object("availability-zones") or [f"{region}a", f"{region}b"]

db_instance_class = config.get("db-instance-class") or "db.t4g.medium"
db_storage_gb = config.get_int("db-storage-gb") or 50
waf_block_mode = config.get_bool("waf-block-mode") or False

network = create_network(prefix, azs, region, tags)
storage = create_storage(prefix, account_id, tags)
data = create_data(
    prefix,
    network.private_subnet_ids,
    network.rds_sg.id,
    db_instance_class,
    db_storage_gb,
    tags,
    config,
)
edge = create_edge(
    prefix,
    network.vpc.id,
    network.public_subnet_ids,
    network.alb_sg.id,
    waf_block_mode,
    tags,
)

# ECS cluster is unblocked (only task definitions need PassRole), so it is
# created now — the service slots into it once IAM lands.
cluster = aws.ecs.Cluster(
    f"{prefix}-cluster",
    name=f"{prefix}-cluster",
    settings=[aws.ecs.ClusterSettingArgs(name="containerInsights", value="disabled")],
    tags={**tags, "Name": f"{prefix}-cluster"},
)

log_group = aws.cloudwatch.LogGroup(
    f"{prefix}-logs",
    name=f"/ecs/{prefix}",
    retention_in_days=30,  # matches the Azure Log Analytics retention
    tags=tags,
)

# ── Outputs ────────────────────────────────────────────────────────────────
pulumi.export("alb_url", edge.alb.dns_name.apply(lambda d: f"http://{d}"))
pulumi.export("alb_arn", edge.alb.arn)
pulumi.export("target_group_arn", edge.target_group.arn)
pulumi.export("ecr_repository_url", storage.ecr_repo.repository_url)
pulumi.export("ecs_cluster_name", cluster.name)
pulumi.export("log_group_name", log_group.name)
pulumi.export("media_bucket", storage.media_bucket.bucket)
pulumi.export("db_endpoint", data.instance.address)
pulumi.export("vpc_id", network.vpc.id)
pulumi.export("private_subnet_ids", network.private_subnet_ids)
pulumi.export("tasks_security_group_id", network.tasks_sg.id)
pulumi.export(
    "secret_arns",
    {
        "database-url": data.database_url_secret.arn,
        **{k: v.arn for k, v in data.app_secrets.items()},
    },
)

# The exact environment the ECS task definition must supply. Kept here rather
# than in prose so it cannot drift from what the app actually reads — every key
# below is consumed by config/settings.py or apps/dashboard/job_dispatch.py.
task_environment = (
    pulumi.Output.all(
        storage.media_bucket.bucket,
        network.private_subnet_ids,
        network.tasks_sg.id,
        cluster.name,
        edge.alb.dns_name,
    ).apply(
        lambda a: {
            "DEBUG": "False",
            "AWS_REGION": region,
            "AWS_MEDIA_BUCKET": a[0],
            "AWS_MEDIA_REGION": region,
            "AWS_SES_REGION": region,
            # Set once an SES identity is verified; absent means console email.
            "AWS_SES_SENDER": "",
            "ECS_CLUSTER": a[3],
            "ECS_MATERIAL_TASK_DEFINITION": f"{prefix}-material",
            "ECS_MATERIAL_CONTAINER_NAME": "material-processor",
            "ECS_SUBNETS": ",".join(a[1]),
            "ECS_SECURITY_GROUPS": a[2],
            # No TLS yet: browsers refuse Secure cookies over plain HTTP, which
            # would silently bounce every login back to the login page.
            "HTTPS_EDGE": "false",
            "ALLOWED_HOSTS": "*",
            "CSRF_TRUSTED_ORIGINS": f"http://{a[4]}",
            "EMBEDDING_BACKEND": "local",
        }
    )
)
pulumi.export("task_environment", task_environment)

# ── ECS, gated on the IAM grant ────────────────────────────────────────────
# Off by default: iam:PassRole is denied, and a Fargate task definition cannot
# omit an execution role. Once the grant in docs/aws-iam-access-request.md
# lands:  pulumi config set enable-ecs true && pulumi up
enable_ecs = config.get_bool("enable-ecs") or False

if enable_ecs:
    from components.compute import create_compute

    image_tag = config.get("image-tag") or "latest"
    compute = create_compute(
        prefix,
        cluster=cluster,
        image=storage.ecr_repo.repository_url.apply(lambda u: f"{u}:{image_tag}"),
        log_group=log_group,
        media_bucket=storage.media_bucket,
        secret_arns={
            "database-url": data.database_url_secret.arn,
            **{k: v.arn for k, v in data.app_secrets.items()},
        },
        task_environment=task_environment,
        private_subnet_ids=network.private_subnet_ids,
        tasks_sg_id=network.tasks_sg.id,
        target_group=edge.target_group,
        region=region,
        account_id=account_id,
        min_tasks=config.get_int("min-tasks") or 1,
        max_tasks=config.get_int("max-tasks") or 4,
        tags=tags,
    )
    pulumi.export("ecs_service_name", compute.service.name)
    pulumi.export("migrate_task_definition", compute.migrate_task_definition.family)
    pulumi.export("material_task_definition", compute.material_task_definition.family)
else:
    pulumi.export(
        "ecs_status",
        "ECS is written but not deployed (enable-ecs=false). Deploy it in this "
        "order — the service pulls <ecr>/aitutor:<tag>, so an image must exist "
        "first or tasks crash-loop on ImagePullFailure: "
        "(1) build and push an image to the ecr_repository_url above, "
        "(2) pulumi config set --secret <key> for the app secrets, "
        "(3) pulumi config set enable-ecs true && pulumi up.",
    )
