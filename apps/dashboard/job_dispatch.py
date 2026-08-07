"""Background job dispatch — large material processing.

Three backends, because Azure Container Apps and AWS ECS run the SAME image
and each supplies only its own environment:
  - ECS RunTask: starts a Fargate task from the material task definition.
  - Azure SDK: posts a Container Apps Job execution start to ARM. Live today.
  - Local subprocess: in dev, runs `python manage.py process_material`
    detached so devs can exercise the same flow without either cloud.

Selection, in order: ECS if `ECS_CLUSTER`, `ECS_MATERIAL_TASK_DEFINITION` and
`ECS_SUBNETS` are all set; else Azure if `AZURE_RESOURCE_GROUP` and
`AZURE_MATERIAL_JOB_NAME` are both set; else subprocess.
"""

import logging
import os
import shlex
import subprocess
import sys
from typing import Optional

from django.conf import settings

logger = logging.getLogger(__name__)


def _ecs_settings():
    """Returns (cluster, task_definition, subnets, security_groups, container_name)
    or None if not configured."""
    cluster = os.getenv('ECS_CLUSTER')
    task_definition = os.getenv('ECS_MATERIAL_TASK_DEFINITION')
    subnets = [s for s in os.getenv('ECS_SUBNETS', '').split(',') if s]
    security_groups = [g for g in os.getenv('ECS_SECURITY_GROUPS', '').split(',') if g]
    container_name = os.getenv('ECS_MATERIAL_CONTAINER_NAME', 'material-processor')
    if not (cluster and task_definition and subnets):
        return None
    return cluster, task_definition, subnets, security_groups, container_name


def _ecs_client():
    """boto3 ECS client. Credentials come from the web task's role."""
    import boto3

    return boto3.client('ecs', region_name=os.getenv('AWS_REGION', 'us-east-1'))


def _dispatch_via_ecs(
    upload_id: int, mode: str,
    cluster: str, task_definition: str,
    subnets: list, security_groups: list, container_name: str,
) -> str:
    """Start a Fargate task from the material task definition.

    Only the command is overridden. The task definition already carries the
    image, CPU/memory, environment and secrets — so unlike the Container Apps
    Job path below, there is no need to restate them per execution. That
    difference is the whole reason this backend is short.
    """
    try:
        client = _ecs_client()
    except ImportError as exc:
        raise RuntimeError(
            "boto3 not installed. Add it to requirements.txt before "
            "dispatching material jobs to ECS."
        ) from exc

    response = client.run_task(
        cluster=cluster,
        taskDefinition=task_definition,
        launchType='FARGATE',
        count=1,
        networkConfiguration={
            'awsvpcConfiguration': {
                'subnets': subnets,
                'securityGroups': security_groups,
                'assignPublicIp': 'DISABLED',
            },
        },
        overrides={
            'containerOverrides': [
                {
                    'name': container_name,
                    'command': [
                        'python', 'manage.py', 'process_material',
                        str(upload_id), '--mode', mode,
                    ],
                },
            ],
        },
    )

    failures = response.get('failures') or []
    if failures:
        raise RuntimeError(
            f"ECS RunTask failed for upload {upload_id}: {failures}"
        )
    tasks = response.get('tasks') or []
    if not tasks:
        raise RuntimeError(
            f"ECS RunTask returned no task for upload {upload_id}"
        )

    task_arn = tasks[0].get('taskArn', '')
    execution_name = task_arn.rsplit('/', 1)[-1] or task_arn
    logger.info(f"Dispatched material job for upload {upload_id} → {execution_name}")
    return execution_name


def _azure_settings():
    """Returns (subscription_id, resource_group, job_name) or None if not configured."""
    rg = os.getenv('AZURE_RESOURCE_GROUP')
    job = os.getenv('AZURE_MATERIAL_JOB_NAME')
    sub = os.getenv('AZURE_SUBSCRIPTION_ID')
    if not (rg and job):
        return None
    return sub, rg, job


def dispatch_material_job(upload_id: int, mode: str = 'rich') -> str:
    """Start a material-processing job execution.

    Returns the execution name (Azure SDK) or a local PID marker
    (subprocess fallback). Persisted on the upload row so the UI can
    surface it for debugging.
    """
    ecs_cfg = _ecs_settings()
    if ecs_cfg:
        return _dispatch_via_ecs(upload_id, mode, *ecs_cfg)
    azure_cfg = _azure_settings()
    if azure_cfg:
        return _dispatch_via_azure_sdk(upload_id, mode, *azure_cfg)
    return _dispatch_via_subprocess(upload_id, mode)


def _dispatch_via_azure_sdk(
    upload_id: int, mode: str,
    subscription_id: Optional[str], resource_group: str, job_name: str,
) -> str:
    """Start a Container Apps Job execution via the Azure Management SDK.

    Per-execution `args` override is the per-upload supply mechanism — the
    Job's base template carries `command=["python", "manage.py", "process_material"]`
    and we append `[str(upload_id), "--mode", mode]` here.
    """
    try:
        from azure.identity import DefaultAzureCredential
        from azure.mgmt.appcontainers import ContainerAppsAPIClient
        from azure.mgmt.appcontainers.models import (
            JobExecutionTemplate,
            JobExecutionContainer,
            ContainerResources,
        )
    except ImportError as exc:
        raise RuntimeError(
            "azure-identity + azure-mgmt-appcontainers not installed. "
            "Add them to requirements.txt before dispatching jobs to Azure."
        ) from exc

    credential = DefaultAzureCredential()
    sub_id = subscription_id or os.getenv('AZURE_SUBSCRIPTION_ID')
    if not sub_id:
        raise RuntimeError(
            "AZURE_SUBSCRIPTION_ID env var is required when dispatching "
            "to Container Apps Jobs."
        )

    client = ContainerAppsAPIClient(credential=credential, subscription_id=sub_id)

    # Azure's Container Apps Jobs API requires the per-execution template
    # to specify EVERYTHING the container needs. Any field omitted gets
    # silently defaulted (image: required → error; resources: 0.5 CPU /
    # 1 Gi → OOM; env: empty → DATABASE_URL missing → SQLite fallback
    # → "no such table" after 1.5h of work; volume_mounts: empty →
    # /app/media not mounted → file-not-found at upload.file_path).
    #
    # We hit each of these in production one at a time. Lesson learned:
    # don't try to pass a "minimal override" — copy EVERY relevant field
    # from the Job's base template (which Pulumi controls). The only
    # thing we genuinely need to override is `args` (the per-upload
    # parameters). Everything else stays in sync with whatever Pulumi
    # has deployed.
    job = client.jobs.get(resource_group_name=resource_group, job_name=job_name)
    base_template = getattr(job, 'template', None)
    base_containers = (
        getattr(base_template, 'containers', None) or []
    ) if base_template else []
    if not base_containers:
        raise RuntimeError(
            f"Container Apps Job {job_name!r} has no containers defined — "
            "cannot dispatch."
        )
    base_container = base_containers[0]
    base_image = getattr(base_container, 'image', None)
    if not base_image:
        raise RuntimeError(
            f"Container Apps Job {job_name!r} container has no image — "
            "cannot dispatch."
        )

    # Mirror resources (CPU / memory).
    base_resources = getattr(base_container, 'resources', None)
    exec_resources = None
    if base_resources is not None:
        exec_resources = ContainerResources(
            cpu=getattr(base_resources, 'cpu', None),
            memory=getattr(base_resources, 'memory', None),
        )

    template = JobExecutionTemplate(
        containers=[
            JobExecutionContainer(
                name=getattr(base_container, 'name', 'material-processor'),
                image=base_image,
                command=list(getattr(base_container, 'command', None) or []) or [
                    "python", "manage.py", "process_material",
                ],
                args=[str(upload_id), "--mode", mode],
                resources=exec_resources,
                # Env is the choke point that bit us — without it the
                # container starts but every Postgres-bound query fails
                # because Django falls back to SQLite. Pass the entire
                # env list through verbatim (secret_refs resolve against
                # the Job's secrets list, which stays as-is).
                env=getattr(base_container, 'env', None),
                # Mount /app/media so process_material can read the
                # uploaded file at upload.file_path.
                volume_mounts=getattr(base_container, 'volume_mounts', None),
            ),
        ],
        # Volumes have to be defined at the template level too, paired
        # with the volume_mounts above.
        volumes=getattr(base_template, 'volumes', None),
    )

    poller = client.jobs.begin_start(
        resource_group_name=resource_group,
        job_name=job_name,
        template=template,
    )
    # `begin_start` returns a poller. We don't need to wait for completion
    # (jobs run for ~25 min); we want the execution name immediately so the
    # caller can persist it. Poll once for the initial response which has
    # the execution metadata.
    result = poller.result(timeout=30)
    execution_name = getattr(result, 'name', '') or getattr(result, 'id', '') or ''
    logger.info(f"Dispatched material job for upload {upload_id} → {execution_name}")
    return execution_name


def _dispatch_via_subprocess(upload_id: int, mode: str) -> str:
    """Local-dev fallback. Runs the management command in a detached subprocess.

    Returns 'local-pid-<pid>' as the execution name. NOT durable across
    restarts — only suitable for development.
    """
    cmd = [
        sys.executable, 'manage.py', 'process_material',
        str(upload_id), '--mode', mode,
    ]
    cwd = str(settings.BASE_DIR) if hasattr(settings, 'BASE_DIR') else os.getcwd()
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    marker = f"local-pid-{proc.pid}"
    logger.info(f"Dispatched material job (subprocess) for upload {upload_id} → {marker}: {shlex.join(cmd)}")
    return marker
