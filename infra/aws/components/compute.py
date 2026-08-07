"""ECS roles, task definitions, service, and autoscaling.

**Gated behind the `enable-ecs` config flag, default false.** `iam:PassRole` is
denied for this account's SSO role and a Fargate task definition cannot omit an
execution role, so none of this can be created yet. It is written now so that
the moment the grant in docs/aws-iam-access-request.md lands, enabling it is:

    pulumi config set enable-ecs true && pulumi up

Three task definitions share one image:

* **web** — gunicorn only. The Dockerfile CMD also runs migrate and six seed
  commands, which Azure relies on because Container Apps single-revision mode
  serialises it across a rollout. ECS starts every task at once, so the web
  definition overrides `command` to skip that chain entirely.
* **migrate** — runs ops/migrate_and_seed.sh as a one-shot task. CI runs this
  and waits for exit 0 before updating the service.
* **material** — large material processing, started at runtime by the web
  container via ecs:RunTask (apps/dashboard/job_dispatch.py).
"""
from __future__ import annotations

import json
from dataclasses import dataclass

import pulumi
import pulumi_aws as aws

APP_PORT = 8000

# 8 GiB is a floor, not a preference: EMBEDDING_BACKEND=local loads
# all-MiniLM-L6-v2 (~500 MB) into each of gunicorn's four workers. The Azure
# staging stack documents a probe-failure crash loop when this ran on 4 GiB.
WEB_CPU = "4096"
WEB_MEMORY = "8192"
JOB_CPU = "2048"
JOB_MEMORY = "4096"


@dataclass
class Compute:
    service: aws.ecs.Service
    web_task_definition: aws.ecs.TaskDefinition
    migrate_task_definition: aws.ecs.TaskDefinition
    material_task_definition: aws.ecs.TaskDefinition


def create_compute(
    prefix: str,
    *,
    cluster,
    image: str,
    log_group,
    media_bucket,
    secret_arns: dict,
    task_environment: dict,
    private_subnet_ids,
    tasks_sg_id,
    target_group,
    alb,
    region: str,
    account_id: str,
    min_tasks: int,
    max_tasks: int,
    tags: dict,
) -> Compute:
    assume_ecs_tasks = json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"Service": "ecs-tasks.amazonaws.com"},
                    "Action": "sts:AssumeRole",
                }
            ],
        }
    )

    # ── Execution role: what ECS itself uses to start the task ─────────────
    execution_role = aws.iam.Role(
        f"{prefix}-task-execution",
        name=f"{prefix}-task-execution",
        assume_role_policy=assume_ecs_tasks,
        tags=tags,
    )
    aws.iam.RolePolicyAttachment(
        f"{prefix}-task-execution-managed",
        role=execution_role.name,
        policy_arn="arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy",
    )
    # The managed policy covers ECR and CloudWatch Logs but NOT Secrets
    # Manager. Without this the task starts and then dies resolving secrets.
    aws.iam.RolePolicy(
        f"{prefix}-task-execution-secrets",
        role=execution_role.id,
        policy=pulumi.Output.all(*secret_arns.values()).apply(
            lambda arns: json.dumps(
                {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Action": ["secretsmanager:GetSecretValue"],
                            "Resource": list(arns),
                        }
                    ],
                }
            )
        ),
    )

    # ── Task role: the application's own runtime identity ──────────────────
    task_role = aws.iam.Role(
        f"{prefix}-task", name=f"{prefix}-task",
        assume_role_policy=assume_ecs_tasks, tags=tags,
    )
    aws.iam.RolePolicy(
        f"{prefix}-task-policy",
        role=task_role.id,
        policy=pulumi.Output.all(media_bucket.arn, task_role.arn, execution_role.arn).apply(
            lambda a: json.dumps(
                {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Sid": "MediaBucket",
                            "Effect": "Allow",
                            "Action": [
                                "s3:GetObject", "s3:PutObject",
                                "s3:DeleteObject", "s3:ListBucket",
                            ],
                            "Resource": [a[0], f"{a[0]}/*"],
                        },
                        {
                            "Sid": "TransactionalEmail",
                            "Effect": "Allow",
                            "Action": ["ses:SendEmail", "ses:SendRawEmail"],
                            "Resource": "*",
                        },
                        {
                            "Sid": "DispatchMaterialProcessing",
                            "Effect": "Allow",
                            "Action": ["ecs:RunTask", "ecs:DescribeTasks"],
                            "Resource": f"arn:aws:ecs:{region}:{account_id}:task-definition/{prefix}-material:*",
                        },
                        {
                            # The web container starts the material task, so it
                            # passes these roles in turn.
                            "Sid": "PassRolesToTheTasksItStarts",
                            "Effect": "Allow",
                            "Action": "iam:PassRole",
                            "Resource": [a[1], a[2]],
                            "Condition": {
                                "StringEquals": {
                                    "iam:PassedToService": "ecs-tasks.amazonaws.com"
                                }
                            },
                        },
                    ],
                }
            )
        ),
    )

    # Secret env-var name -> Secrets Manager ARN. Ordered explicitly rather than
    # zipped against dict order, so a reordering upstream can't silently wire
    # ANTHROPIC_API_KEY to the database DSN.
    secret_env = [
        ("DATABASE_URL", "database-url"),
        ("SECRET_KEY", "django-secret-key"),
        ("ANTHROPIC_API_KEY", "anthropic-api-key"),
        ("OPENAI_API_KEY", "openai-api-key"),
        ("GOOGLE_API_KEY", "google-api-key"),
        ("ELEVENLABS_API_KEY", "elevenlabs-api-key"),
    ]
    ordered_secret_arns = [secret_arns[key] for _, key in secret_env]

    def _container(name, command, cpu, memory):
        # `image` is an Output (derived from the ECR repo URL) and so are the
        # secret ARNs — every one has to go through Output.all before json.dumps
        # can see it, or serialization fails with "Object of type Output is not
        # JSON serializable".
        return pulumi.Output.all(
            image, log_group.name, task_environment, *ordered_secret_arns
        ).apply(
            lambda a: json.dumps(
                [
                    {
                        "name": name,
                        "image": a[0],
                        "essential": True,
                        "command": command,
                        "portMappings": (
                            [{"containerPort": APP_PORT, "protocol": "tcp"}]
                            if name == "web" else []
                        ),
                        "environment": [
                            {"name": k, "value": str(v)} for k, v in a[2].items()
                        ],
                        "secrets": [
                            {"name": env_name, "valueFrom": arn}
                            for (env_name, _), arn in zip(secret_env, a[3:])
                        ],
                        "logConfiguration": {
                            "logDriver": "awslogs",
                            "options": {
                                "awslogs-group": a[1],
                                "awslogs-region": region,
                                "awslogs-stream-prefix": name,
                            },
                        },
                    }
                ]
            )
        )

    def _task_def(suffix, container_name, command, cpu, memory):
        return aws.ecs.TaskDefinition(
            f"{prefix}-{suffix}",
            family=f"{prefix}-{suffix}",
            requires_compatibilities=["FARGATE"],
            network_mode="awsvpc",
            cpu=cpu,
            memory=memory,
            execution_role_arn=execution_role.arn,
            task_role_arn=task_role.arn,
            runtime_platform=aws.ecs.TaskDefinitionRuntimePlatformArgs(
                cpu_architecture="X86_64", operating_system_family="LINUX"
            ),
            container_definitions=_container(container_name, command, cpu, memory),
            tags=tags,
        )

    web_td = _task_def(
        "web", "web",
        # gunicorn ONLY — the Dockerfile CMD's migrate chain would race across
        # tasks. CI runs the migrate task definition below instead.
        ["gunicorn", "config.wsgi:application", "--bind", f"0.0.0.0:{APP_PORT}",
         "--workers", "4", "--threads", "4", "--timeout", "120"],
        WEB_CPU, WEB_MEMORY,
    )
    migrate_td = _task_def(
        "migrate", "migrate", ["sh", "ops/migrate_and_seed.sh"], JOB_CPU, JOB_MEMORY
    )
    material_td = _task_def(
        "material", "material-processor",
        # Overridden per upload by apps/dashboard/job_dispatch.py.
        ["python", "manage.py", "process_material"], JOB_CPU, JOB_MEMORY,
    )

    service = aws.ecs.Service(
        f"{prefix}-service",
        name=f"{prefix}-service",
        cluster=cluster.arn,
        task_definition=web_td.arn,
        desired_count=min_tasks,
        launch_type="FARGATE",
        network_configuration=aws.ecs.ServiceNetworkConfigurationArgs(
            subnets=private_subnet_ids,
            security_groups=[tasks_sg_id],
            assign_public_ip=False,
        ),
        load_balancers=[
            aws.ecs.ServiceLoadBalancerArgs(
                target_group_arn=target_group.arn,
                container_name="web",
                container_port=APP_PORT,
            )
        ],
        health_check_grace_period_seconds=120,
        deployment_minimum_healthy_percent=100,
        deployment_maximum_percent=200,
        # CI deploys new task definition revisions; Pulumi owns the shape.
        # Without this every `pulumi up` would revert production to the image
        # this stack last knew about.
        opts=pulumi.ResourceOptions(ignore_changes=["taskDefinition", "desiredCount"]),
        tags=tags,
    )

    target = aws.appautoscaling.Target(
        f"{prefix}-scale-target",
        service_namespace="ecs",
        scalable_dimension="ecs:service:DesiredCount",
        resource_id=pulumi.Output.all(cluster.name, service.name).apply(
            lambda a: f"service/{a[0]}/{a[1]}"
        ),
        min_capacity=min_tasks,
        max_capacity=max_tasks,
    )

    # Azure scales on 12 concurrent requests. ECS has no concurrency metric, so
    # this is a RATE standing in for a concurrency limit and WILL need
    # calibration against real traffic.
    aws.appautoscaling.Policy(
        f"{prefix}-scale-requests",
        policy_type="TargetTrackingScaling",
        service_namespace=target.service_namespace,
        scalable_dimension=target.scalable_dimension,
        resource_id=target.resource_id,
        target_tracking_scaling_policy_configuration=aws.appautoscaling.PolicyTargetTrackingScalingPolicyConfigurationArgs(
            target_value=150,
            predefined_metric_specification=aws.appautoscaling.PolicyTargetTrackingScalingPolicyConfigurationPredefinedMetricSpecificationArgs(
                predefined_metric_type="ALBRequestCountPerTarget",
                # Must be "app/<lb-name>/<lb-id>/targetgroup/<tg-name>/<tg-id>",
                # which is exactly arn_suffix on each. Deriving it from
                # target_group.load_balancer_arns does not work — that list is
                # empty here, since the association is made by the Listener.
                resource_label=pulumi.Output.all(
                    alb.arn_suffix, target_group.arn_suffix
                ).apply(lambda a: f"{a[0]}/{a[1]}"),
            ),
            scale_in_cooldown=300,
            scale_out_cooldown=60,
        ),
    )

    # Backstop for the case where requests are slow rather than numerous —
    # which, with multi-second LLM calls, is the likelier shape.
    aws.appautoscaling.Policy(
        f"{prefix}-scale-cpu",
        policy_type="TargetTrackingScaling",
        service_namespace=target.service_namespace,
        scalable_dimension=target.scalable_dimension,
        resource_id=target.resource_id,
        target_tracking_scaling_policy_configuration=aws.appautoscaling.PolicyTargetTrackingScalingPolicyConfigurationArgs(
            target_value=70,
            predefined_metric_specification=aws.appautoscaling.PolicyTargetTrackingScalingPolicyConfigurationPredefinedMetricSpecificationArgs(
                predefined_metric_type="ECSServiceAverageCPUUtilization"
            ),
            scale_in_cooldown=300,
            scale_out_cooldown=60,
        ),
    )

    return Compute(
        service=service,
        web_task_definition=web_td,
        migrate_task_definition=migrate_td,
        material_task_definition=material_td,
    )
