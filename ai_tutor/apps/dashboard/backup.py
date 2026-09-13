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

import hashlib
import json
import logging
import os
import shutil
import subprocess
import tarfile
import tempfile
import threading
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.core.cache import cache
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

# Manifest shape. 1 was the original — no checksums, no migration heads, and
# `app_version` standing in for a schema version it could not actually report.
# Archives at version 1 are still restorable; a restore just cannot verify them,
# and has to say so rather than imply a check it did not perform.
ARCHIVE_FORMAT_VERSION = 2


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


def _migration_heads() -> dict:
    """The last migration APPLIED per app, straight out of django_migrations.

    This is the archive's schema version, and the only honest one available.
    `VERSION` at the repo root has read 0.1.0 since May and is never bumped, so
    `app_version` in the manifest answers "which code wrote this?" with a
    constant.

    Applied, not what the code ships: the question a restore has to answer is
    "what shape is the data in this dump", and `MigrationLoader.graph` would
    describe the shape of the code doing the reading instead. Sixteen rows, one
    query.

    A dump whose heads are AHEAD of the running code cannot be restored — there
    is no migrating backwards — which is the check this field exists for.
    """
    try:
        with connection.cursor() as cur:
            cur.execute(
                "SELECT app, MAX(name) FROM django_migrations GROUP BY app"
            )
            return {app: name for app, name in cur.fetchall()}
    except Exception as exc:                           # noqa: BLE001 - reported, not raised
        # Never fail a backup over its own metadata. A manifest without heads is
        # a manifest preflight will say it cannot verify, which is the correct
        # outcome and strictly better than no archive at all.
        logger.warning('could not read migration heads: %s', exc)
        return {}


def _sha256(path: Path) -> str:
    """Hash a file in chunks. The dump can be hundreds of MB; do not read it whole."""
    digest = hashlib.sha256()
    with path.open('rb') as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _database_inventory() -> dict:
    """Engine, and a row count for the tables that carry student data.

    The counts are what makes a restore checkable: a manifest saying 23 students
    and 1,412 turns is something you can compare against what came back.

    They are taken BEFORE the dump, so on a large database they describe the
    moment the backup started rather than the dump's exact contents. Good enough
    to catch "this archive restored to a tenth of the rows"; not an integrity
    check. That is what dump_sha256 is for, and the two are labelled differently
    wherever they are shown.
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

    return {'engine': engine, 'bytes': size, 'row_counts': counts,
            'migration_heads': _migration_heads()}


INVENTORY_CACHE_KEY = 'dashboard:backup:inventory'
INVENTORY_CACHE_SECONDS = 15 * 60


def inventory(*, fresh: bool = False) -> dict:
    """What the settings page shows before anyone presses the button.

    Cached, because this is not cheap and the settings page called it on every
    render: a row count on every model in eight app labels, plus — with S3 media
    — a full paginated `list_objects_v2` over the whole bucket, which is eleven
    round-trips for 10,521 objects. All to show two numbers that change slowly.

    Pass fresh=True from build(), where the numbers go into the manifest and a
    15-minute-old count is not good enough.

    Note the cache is per-process: CACHES is unconfigured, so this is Django's
    LocMemCache and each gunicorn worker keeps its own copy. That turns "one
    bucket listing per page view" into "one per worker per 15 minutes", which is
    the whole of the win here. A shared cache would be better and is not needed.
    """
    if not fresh:
        cached = cache.get(INVENTORY_CACHE_KEY)
        if cached is not None:
            return cached

    # A datetime, not a string: the template renders it as "4 minutes ago",
    # which is what someone reading it wants to know. Nothing puts this in the
    # manifest, so it never needs to be JSON.
    data = {'database': _database_inventory(), 'media': _media_inventory(),
            'measured_at': timezone.now()}
    cache.set(INVENTORY_CACHE_KEY, data, INVENTORY_CACHE_SECONDS)
    return data


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


def _store(job, archive: Path, manifest: dict, *, prefix: str = OPS_PREFIX) -> str:
    """Put the finished archive where the download view will look for it.

    Writes a `<key>.manifest.json` sidecar beside it. The manifest is already on
    job.summary, which is how the settings page reads it for nothing — but an
    archive can outlive its row. It is downloaded to a laptop and uploaded back
    months later; the pre-restore safety copy has its row dropped by the very
    restore that took it. The sidecar is how such an archive still says what it
    contains without reading gigabytes to find the copy inside the tar.

    The in-tar manifest stays exactly where it is. Someone handed this file years
    from now, with no bucket and no database, should still be able to open it and
    find out what it holds.

    `prefix` exists for the restore's safety copy, which lands under
    backups/pre-restore/<id>/ so it is not one undated row among the ordinary
    backups at the moment somebody badly needs to find it.
    """
    name = archive.name
    bucket = backup_bucket()
    blob = json.dumps(manifest, indent=2, default=str).encode()
    if bucket:
        import boto3
        client = boto3.client('s3', region_name=getattr(settings, 'AWS_REGION', None)
                              or getattr(settings, 'AWS_MEDIA_REGION', None))
        key = f'{prefix}/{name}'
        client.upload_file(str(archive), bucket, key,
                           ExtraArgs={'ServerSideEncryption': 'AES256'})
        client.put_object(Bucket=bucket, Key=f'{key}.manifest.json', Body=blob,
                          ContentType='application/json',
                          ServerSideEncryption='AES256')
        return key

    destination = backup_root() / name
    shutil.move(str(archive), destination)
    destination.with_name(destination.name + '.manifest.json').write_bytes(blob)
    return str(destination)


def build(job) -> None:
    """Take the backup. Runs in a thread; never raises into the caller."""
    job.status = job.Status.RUNNING
    job.started_at = timezone.now()
    job.stage = 'starting'
    job.save(update_fields=['status', 'started_at', 'stage'])

    stamp = timezone.now().strftime('%Y%m%d-%H%M%S')
    # The kind is in the filename because the file outlives this page. Someone
    # holding aitutor-backup-...-db.tar.gz a year from now should not have to
    # open it to find out the figures are missing.
    kind = 'full' if job.include_media else 'db'
    name = f'aitutor-backup-{stamp}-{kind}.tar.gz'

    try:
        # fresh=True: these numbers go into the manifest and are what a restore
        # is checked against. The page may show a 15-minute-old count; this
        # cannot.
        counts = inventory(fresh=True)
        _touch(job, stage='dumping database', progress=5)

        with tempfile.TemporaryDirectory() as scratch:
            scratch_path = Path(scratch)
            dump_path = scratch_path / ('db.dump' if connection.vendor == 'postgresql'
                                        else 'db.sqlite3')
            dump_format = _dump_database(dump_path, job)
            _touch(job, stage='checksumming', progress=20)
            dump_sha256 = _sha256(dump_path)
            _touch(job, stage='writing archive', progress=25)

            manifest = {
                # Bumped when the shape of this document changes. A restore that
                # meets a version it does not know should say so rather than
                # guess at fields that may not mean what it assumes.
                'archive_format_version': ARCHIVE_FORMAT_VERSION,
                'created_at': timezone.now().isoformat(),
                'scope': 'platform',
                'created_by': getattr(job.created_by, 'username', None),
                'database': {**counts['database'], 'dump_format': dump_format,
                             'dump_file': dump_path.name,
                             'dump_bytes': dump_path.stat().st_size,
                             # The integrity check. row_counts is a sanity
                             # signal taken before the dump ran; this is of the
                             # dump itself. They are not interchangeable and the
                             # UI must not present them as if they were.
                             'dump_sha256': dump_sha256,
                             'row_counts_taken': 'at backup start'},
                'media': counts['media'],
                'app_version': _app_version(),
                'restore': _restore_instructions(dump_format, dump_path.name,
                                                 job.include_media),
            }

            archive_path = scratch_path / name
            with tarfile.open(archive_path, 'w:gz') as tar:
                tar.add(dump_path, arcname=dump_path.name)
                if job.include_media:
                    media_written = _add_media(tar, job, counts['media'])
                else:
                    _touch(job, stage='skipping media', progress=85)
                    media_written = 0
                manifest['media']['included'] = job.include_media
                manifest['media']['files_archived'] = media_written

                manifest_path = scratch_path / 'manifest.json'
                manifest_path.write_text(json.dumps(manifest, indent=2, default=str))
                tar.add(manifest_path, arcname='manifest.json')

            _touch(job, stage='uploading', progress=92)
            size = archive_path.stat().st_size
            # Of the whole archive, so a restore can tell a truncated upload or
            # a bit-rotted copy from a good one BEFORE it drops the live
            # database. dump_sha256 above catches the same for the dump once
            # the archive is open; this catches it one layer earlier.
            archive_sha256 = _sha256(archive_path)
            manifest['archive_sha256'] = archive_sha256
            key = _store(job, archive_path, manifest)

        job.status = job.Status.DONE
        job.storage_key = key
        job.size_bytes = size
        job.archive_sha256 = archive_sha256
        job.summary = manifest
        job.stage = 'done'
        job.progress = 100
        job.finished_at = timezone.now()
        job.save()
        logger.info('backup %s complete: %s (%s bytes)', job.pk, key, size)

    except Exception as exc:                           # noqa: BLE001 - recorded on the row
        logger.exception('backup %s failed', job.pk)
        try:
            job.status = job.Status.FAILED
            job.error = str(exc)[:2000]
            job.stage = 'failed'
            job.finished_at = timezone.now()
            job.save(update_fields=['status', 'error', 'stage', 'finished_at'])
        except Exception:                              # noqa: BLE001
            # The failure handler failing is the case that matters most: if the
            # database is what went away, this save goes with it, the thread
            # dies, and the row stays RUNNING — where the one-active-backup
            # constraint then blocks every future backup for good. Swallow it
            # here and let reap_stale() clear the row instead.
            logger.exception('backup %s: could not record its own failure', job.pk)


# How long a backup may sit in RUNNING before it is presumed dead. Generous:
# a full archive of a large media store is legitimately slow. The point is
# only that it is finite — see reap_stale().
STALE_AFTER = timedelta(hours=6)


def reap_stale() -> int:
    """Mark long-abandoned jobs failed, and return how many.

    A job whose thread died without recording anything — the task was replaced
    mid-backup, the database went away — holds the one-active-backup constraint
    shut forever. Nothing else would ever clear it, and the symptom is a button
    that says "a backup is already running" for the rest of time.
    """
    cutoff = timezone.now() - STALE_AFTER
    from ai_tutor.apps.dashboard.models import BackupJob
    stale = BackupJob.objects.filter(
        status__in=(BackupJob.Status.PENDING, BackupJob.Status.RUNNING),
        created_at__lt=cutoff,
    )
    return stale.update(
        status=BackupJob.Status.FAILED,
        stage='abandoned',
        error=(f'No progress for over {STALE_AFTER}. The process was probably '
               f'replaced mid-backup. Nothing was kept; take another.'),
        finished_at=timezone.now(),
    )


def start(job) -> None:
    """Run build() on a daemon thread, as content generation does."""
    thread = threading.Thread(target=build, args=(job,), daemon=True,
                              name=f'backup-{job.pk}')
    thread.start()


def _restore_instructions(dump_format: str, dump_file: str,
                          include_media: bool = True) -> str:
    """How to put this archive back, written for whoever opens it cold.

    Carried inside the archive rather than left in a runbook: the person doing
    the restore may be reading a file handed to them years from now, with no
    access to this repository and no idea which engine produced it.
    """
    if dump_format == 'pg_dump-custom':
        database = f'pg_restore --clean --no-owner --dbname=<target> {dump_file}'
    else:
        database = (f'Put {dump_file} where DATABASE_URL/settings point '
                    f'(SQLite file)')

    if include_media:
        return (
            f'{database}, then copy media/ back into the media store (S3 bucket '
            'or MEDIA_ROOT). Restore both halves from the same archive: the '
            'database holds the reference to every uploaded file and the store '
            'holds the file.'
        )
    # Said plainly, because this is the archive someone reaches for in a hurry
    # and the failure is silent — lessons render with gaps and nothing errors.
    return (
        f'{database}. THIS ARCHIVE CONTAINS NO MEDIA: the database still holds '
        'a reference to every uploaded figure, so restoring it against an empty '
        'or older media store leaves lessons with broken images and reports no '
        'error. Pair it with a full archive, or with the media store as it '
        'stands now if that is intact.'
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
