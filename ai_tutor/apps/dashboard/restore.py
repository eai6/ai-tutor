"""Putting an archive back: the checks, the dispatch, and the progress feed.

The half of a restore that is safe to run in a web request lives here. Nothing
in this module destroys anything — it reads an archive's manifest, decides
whether restoring it is possible at all, and works out what it would change, so
that the person clicking the button is shown the consequences before they accept
them rather than discovering them afterwards. The destructive half is
manage.py restore_backup, which this only starts.

Three things shape the design:

  - **The manifest is usually free.** backup.build() copies it onto
    BackupJob.summary, so preflighting an archive still listed on the settings
    page costs a database read and no S3 bytes at all — including for a 9.3 GB
    one. Archives whose row is gone fall back to the sidecar _store() writes
    beside them, and only genuinely old archives require opening the tar.
  - **Refusing is cheap; a bad restore is not.** A cross-engine archive or a
    dump whose schema is ahead of this code cannot be restored — there is no
    migrating backwards — so those are refused outright rather than warned
    about. Everything recoverable is a warning.
  - **Progress cannot live in the database.** The restore drops it. The status
    object in S3 is the only place a running restore can report from, and the
    only place anyone can read it from while the service is scaled to zero.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tarfile
from pathlib import Path

from django.conf import settings
from django.db import connection
from django.utils import timezone

from ai_tutor.apps.dashboard import backup as backup_service

logger = logging.getLogger(__name__)

# Where uploaded payloads and status objects live inside the backups bucket.
# Separate from the archives themselves so a lifecycle rule can expire these
# after days rather than the 90 the archives get — an uploaded copy of somebody
# else's student records should not outstay its restore.
RESTORE_PREFIX = 'restores'

# Which dump format belongs to which engine. A dump cannot be restored into an
# engine that did not produce it, and the failure if you try is neither quick
# nor clean, so this is checked before anything else.
ENGINE_FOR_FORMAT = {
    'pg_dump-custom': 'postgresql',
    'sqlite-file': 'sqlite',
}


class PreflightFailed(RuntimeError):
    """The archive cannot be restored at all. Carries the failing checks."""

    def __init__(self, message: str, report: dict):
        super().__init__(message)
        self.report = report


# ---------------------------------------------------------------------------
# Reading what an archive says about itself
# ---------------------------------------------------------------------------

def _sidecar_manifest(key: str) -> dict | None:
    """The `<key>.manifest.json` written beside the archive by _store()."""
    bucket = backup_service.backup_bucket()
    name = f'{key}.manifest.json'
    if bucket and not key.startswith('/'):
        import boto3
        client = boto3.client('s3', region_name=getattr(settings, 'AWS_REGION', None)
                              or getattr(settings, 'AWS_MEDIA_REGION', None))
        try:
            body = client.get_object(Bucket=bucket, Key=name)['Body'].read()
            return json.loads(body)
        except Exception as exc:                       # noqa: BLE001 - absent is normal
            logger.info('no sidecar manifest at %s: %s', name, exc)
            return None

    path = Path(name)
    if path.is_file():
        return json.loads(path.read_text())
    return None


def _manifest_from_archive(key: str) -> dict | None:
    """Last resort: open the tar and find the manifest inside it.

    Only reached for archives taken before the sidecar existed. Streams rather
    than downloading, and stops at the manifest — but the manifest is the LAST
    member in those archives, so on a large one this reads the whole thing.
    That cost is exactly why the sidecar exists, and why this is the third
    choice rather than the first.
    """
    bucket = backup_service.backup_bucket()
    try:
        if bucket and not key.startswith('/'):
            import boto3
            client = boto3.client('s3', region_name=getattr(settings, 'AWS_REGION', None)
                                  or getattr(settings, 'AWS_MEDIA_REGION', None))
            body = client.get_object(Bucket=bucket, Key=key)['Body']
            with tarfile.open(fileobj=body, mode='r|gz') as tar:
                for member in tar:
                    if member.name == 'manifest.json':
                        return json.loads(tar.extractfile(member).read())
            return None

        with tarfile.open(key) as tar:
            return json.loads(tar.extractfile('manifest.json').read())
    except Exception as exc:                           # noqa: BLE001
        logger.warning('could not read a manifest out of %s: %s', key, exc)
        return None


def manifest_for(*, backup=None, key: str = '') -> tuple[dict, str]:
    """An archive's manifest, and how it was obtained.

    In order of cost: the BackupJob row (free), the sidecar object (one small
    GET), the archive itself (expensive, and only for archives that predate the
    sidecar). The caller shows the source, because "read from the archive" and
    "read from the row that claims to describe it" are different degrees of
    evidence and the confirmation page should not blur them.
    """
    if backup is not None and backup.summary:
        return backup.summary, 'backup record'

    key = key or (backup.storage_key if backup else '')
    if not key:
        raise PreflightFailed('no archive to read', {'ok': False, 'checks': []})

    sidecar = _sidecar_manifest(key)
    if sidecar:
        return sidecar, 'sidecar manifest'

    inside = _manifest_from_archive(key)
    if inside:
        return inside, 'archive contents'

    raise PreflightFailed(
        'This file does not describe itself: no manifest was found in it, '
        'beside it, or in this platform\'s records. It may not be an AI Tutor '
        'backup, or it may be truncated.',
        {'ok': False, 'checks': []},
    )


# ---------------------------------------------------------------------------
# Schema comparison
# ---------------------------------------------------------------------------

def code_migrations() -> dict[str, set[str]]:
    """Every migration THIS code ships, per app, read from disk.

    Deliberately not from django_migrations: the question is what this code
    knows how to run, not what the current database happens to have applied.
    MigrationLoader(None) does not touch the database, which matters because
    this is also called from the restore task after the database has gone.
    """
    from django.db.migrations.loader import MigrationLoader
    loader = MigrationLoader(None, ignore_no_migrations=True)
    known: dict[str, set[str]] = {}
    for app, name in loader.disk_migrations:
        known.setdefault(app, set()).add(name)
    return known


def compare_schema(archive_heads: dict) -> dict:
    """Whether this code can run the schema in the archive.

    Set membership, not name ordering. A migration name sorts by its numeric
    prefix only by convention, and the question here is exact: does this code
    contain the migration the archive stopped at?

      - Archive head this code does not have  -> the archive is AHEAD. Refuse.
        The dump carries tables and columns this code has no migration to
        produce and none to remove, and there is no migrating backwards.
      - Archive head this code has, but later ones exist -> BEHIND. Fine:
        migrate runs forward after the restore and catches it up.
    """
    known = code_migrations()
    ahead, behind = {}, {}

    for app, head in (archive_heads or {}).items():
        available = known.get(app)
        if not available:
            # An app this code does not have at all. Same class of problem:
            # rows we cannot describe, and no migration that would remove them.
            ahead[app] = head
            continue
        if head not in available:
            ahead[app] = head
        elif head != max(available):
            behind[app] = {'archive': head, 'code': max(available)}

    return {'ahead': ahead, 'behind': behind}


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------

def _check(name: str, status: str, detail: str) -> dict:
    """One line of the report. status is pass | warn | fail | unknown.

    `unknown` is its own outcome and never collapses into `pass`. An archive
    that predates checksums cannot be verified, and saying so is the point —
    claiming it passed a check that was never run is how someone ends up
    trusting a truncated file.
    """
    return {'name': name, 'status': status, 'detail': detail}


def _row_diff(archive_counts: dict, live_counts: dict) -> list[dict]:
    """What each table would go from and to, biggest loss first.

    Sorted by what is lost rather than by name, because the question being
    answered is "what am I about to destroy" and the largest losses are the
    ones worth reading first.
    """
    rows = []
    for table in sorted(set(archive_counts) | set(live_counts)):
        archive = archive_counts.get(table)
        live = live_counts.get(table)
        rows.append({
            'table': table,
            'archive': archive,
            'live': live,
            'delta': None if archive is None or live is None else archive - live,
        })
    rows.sort(key=lambda r: (r['delta'] if r['delta'] is not None else 0))
    return rows


def preflight(*, backup=None, key: str = '') -> dict:
    """Everything knowable about an archive without restoring it.

    Returns a report. Raises PreflightFailed only when the archive cannot be
    read at all; a readable archive that must not be restored comes back with
    ok=False and the reasons, because the caller needs to show them.
    """
    manifest, source = manifest_for(backup=backup, key=key)
    checks: list[dict] = []
    blocking: list[str] = []

    version = manifest.get('archive_format_version', 1)
    database = manifest.get('database') or {}
    media = manifest.get('media') or {}

    # --- is this one of ours, and whole ---------------------------------
    if manifest.get('scope') != 'platform':
        blocking.append(
            f"This archive's scope is {manifest.get('scope') or 'unrecorded'}, "
            f"not a whole platform. Restoring it would not mean what it says."
        )
        checks.append(_check('Archive kind', 'fail', 'not a platform backup'))
    else:
        checks.append(_check('Archive kind', 'pass', 'whole-platform backup'))

    # --- engine -----------------------------------------------------------
    dump_format = database.get('dump_format')
    expected = ENGINE_FOR_FORMAT.get(dump_format)
    if expected is None:
        blocking.append(f'Unrecognised dump format {dump_format!r}.')
        checks.append(_check('Dump format', 'fail', repr(dump_format)))
    elif expected != connection.vendor:
        # No conversion path exists, and a half-applied attempt would leave the
        # database in a state neither engine understands.
        blocking.append(
            f'This archive holds a {expected} dump and this platform runs on '
            f'{connection.vendor}. One cannot be restored into the other.'
        )
        checks.append(_check('Database engine', 'fail',
                             f'{expected} archive, {connection.vendor} server'))
    else:
        checks.append(_check('Database engine', 'pass', connection.vendor))

    # --- schema -----------------------------------------------------------
    heads = database.get('migration_heads')
    if not heads:
        checks.append(_check(
            'Schema version', 'unknown',
            'This archive predates schema recording. Whether its tables match '
            'the running code will not be known until the restore runs.'))
    else:
        schema = compare_schema(heads)
        if schema['ahead']:
            named = ', '.join(f'{a} {n}' for a, n in sorted(schema['ahead'].items()))
            blocking.append(
                f'This archive was taken from a NEWER version of the platform '
                f'than the one running ({named}). Its tables cannot be undone '
                f'to match this code — deploy that version first, then restore.'
            )
            checks.append(_check('Schema version', 'fail',
                                 f'archive is ahead: {named}'))
        elif schema['behind']:
            named = ', '.join(sorted(schema['behind']))
            checks.append(_check(
                'Schema version', 'warn',
                f'Older than the running code ({named}). Migrations will run '
                f'after the restore to bring it forward.'))
        else:
            checks.append(_check('Schema version', 'pass',
                                 'matches the running code'))

    # --- integrity --------------------------------------------------------
    if database.get('dump_sha256'):
        checks.append(_check(
            'Integrity', 'pass',
            'A checksum is recorded and will be verified against the dump '
            'before anything is dropped.'))
    else:
        checks.append(_check(
            'Integrity', 'unknown',
            f'This archive records no checksum (format version {version}). '
            f'A truncated or corrupted copy cannot be told from a good one.'))

    # --- media ------------------------------------------------------------
    if media.get('included'):
        checks.append(_check('Uploaded files', 'pass',
                             f"{media.get('files_archived', 0)} files travel with it"))
    else:
        checks.append(_check(
            'Uploaded files', 'warn',
            'Database only. Lesson figures will point at files this archive '
            'does not contain, and will break unless the media store is intact.'))

    # --- what it would change --------------------------------------------
    try:
        # _database_inventory() directly, NOT the cached inventory(). Two
        # reasons, and both matter on this particular screen.
        #
        # Freshness: this diff is the "what am I about to destroy" number, read
        # in the seconds before someone accepts it. A count up to fifteen
        # minutes old is fine for the card on the settings page and is not fine
        # here.
        #
        # Cost: the diff needs row counts only, and inventory(fresh=True) would
        # also re-list the entire media bucket to produce two numbers nothing
        # on this page shows.
        live = backup_service._database_inventory().get('row_counts', {})
    except Exception as exc:                           # noqa: BLE001
        logger.warning('could not count live rows for the diff: %s', exc)
        live = {}

    report = {
        'ok': not blocking,
        'blocking': blocking,
        'manifest_source': source,
        'archive_format_version': version,
        'checks': checks,
        'created_at': manifest.get('created_at'),
        'created_by': manifest.get('created_by'),
        'include_media': bool(media.get('included')),
        'dump_sha256': database.get('dump_sha256', ''),
        'archive_sha256': manifest.get('archive_sha256', ''),
        'row_diff': _row_diff(database.get('row_counts') or {}, live),
        'checked_at': timezone.now().isoformat(),
    }
    return report


# ---------------------------------------------------------------------------
# Archive member safety
# ---------------------------------------------------------------------------

def media_key_for(member: tarfile.TarInfo, dump_file: str) -> str | None:
    """The media object key a tar member maps to, or None if it is not media.

    Raises on anything that is neither media nor an expected bookkeeping file.
    An uploaded archive's member names are attacker-supplied, and while these
    end up as S3 keys rather than filesystem paths, a key of "../.." is still
    wrong and a symlink is still not a file. Nothing here ever calls extractall.
    """
    name = member.name
    if name in (dump_file, 'manifest.json', 'manifest-final.json'):
        return None

    if not member.isreg():
        raise PreflightFailed(
            f'Archive member {name!r} is not a regular file '
            f'(directories, symlinks and devices are never restored).',
            {'ok': False, 'checks': []})

    if not name.startswith('media/'):
        raise PreflightFailed(
            f'Unexpected archive member {name!r}. A platform backup contains '
            f'a dump, a manifest, and media/ — nothing else.',
            {'ok': False, 'checks': []})

    key = name[len('media/'):]
    # Normalised, then checked: ".." can hide behind "a/../..".
    normalised = os.path.normpath(key)
    if (not key or key.startswith('/') or normalised.startswith(('..', '/'))
            or os.path.isabs(normalised)):
        raise PreflightFailed(
            f'Archive member {name!r} would escape the media store.',
            {'ok': False, 'checks': []})
    return normalised


# ---------------------------------------------------------------------------
# Progress, somewhere the restore cannot destroy
# ---------------------------------------------------------------------------

def status_key(job) -> str:
    return f'{RESTORE_PREFIX}/status-{job.pk}.json'


def write_status(job, **fields) -> None:
    """Record progress outside the database.

    The restore drops the table holding RestoreJob, so for most of the run this
    object is the only account of what is happening — and with the service
    scaled to zero it is also the only one anybody can reach. Never raises: a
    restore must not fail because it could not describe itself.
    """
    payload = {'job_id': job.pk, 'updated_at': timezone.now().isoformat(), **fields}
    blob = json.dumps(payload, indent=2, default=str).encode()
    bucket = backup_service.backup_bucket()
    try:
        if bucket:
            import boto3
            client = boto3.client('s3', region_name=getattr(settings, 'AWS_REGION', None)
                                  or getattr(settings, 'AWS_MEDIA_REGION', None))
            client.put_object(Bucket=bucket, Key=status_key(job), Body=blob,
                              ContentType='application/json',
                              ServerSideEncryption='AES256')
        else:
            path = backup_service.backup_root() / f'restore-status-{job.pk}.json'
            path.write_bytes(blob)
    except Exception as exc:                           # noqa: BLE001
        logger.warning('could not write restore status for %s: %s', job.pk, exc)


def read_status(job) -> dict | None:
    """The status object, or None. Reads no database."""
    bucket = backup_service.backup_bucket()
    try:
        if bucket:
            import boto3
            client = boto3.client('s3', region_name=getattr(settings, 'AWS_REGION', None)
                                  or getattr(settings, 'AWS_MEDIA_REGION', None))
            body = client.get_object(Bucket=bucket, Key=status_key(job))['Body'].read()
            return json.loads(body)
        path = backup_service.backup_root() / f'restore-status-{job.pk}.json'
        return json.loads(path.read_text()) if path.is_file() else None
    except Exception as exc:                           # noqa: BLE001
        logger.info('no restore status for %s: %s', job.pk, exc)
        return None


# Long, on purpose. This link is handed over BEFORE the restore starts, and has
# to still work when the admin comes back to it after the site has been down
# for half an hour.
STATUS_URL_TTL_SECONDS = 6 * 60 * 60


def status_presigned_url(job) -> str | None:
    """A link to the status object that works while the platform is down.

    The page that would normally show progress is served by gunicorn, which is
    scaled to zero for the whole destructive window, behind a login that needs
    the database. Without this the only way to watch a restore is the AWS
    console.
    """
    bucket = backup_service.backup_bucket()
    if not bucket:
        return None
    import boto3
    client = boto3.client('s3', region_name=getattr(settings, 'AWS_REGION', None)
                          or getattr(settings, 'AWS_MEDIA_REGION', None))
    return client.generate_presigned_url(
        'get_object',
        Params={'Bucket': bucket, 'Key': status_key(job)},
        ExpiresIn=STATUS_URL_TTL_SECONDS,
    )


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def _ecs_settings():
    """(cluster, task_definition, subnets, security_groups, container) or None.

    The restore reuses the migrate task definition unless given one of its own:
    it already carries DATABASE_URL, already runs in the subnets that can reach
    RDS, and — unlike a family Pulumi creates but CI never re-registers — it is
    rebuilt on every deploy, so it cannot drift from the code whose migrations
    it is about to run. ops/restore_from_dump.sh makes the same choice.
    """
    cluster = os.getenv('ECS_CLUSTER')
    task_definition = (os.getenv('ECS_RESTORE_TASK_DEFINITION')
                       or os.getenv('ECS_MIGRATE_TASK_DEFINITION'))
    subnets = [s for s in os.getenv('ECS_SUBNETS', '').split(',') if s]
    security_groups = [g for g in os.getenv('ECS_SECURITY_GROUPS', '').split(',') if g]
    container = os.getenv('ECS_RESTORE_CONTAINER_NAME', 'migrate')
    if not (cluster and task_definition and subnets):
        return None
    return cluster, task_definition, subnets, security_groups, container


def _dispatch_via_ecs(job, cluster, task_definition, subnets,
                      security_groups, container) -> str:
    import boto3
    client = boto3.client('ecs', region_name=os.getenv('AWS_REGION', 'us-east-1'))
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
                    'name': container,
                    # The job id and nothing else. Everything the restore acts
                    # on — which archive, whether media travels, what was
                    # approved — is read from the row, so a tampered override
                    # cannot redirect it at a different file.
                    'command': ['python', 'manage.py', 'restore_backup',
                                '--job', str(job.pk)],
                },
            ],
        },
    )
    failures = response.get('failures') or []
    if failures:
        raise RuntimeError(f'ECS RunTask failed for restore {job.pk}: {failures}')
    tasks = response.get('tasks') or []
    if not tasks:
        raise RuntimeError(f'ECS RunTask returned no task for restore {job.pk}')
    return tasks[0].get('taskArn', '')


def _dispatch_via_subprocess(job) -> str:
    """Dev, and Path A on one server. Detached, because the restore must not
    share a process with the web server whose database connection it is about
    to drop.

    Output goes to a log file, NOT to DEVNULL. On ECS the task's stdout reaches
    CloudWatch and a failed restore can be read back; off ECS there is no such
    collector, and discarding it means a restore that half-worked leaves no
    explanation anywhere — the row that would have recorded the failure is in
    the database the restore was busy replacing. Learned by doing exactly that.
    """
    manage = Path(settings.BASE_DIR) / 'manage.py'
    if not manage.is_file():                           # installed as a package
        manage = Path(__file__).resolve().parents[3] / 'manage.py'

    log_path = backup_service.backup_root() / f'restore-{job.pk}.log'
    log = log_path.open('ab')
    try:
        process = subprocess.Popen(
            [sys.executable, str(manage), 'restore_backup', '--job', str(job.pk)],
            stdout=log, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    finally:
        # The child holds its own duplicate of the descriptor.
        log.close()
    logger.info('restore %s logging to %s', job.pk, log_path)
    return f'pid:{process.pid}'


def required_settings_missing() -> list[str]:
    """What a restore on ECS still needs, or [] when it is fully configured.

    Checked because the subprocess fallback is SAFE off ECS and CATASTROPHIC on
    it. Running in-container means no scale-to-zero, no autoscaling suspension
    and no idle wait — the restore would drop the production database while the
    other web tasks are still serving from it, which is the 2026-08-08 collision
    reproduced deliberately.

    job_dispatch.py can fall through to a subprocess on partial configuration
    because the worst case there is a material upload processed in the wrong
    place. Here the worst case is the database. So this fails closed instead.
    """
    if not os.getenv('ECS_CLUSTER'):
        return []                                      # genuinely not on ECS

    missing = []
    if not (os.getenv('ECS_RESTORE_TASK_DEFINITION')
            or os.getenv('ECS_MIGRATE_TASK_DEFINITION')):
        missing.append('ECS_MIGRATE_TASK_DEFINITION')
    if not os.getenv('ECS_SUBNETS'):
        missing.append('ECS_SUBNETS')
    if not os.getenv('ECS_SERVICE'):
        # Without this the restore cannot stop the platform, and would drop the
        # database out from under the tasks still serving it.
        missing.append('ECS_SERVICE')
    return missing


def dispatch(job) -> str:
    """Start the restore. Returns the task ARN, or a pid marker off ECS.

    Backend selection follows apps/dashboard/job_dispatch.py — one image runs in
    several places and each supplies only its own environment — with one
    deliberate difference: a PARTIALLY configured ECS environment refuses rather
    than falling back. See required_settings_missing().

    Azure is not supported. Container Apps has no equivalent of scaling an ECS
    service to zero, so a correct restore there is different work and a
    half-correct one is worse than none.
    """
    missing = required_settings_missing()
    if missing:
        raise RuntimeError(
            'This platform runs on ECS but the restore is not configured: '
            + ', '.join(missing) + ' unset. Refusing to fall back to running '
            'the restore inside the web container, which would drop the '
            'database while the other tasks are still serving from it. Apply '
            'the infrastructure changes (pulumi up) first.')

    ecs = _ecs_settings()
    if ecs:
        arn = _dispatch_via_ecs(job, *ecs)
        logger.info('restore %s dispatched to %s', job.pk, arn)
        return arn
    marker = _dispatch_via_subprocess(job)
    logger.info('restore %s dispatched locally (%s)', job.pk, marker)
    return marker


# ---------------------------------------------------------------------------
# Reaping
# ---------------------------------------------------------------------------

def _task_is_alive(task_arn: str) -> bool | None:
    """Whether the cluster still has this task. None when it cannot be asked."""
    cluster = os.getenv('ECS_CLUSTER')
    if not (cluster and task_arn.startswith('arn:')):
        return None
    try:
        import boto3
        client = boto3.client('ecs', region_name=os.getenv('AWS_REGION', 'us-east-1'))
        tasks = client.describe_tasks(cluster=cluster, tasks=[task_arn]).get('tasks', [])
        if not tasks:
            return False
        return tasks[0].get('lastStatus') != 'STOPPED'
    except Exception as exc:                           # noqa: BLE001
        logger.warning('could not describe restore task %s: %s', task_arn, exc)
        return None


# Wall-clock backstop only. A restore of a large archive is legitimately slow,
# and unlike a backup there is no safe way to guess: the point of the ECS check
# above is that elapsed time is the weakest possible evidence.
STALE_AFTER = backup_service.STALE_AFTER


def reap_stale() -> int:
    """Clear restore rows whose task is demonstrably gone.

    Deliberately not a copy of backup.reap_stale(). That one may assume a dead
    thread after six hours because nothing else can tell it. Here the cluster
    knows, and asking it is the difference between clearing a crashed restore
    promptly and killing a slow one that is still working. The wall-clock rule
    only applies when the cluster cannot be asked at all.
    """
    from ai_tutor.apps.dashboard.models import RestoreJob

    cutoff = timezone.now() - STALE_AFTER
    reaped = 0
    unfinished = RestoreJob.objects.filter(
        status__in=(RestoreJob.Status.PENDING, RestoreJob.Status.RUNNING))

    for job in unfinished:
        alive = _task_is_alive(job.task_arn) if job.task_arn else None
        if alive:
            continue
        if alive is None and job.created_at > cutoff:
            # Cannot ask, and not old enough to presume. Leave it be — a
            # restore wrongly marked failed is one somebody may start again on
            # top of a database the first one is still writing to.
            continue

        reason = ('Its task is no longer running in the cluster.' if alive is False
                  else f'No progress for over {STALE_AFTER}.')
        job.status = RestoreJob.Status.FAILED
        job.stage = 'abandoned'
        job.error = (
            f'{reason} The restore did not report finishing. Check the status '
            f'object and the safety copy recorded on this row before retrying: '
            f'the database may have been partly restored.'
        )
        job.finished_at = timezone.now()
        job.save(update_fields=['status', 'stage', 'error', 'finished_at'])
        reaped += 1

    return reaped
