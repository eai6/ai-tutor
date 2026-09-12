"""Whole-platform backup: a restorable database dump, the media beside it, and
a manifest saying what the two contain.

Written for the question a regulator asks — "if this server burned down this
morning, what would you restore from?" — which RDS automated snapshots answer
only partly. Snapshots live in the same account as the thing they protect, are
deleted with the instance, and cannot be read anywhere except AWS. A `pg_dump`
in a bucket can be pulled down, inspected, and loaded into any Postgres.

Both halves travel together on purpose. The database holds the reference to
every uploaded figure and the bucket holds the file; a database restored
against older media leaves lessons with broken images and reports no error.
See docs/self-hosting.md, which says the same thing to whoever runs it.

Runs in a background thread rather than in the request: gunicorn is started
with `--timeout 120` on AWS, and a dump plus a few gigabytes of media does not
fit in two minutes. The thread writes progress to the BackupJob row, which is
what the settings page polls.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tarfile
import tempfile
import threading
from pathlib import Path

from django.conf import settings
from django.db import connection, connections
from django.utils import timezone

logger = logging.getLogger(__name__)

# Key prefix inside the bucket. The bucket holds nothing else today, but a
# prefix costs nothing and lets a lifecycle rule or an access policy name these
# objects specifically if it ever does.
OPS_PREFIX = 'backups'

# How long a download link stays valid. Short: the link is the archive, and it
# needs no credentials, so anyone it is forwarded to gets every student record.
DOWNLOAD_URL_TTL_SECONDS = 15 * 60


def backup_bucket() -> str:
    return getattr(settings, 'AWS_BACKUP_BUCKET', '') or ''


def backup_root() -> Path:
    """Where archives go when there is no bucket — dev, and Docker without S3.

    Deliberately NOT under MEDIA_ROOT. Media is web-reachable in several of the
    configurations this runs in, and an archive of every student record is the
    last thing that should be fetchable by URL.
    """
    root = getattr(settings, 'BACKUP_ROOT', None) or Path(settings.BASE_DIR) / 'backups'
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    return root


# ---------------------------------------------------------------------------
# Inventory — what a backup would contain, without taking one
# ---------------------------------------------------------------------------

def _media_inventory() -> dict:
    """File count and total bytes, from whichever store media lives in."""
    if getattr(settings, 'USE_S3_MEDIA', False) and getattr(settings, 'AWS_MEDIA_BUCKET', ''):
        try:
            import boto3
            client = boto3.client('s3', region_name=getattr(settings, 'AWS_MEDIA_REGION', None))
            paginator = client.get_paginator('list_objects_v2')
            count = total = 0
            for page in paginator.paginate(Bucket=settings.AWS_MEDIA_BUCKET):
                for obj in page.get('Contents', []):
                    count += 1
                    total += obj['Size']
            return {'store': 's3', 'files': count, 'bytes': total}
        except Exception as exc:                       # noqa: BLE001 - reported, not raised
            logger.warning('media inventory failed: %s', exc)
            return {'store': 's3', 'files': 0, 'bytes': 0, 'error': str(exc)}

    root = Path(settings.MEDIA_ROOT)
    if not root.exists():
        return {'store': 'local', 'files': 0, 'bytes': 0}
    count = total = 0
    for path in root.rglob('*'):
        if path.is_file():
            count += 1
            total += path.stat().st_size
    return {'store': 'local', 'files': count, 'bytes': total}


def _database_inventory() -> dict:
    """Engine, and a row count for the tables that carry student data.

    The counts are what makes a restore checkable: a manifest saying 23 students
    and 1,412 turns is something you can compare against what came back.
    """
    engine = connection.vendor
    counts = {}
    from django.apps import apps
    for model in apps.get_models():
        label = model._meta.label
        if not label.startswith(('accounts.', 'tutoring.', 'curriculum.',
                                 'dashboard.', 'safety.', 'media_library.',
                                 'support.', 'auth.')):
            continue
        try:
            counts[model._meta.db_table] = model.objects.count()
        except Exception:                              # noqa: BLE001 - unmanaged table
            continue

    size = None
    if engine == 'postgresql':
        with connection.cursor() as cur:
            cur.execute("SELECT pg_database_size(current_database())")
            size = cur.fetchone()[0]
    elif engine == 'sqlite':
        path = Path(connection.settings_dict['NAME'])
        size = path.stat().st_size if path.exists() else 0

    return {'engine': engine, 'bytes': size, 'row_counts': counts}


def inventory() -> dict:
    """What the settings page shows before anyone presses the button."""
    return {'database': _database_inventory(), 'media': _media_inventory()}


# ---------------------------------------------------------------------------
# Building
# ---------------------------------------------------------------------------

def _dump_database(dest: Path, job) -> str:
    """Write a restorable dump to *dest*. Returns the format actually used.

    Postgres gets `pg_dump -Fc`, which pg_restore reads selectively — one table
    out of an archive, without loading the rest. SQLite gets a file copy taken
    through the online-backup API rather than `cp`, which can capture a torn
    page if anything writes mid-read.
    """
    db = connections['default'].settings_dict
    if connection.vendor == 'postgresql':
        env = dict(os.environ)
        if db.get('PASSWORD'):
            env['PGPASSWORD'] = db['PASSWORD']
        cmd = [
            'pg_dump', '--format=custom', '--no-owner', '--no-privileges',
            '--dbname', db['NAME'],
        ]
        if db.get('USER'):
            cmd += ['--username', db['USER']]
        if db.get('HOST'):
            cmd += ['--host', db['HOST']]
        if db.get('PORT'):
            cmd += ['--port', str(db['PORT'])]
        cmd += ['--file', str(dest)]
        # sslmode rides in the environment: it is in OPTIONS, not the DSN we
        # rebuild here, and RDS refuses a connection without it.
        sslmode = (db.get('OPTIONS') or {}).get('sslmode')
        if sslmode:
            env['PGSSLMODE'] = sslmode
        result = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=3600)
        if result.returncode != 0:
            raise RuntimeError(f'pg_dump failed: {result.stderr.strip()[:500]}')
        return 'pg_dump-custom'

    if connection.vendor == 'sqlite':
        import sqlite3
        name = db['NAME']
        # A name of the form file:...?mode=memory&cache=shared is a URI, and
        # sqlite3 only reads it as one when told. Without this it creates a file
        # with that literal name and backs up an empty database — a backup that
        # succeeds and contains nothing.
        uri = isinstance(name, str) and name.startswith('file:')
        # Bounded, not infinite. SQLite's backup API waits for a write lock for
        # as long as one is held, and a backup thread parked on a lock forever
        # looks exactly like a slow one while holding the single-active-job
        # constraint shut. Better to fail and say why.
        source = sqlite3.connect(str(name), uri=uri, timeout=30)
        target = sqlite3.connect(str(dest))
        try:
            with target:
                source.backup(target)
        finally:
            target.close()
            source.close()
        return 'sqlite-file'

    raise RuntimeError(f'no dump strategy for database engine {connection.vendor!r}')


def _add_media(tar: tarfile.TarFile, job, media: dict) -> int:
    """Copy every media file into the archive under `media/`.

    Streams object by object rather than syncing the bucket to disk first: the
    task has ~20 GB of ephemeral storage and the archive is already using some
    of it.
    """
    written = 0
    total = max(media.get('files', 0), 1)

    if media.get('store') == 's3':
        import boto3
        client = boto3.client('s3', region_name=getattr(settings, 'AWS_MEDIA_REGION', None))
        paginator = client.get_paginator('list_objects_v2')
        with tempfile.TemporaryDirectory() as scratch:
            for page in paginator.paginate(Bucket=settings.AWS_MEDIA_BUCKET):
                for obj in page.get('Contents', []):
                    key = obj['Key']
                    if key.endswith('/'):
                        continue
                    local = Path(scratch) / 'object'
                    client.download_file(settings.AWS_MEDIA_BUCKET, key, str(local))
                    tar.add(local, arcname=f'media/{key}')
                    local.unlink(missing_ok=True)
                    written += 1
                    if written % 25 == 0:
                        _touch(job, stage=f'media {written}/{total}',
                               progress=30 + int(60 * written / total))
        return written

    root = Path(settings.MEDIA_ROOT)
    if not root.exists():
        return 0
    for path in sorted(root.rglob('*')):
        if not path.is_file():
            continue
        tar.add(path, arcname=f'media/{path.relative_to(root)}')
        written += 1
        if written % 25 == 0:
            _touch(job, stage=f'media {written}/{total}',
                   progress=30 + int(60 * written / total))
    return written


def _touch(job, *, stage: str | None = None, progress: int | None = None):
    fields = []
    if stage is not None:
        job.stage = stage[:120]
        fields.append('stage')
    if progress is not None:
        job.progress = max(0, min(100, progress))
        fields.append('progress')
    if fields:
        job.save(update_fields=fields)


def _store(job, archive: Path) -> str:
    """Put the finished archive where the download view will look for it."""
    name = archive.name
    bucket = backup_bucket()
    if bucket:
        import boto3
        client = boto3.client('s3', region_name=getattr(settings, 'AWS_REGION', None)
                              or getattr(settings, 'AWS_MEDIA_REGION', None))
        key = f'{OPS_PREFIX}/{name}'
        client.upload_file(str(archive), bucket, key,
                           ExtraArgs={'ServerSideEncryption': 'AES256'})
        return key

    destination = backup_root() / name
    shutil.move(str(archive), destination)
    return str(destination)


def build(job) -> None:
    """Take the backup. Runs in a thread; never raises into the caller."""
    job.status = job.Status.RUNNING
    job.started_at = timezone.now()
    job.stage = 'starting'
    job.save(update_fields=['status', 'started_at', 'stage'])

    stamp = timezone.now().strftime('%Y%m%d-%H%M%S')
    name = f'aitutor-backup-{stamp}.tar.gz'

    try:
        counts = inventory()
        _touch(job, stage='dumping database', progress=5)

        with tempfile.TemporaryDirectory() as scratch:
            scratch_path = Path(scratch)
            dump_path = scratch_path / ('db.dump' if connection.vendor == 'postgresql'
                                        else 'db.sqlite3')
            dump_format = _dump_database(dump_path, job)
            _touch(job, stage='writing archive', progress=25)

            manifest = {
                'created_at': timezone.now().isoformat(),
                'scope': 'platform',
                'created_by': getattr(job.created_by, 'username', None),
                'database': {**counts['database'], 'dump_format': dump_format,
                             'dump_file': dump_path.name},
                'media': counts['media'],
                'app_version': _app_version(),
                'restore': _restore_instructions(dump_format, dump_path.name),
            }

            archive_path = scratch_path / name
            with tarfile.open(archive_path, 'w:gz') as tar:
                tar.add(dump_path, arcname=dump_path.name)
                media_written = _add_media(tar, job, counts['media'])
                manifest['media']['files_archived'] = media_written

                manifest_path = scratch_path / 'manifest.json'
                manifest_path.write_text(json.dumps(manifest, indent=2, default=str))
                tar.add(manifest_path, arcname='manifest.json')

            _touch(job, stage='uploading', progress=92)
            size = archive_path.stat().st_size
            key = _store(job, archive_path)

        job.status = job.Status.DONE
        job.storage_key = key
        job.size_bytes = size
        job.summary = manifest
        job.stage = 'done'
        job.progress = 100
        job.finished_at = timezone.now()
        job.save()
        logger.info('backup %s complete: %s (%s bytes)', job.pk, key, size)

    except Exception as exc:                           # noqa: BLE001 - recorded on the row
        logger.exception('backup %s failed', job.pk)
        job.status = job.Status.FAILED
        job.error = str(exc)[:2000]
        job.stage = 'failed'
        job.finished_at = timezone.now()
        job.save(update_fields=['status', 'error', 'stage', 'finished_at'])


def start(job) -> None:
    """Run build() on a daemon thread, as content generation does."""
    thread = threading.Thread(target=build, args=(job,), daemon=True,
                              name=f'backup-{job.pk}')
    thread.start()


def _restore_instructions(dump_format: str, dump_file: str) -> str:
    """How to put this archive back, written for whoever opens it cold.

    Carried inside the archive rather than left in a runbook: the person doing
    the restore may be reading a file handed to them years from now, with no
    access to this repository and no idea which engine produced it.
    """
    if dump_format == 'pg_dump-custom':
        return (
            f'pg_restore --clean --no-owner --dbname=<target> {dump_file}, then '
            'copy media/ back into the media store (S3 bucket or MEDIA_ROOT). '
            'Restore both halves from the same archive: the database holds the '
            'reference to every uploaded file and the store holds the file.'
        )
    return (
        f'Put {dump_file} where DATABASE_URL/settings point (SQLite file), then '
        'copy media/ back into MEDIA_ROOT. Restore both halves from the same '
        'archive: the database holds the reference to every uploaded file and '
        'the store holds the file.'
    )


def _app_version() -> str:
    try:
        return (Path(settings.BASE_DIR) / 'VERSION').read_text().strip()
    except Exception:                                  # noqa: BLE001
        return ''


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

def download_url(job) -> str | None:
    """A presigned URL for an archive in the bucket; None when it is on disk."""
    bucket = backup_bucket()
    if not bucket or not job.storage_key or job.storage_key.startswith('/'):
        return None
    import boto3
    client = boto3.client('s3', region_name=getattr(settings, 'AWS_REGION', None)
                          or getattr(settings, 'AWS_MEDIA_REGION', None))
    return client.generate_presigned_url(
        'get_object',
        Params={'Bucket': bucket, 'Key': job.storage_key},
        ExpiresIn=DOWNLOAD_URL_TTL_SECONDS,
    )
