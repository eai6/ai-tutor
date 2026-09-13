"""The one-restore-at-a-time guarantee, held somewhere the restore cannot destroy.

A restore's first destructive act is dropping the database. Any lock living in
that database — a row, a partial unique index, an advisory lock on the
connection — goes with it, and stays gone until the restore puts a table back.
That window is the exact window in which a second restore would do the most
damage: two pg_restores into one database, or one task scaling the service back
up while the other is still loading rows.

RestoreJob's partial unique index still guards the *dispatch* path, which is the
race a person can cause from the settings page. This guards the rest. The two
are documented together on RestoreJob.

Backends, chosen the way apps/dashboard/job_dispatch.py chooses its own:

  - **ECS** — ask the cluster whether another restore task is running. The
    cluster is not the database and does not go away with it, the answer is
    exact rather than advisory, and a dead task releases the lock by ceasing to
    exist. Nothing has to be cleaned up, which is the property that matters for
    a process that may be SIGKILLed halfway.
  - **File** — dev, and Path A on one server. An O_EXCL file carrying its own
    expiry, so a crashed holder cannot wedge future restores forever.

Both fail CLOSED: if the check cannot be made, the restore does not run. The
cost of a refused restore is a retry; the cost of two concurrent ones is the
database.
"""
from __future__ import annotations

import json
import logging
import os
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

from django.utils import timezone

logger = logging.getLogger(__name__)

# How long a file lock stays valid without being released. Long enough to cover
# a real restore of a large archive; finite so a killed process does not block
# every future restore. The ECS backend needs no equivalent — a task that dies
# stops being listed.
FILE_LOCK_TTL = timedelta(hours=6)

LOCK_FILENAME = 'restore.lock'


class RestoreLocked(RuntimeError):
    """Another restore holds the lock. Never retried automatically."""


# ---------------------------------------------------------------------------
# ECS
# ---------------------------------------------------------------------------

def _ecs_settings():
    """(cluster, family) when this is running under ECS, else None."""
    cluster = os.getenv('ECS_CLUSTER')
    family = os.getenv('ECS_RESTORE_FAMILY') or os.getenv('ECS_MIGRATE_TASK_DEFINITION')
    if not (cluster and family):
        return None
    # A task definition arrives as `family`, `family:revision`, or a full ARN
    # (`arn:aws:ecs:REGION:ACCT:task-definition/family:revision`). ListTasks
    # wants the bare family and matches NOTHING when given anything else —
    # silently, which reads as "no other restore is running".
    #
    # Order matters: strip the ARN prefix first, then the revision. Doing it the
    # other way round splits the ARN on its own first colon and yields "arn".
    return cluster, family.split('/')[-1].split(':')[0]


def own_task_arn() -> str | None:
    """This task's own ARN, from the ECS task metadata endpoint.

    Without this the cluster check can never pass. `list_tasks` filtered by the
    restore family returns every running task in that family — including the one
    asking. ops/restore_from_dump.sh can count running tasks down to zero
    because it runs from a laptop BEFORE starting anything; the same loop moved
    inside the task would wait for itself to exit and abort every single time.
    """
    uri = os.getenv('ECS_CONTAINER_METADATA_URI_V4')
    if not uri:
        return None
    import urllib.request
    try:
        with urllib.request.urlopen(f'{uri}/task', timeout=5) as response:
            return json.load(response).get('TaskARN')
    except Exception as exc:                           # noqa: BLE001
        logger.warning('could not read own task ARN: %s', exc)
        return None


def _sibling_restore_tasks(cluster: str, family: str) -> list[str]:
    """Every other task of the restore family the cluster considers alive.

    PENDING as well as RUNNING: a task that has been accepted but has not yet
    started is exactly the one about to race this restore, and it would not
    appear in a RUNNING-only list.
    """
    import boto3
    client = boto3.client('ecs', region_name=os.getenv('AWS_REGION', 'us-east-1'))
    mine = own_task_arn()

    arns: list[str] = []
    for desired in ('RUNNING', 'PENDING'):
        paginator = client.get_paginator('list_tasks')
        for page in paginator.paginate(cluster=cluster, family=family,
                                       desiredStatus=desired):
            arns.extend(page.get('taskArns', []))

    return [arn for arn in arns if arn != mine]


@contextmanager
def _ecs_lock(job):
    cluster, family = _ecs_settings()
    # Deliberately not caught: if ListTasks fails — throttled, denied, network —
    # we do not know whether another restore is running, and "do not know" must
    # mean "do not start". Failing closed here costs a retry.
    others = _sibling_restore_tasks(cluster, family)
    if others:
        raise RestoreLocked(
            f'another restore task is already running in {cluster}: '
            f'{", ".join(a.rsplit("/", 1)[-1] for a in others)}'
        )
    logger.info('restore %s holds the cluster lock (family %s)', job.pk, family)
    # Nothing to release. The lock IS this task's existence, so it is given up
    # by exiting — including on SIGKILL, which no explicit release survives.
    yield


# ---------------------------------------------------------------------------
# File
# ---------------------------------------------------------------------------

def _lock_path() -> Path:
    from ai_tutor.apps.dashboard import backup as backup_service
    return backup_service.backup_root() / LOCK_FILENAME


def _write_lock(path: Path, job, *, replacing: bool) -> None:
    payload = json.dumps({
        'job_id': job.pk,
        'pid': os.getpid(),
        'taken_at': timezone.now().isoformat(),
        'expires_at': (timezone.now() + FILE_LOCK_TTL).isoformat(),
    }).encode()
    if replacing:
        path.write_bytes(payload)
        return

    # Write the contents FIRST, under a private name, then link it into place.
    # os.link fails with FileExistsError if the target exists, so the link is
    # the mutual exclusion — and the file is complete at the instant it becomes
    # visible under the lock's name.
    #
    # O_EXCL alone is not enough here, and the difference is a two-holder bug.
    # O_EXCL makes the CREATE atomic but leaves the file empty until the write
    # lands. A rival arriving in that gap sees a lock it cannot parse, and
    # _expired() below treats unparseable as "written by a process that was
    # killed mid-write" — so it takes the lock over, and now two restores are
    # running. Found by the concurrency test, which only fails under enough
    # load to open the gap: it passed on its own and failed in the full suite.
    tmp = path.with_name(f'{path.name}.{os.getpid()}.{id(job):x}.tmp')
    tmp.write_bytes(payload)
    try:
        os.link(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _expired(path: Path) -> bool:
    """Whether an existing lock has outlived its TTL, or is unreadable.

    An unreadable lock counts as expired: a truncated or corrupt file is the
    fingerprint of a process killed mid-write, which is precisely the holder
    that will never come back to release it.
    """
    try:
        held = json.loads(path.read_text())
        return timezone.now() > timezone.datetime.fromisoformat(held['expires_at'])
    except Exception:                                  # noqa: BLE001
        logger.warning('restore lock at %s is unreadable; treating as expired', path)
        return True


@contextmanager
def _file_lock(job):
    path = _lock_path()
    try:
        _write_lock(path, job, replacing=False)
    except FileExistsError:
        if not _expired(path):
            raise RestoreLocked(
                f'another restore holds {path}. If no restore is running, the '
                f'lock clears itself {FILE_LOCK_TTL} after it was taken.'
            ) from None
        logger.warning('taking over an expired restore lock at %s', path)
        _write_lock(path, job, replacing=True)

    try:
        yield
    finally:
        # Best effort. A crash leaves the file behind, which is what the TTL is
        # for — the lock is bounded whether or not this line ever runs.
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning('could not release restore lock %s: %s', path, exc)


# ---------------------------------------------------------------------------

@contextmanager
def exclusive(job):
    """Hold the restore lock for the duration of the block.

    Raises RestoreLocked if another restore has it. Never blocks waiting: a
    restore that queues behind another is not something anyone wants to happen
    unattended, and the second one's archive is almost certainly stale by the
    time the first finishes.
    """
    backend = _ecs_lock if _ecs_settings() else _file_lock
    with backend(job):
        yield
