"""RDS PostgreSQL and the Secrets Manager entries the app reads at boot.

Sized a step above the Azure production database on purpose. Azure runs
Standard_B1ms (1 vCore / 2 GiB), which caps at 50 connections — a documented
source of production incidents, because ``/health/`` opens a connection and
returns 503 when it cannot, so exhausting the pool marked every replica
unhealthy at once. db.t4g.medium lifts the ceiling to roughly 450.

Enhanced monitoring is deliberately OFF: it requires ``iam:PassRole`` on
``rds-monitoring-role``, which this account's SSO role does not have. Standard
CloudWatch metrics are unaffected. See docs/aws-iam-access-request.md.
"""
from __future__ import annotations

from dataclasses import dataclass

import pulumi
import pulumi_aws as aws
import pulumi_random as random

DB_NAME = "aitutor"
DB_USER = "aitutoradmin"
DB_PORT = 5432
ENGINE_VERSION = "16"


@dataclass
class Data:
    instance: aws.rds.Instance
    database_url_secret: aws.secretsmanager.Secret
    app_secrets: dict  # logical name -> aws.secretsmanager.Secret


def create_data(
    prefix: str,
    private_subnet_ids,
    rds_sg_id,
    instance_class: str,
    storage_gb: int,
    tags: dict,
    config: pulumi.Config,
) -> Data:
    subnet_group = aws.rds.SubnetGroup(
        f"{prefix}-db-subnets",
        subnet_ids=private_subnet_ids,
        tags={**tags, "Name": f"{prefix}-db-subnets"},
    )

    # Generated rather than configured: nothing outside this stack needs to know
    # it, and it lands in Secrets Manager as part of the DSN below.
    db_password = random.RandomPassword(
        f"{prefix}-db-password",
        length=32,
        special=True,
        # RDS rejects /, @, ", and space in master passwords.
        override_special="!#$%&*()-_=+[]{}<>:?",
    )

    instance = aws.rds.Instance(
        f"{prefix}-db",
        identifier=f"{prefix}-db",
        engine="postgres",
        engine_version=ENGINE_VERSION,
        instance_class=instance_class,
        allocated_storage=storage_gb,
        max_allocated_storage=storage_gb * 4,  # storage autoscaling headroom
        storage_type="gp3",
        storage_encrypted=True,
        db_name=DB_NAME,
        username=DB_USER,
        password=db_password.result,
        port=DB_PORT,
        db_subnet_group_name=subnet_group.name,
        vpc_security_group_ids=[rds_sg_id],
        publicly_accessible=False,
        multi_az=False,  # matches Azure, which has no HA either
        backup_retention_period=14,
        backup_window="03:00-04:00",
        maintenance_window="Mon:04:00-Mon:05:00",
        auto_minor_version_upgrade=True,
        deletion_protection=False,
        skip_final_snapshot=True,
        apply_immediately=True,
        # NOT set: monitoring_interval / monitoring_role_arn. Enhanced
        # monitoring needs iam:PassRole, which is currently denied.
        tags={**tags, "Name": f"{prefix}-db"},
    )

    # pgvector needs no parameter-group work on RDS — `vector` ships in the
    # available extensions, and apps/curriculum/migrations/0029 creates it.
    # (Azure Flexible Server required an azure.extensions allowlist first.)

    database_url = pulumi.Output.all(instance.address, db_password.result).apply(
        # sslmode=require is NOT optional: nothing in config/settings.py enforces
        # TLS, so it comes entirely from this query string.
        lambda a: f"postgres://{DB_USER}:{a[1]}@{a[0]}:{DB_PORT}/{DB_NAME}?sslmode=require"
    )

    database_url_secret = _secret(
        prefix, "database-url", database_url, tags,
        description="Full Postgres DSN including ?sslmode=require",
    )

    # Application secrets. Real values come from Pulumi config; where one is
    # absent a clearly-marked placeholder is stored so the foundation can be
    # deployed before every key is in hand. The app fails loudly on a
    # placeholder rather than half-working.
    app_secrets = {}
    for key, description in (
        ("django-secret-key", "Django SECRET_KEY"),
        ("anthropic-api-key", "Anthropic API key"),
        ("openai-api-key", "OpenAI API key"),
        ("google-api-key", "Google/Gemini API key"),
        ("elevenlabs-api-key", "ElevenLabs API key (TTS/STT)"),
    ):
        value = config.get_secret(key)
        if value is None:
            value = pulumi.Output.secret(f"PLACEHOLDER-set-via-pulumi-config-{key}")
        app_secrets[key] = _secret(prefix, key, value, tags, description=description)

    return Data(
        instance=instance,
        database_url_secret=database_url_secret,
        app_secrets=app_secrets,
    )


def _secret(prefix, name, value, tags, description=""):
    secret = aws.secretsmanager.Secret(
        f"{prefix}-secret-{name}",
        name=f"{prefix}/{name}",
        description=description,
        # Short window so a mistaken destroy can be re-created same-day rather
        # than colliding with a 30-day scheduled deletion.
        recovery_window_in_days=7,
        tags=tags,
    )
    aws.secretsmanager.SecretVersion(
        f"{prefix}-secret-{name}-value",
        secret_id=secret.id,
        secret_string=value,
    )
    return secret
