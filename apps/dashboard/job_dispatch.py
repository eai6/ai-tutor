"""Container Apps Job dispatch — large material processing.

Two backends:
  - Azure SDK: in production, posts a Job execution start to ARM.
  - Local subprocess: in dev, runs `python manage.py process_material`
    detached so devs can exercise the same flow without Azure.

Selection: if `AZURE_RESOURCE_GROUP` and `AZURE_MATERIAL_JOB_NAME` env vars
are both set, use the SDK backend; otherwise fall back to subprocess.
"""

import logging
import os
import shlex
import subprocess
import sys
from typing import Optional

from django.conf import settings

logger = logging.getLogger(__name__)


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
