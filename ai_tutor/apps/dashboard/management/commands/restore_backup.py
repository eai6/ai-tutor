"""Put an archive back. DESTRUCTIVE — this is the command that drops the database.

A port of ops/restore_from_dump.sh and ops/restore_inner.py, which are not a
rough draft to improve on but a record of things that went wrong. Every step
below that looks paranoid is there because the un-paranoid version failed:

  - The service is scaled to zero because a database cannot be dropped while
    the connection pool holds it open.
  - The cluster is waited on until IDLE because on 2026-08-08 a deploy's migrate
    task survived the scale-down, reconnected to the recreated database, and
    raced pg_restore into a primary-key collision.
  - The vector extension is created BEFORE the restore because otherwise the
    pgvector table fails quietly and the app looks healthy until a knowledge
    base search returns nothing.
  - --no-owner --no-acl because the RDS master is rds_superuser, not a
    superuser, and restoring ownership of extension members raises.

What this adds over the hand-run script is the four things a laptop operator did
themselves: taking a safety copy first, handling media, catching the schema up,
and reporting progress somewhere that survives the database being dropped.

Read memory/platform_restore_plan.md before changing the order of anything here.
"""
from __future__ import annotations

import os
import re
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, connections
from django.utils import timezone

from ai_tutor.apps.dashboard import backup as backup_service
from ai_tutor.apps.dashboard import restore as restore_service
from ai_tutor.apps.dashboard import restore_lock
from ai_tutor.apps.dashboard.models import BackupJob, RestoreJob

# How long to wait for the service to drain and the cluster to go quiet before
# giving up. Generous: deregistration_delay alone is 120s. Finite, because
# refusing to restore is recoverable and restoring into a live database is not.
IDLE_TIMEOUT_SECONDS = 15 * 60
IDLE_POLL_SECONDS = 10

# pg_restore exits non-zero when it merely IGNORED errors, which --no-owner
# --no-acl on RDS reliably produces. Treating that as failure would abort a
# restore that actually worked, after the database had already been dropped.
IGNORED_ERRORS = re.compile(r'errors ignored on restore:\s*(\d+)', re.I)
# Above this, "ignored" stops meaning noise and starts meaning the dump did not
# land. The row-count gate is the real check; this is the cheap one.
MAX_IGNORED_ERRORS = 100


class Command(BaseCommand):
    help = 'Restore the platform from a backup archive. Destroys current data.'

    def add_arguments(self, parser):
        parser.add_argument('--job', type=int, required=True,
                            help='RestoreJob id. Everything else is read from '
                                 'that row, so a tampered container override '
                                 'cannot redirect this at a different archive.')

    # -- plumbing ---------------------------------------------------------

    def say(self, message: str) -> None:
        # print+flush, not logger: background output in this codebase does not
        # reliably reach the log in every environment it runs in, and these are
        # the lines someone reads during an incident.
        print(f'[restore] {message}', flush=True)

    def step(self, job, stage: str, progress: int, **extra) -> None:
        """Record progress in BOTH places, while both still exist."""
        self.say(f'{stage} ({progress}%)')
        restore_service.write_status(
            job, state='running', stage=stage, progress=progress, **extra)
        try:
            job.stage, job.progress = stage[:120], progress
            job.save(update_fields=['stage', 'progress'])
        except Exception:                              # noqa: BLE001
            # Expected once the database is gone. The status object is the
            # authoritative feed precisely because this stops working.
            pass

    # -- ECS ---------------------------------------------------------------

    def _ecs(self):
        import boto3
        return boto3.client('ecs', region_name=os.getenv('AWS_REGION', 'us-east-1'))

    def _autoscaling(self):
        import boto3
        return boto3.client('application-autoscaling',
                            region_name=os.getenv('AWS_REGION', 'us-east-1'))

    def _scalable_target_id(self, cluster: str, service: str) -> str:
        return f'service/{cluster}/{service}'

    def suspend_autoscaling(self, cluster: str, service: str) -> dict | None:
        """Stop Application Auto Scaling putting tasks back mid-restore.

        Setting desiredCount to zero by hand does NOT deregister the scalable
        target, and min_capacity is a floor the next scaling activity restores.
        Zero running tasks is itself a state that produces one. If it fires
        while pg_restore is loading, a web task boots, DROP DATABASE WITH
        (FORCE) cuts it off, it reconnects to the new empty database, and that
        is the 2026-08-08 incident reached by a different road.

        Returns the PRIOR suspended state so it can be put back as it was
        rather than blanket-enabled.
        """
        client = self._autoscaling()
        resource_id = self._scalable_target_id(cluster, service)
        described = client.describe_scalable_targets(
            ServiceNamespace='ecs', ResourceIds=[resource_id],
            ScalableDimension='ecs:service:DesiredCount')
        targets = described.get('ScalableTargets') or []
        if not targets:
            self.say('no scalable target registered; nothing to suspend')
            return None

        previous = targets[0].get('SuspendedState') or {}
        client.register_scalable_target(
            ServiceNamespace='ecs', ResourceId=resource_id,
            ScalableDimension='ecs:service:DesiredCount',
            SuspendedState={'DynamicScalingInSuspended': True,
                            'DynamicScalingOutSuspended': True,
                            'ScheduledScalingSuspended': True})
        self.say(f'autoscaling suspended (was {previous or "not suspended"})')
        return previous

    def resume_autoscaling(self, cluster: str, service: str, previous: dict | None):
        if previous is None:
            return
        try:
            self._autoscaling().register_scalable_target(
                ServiceNamespace='ecs',
                ResourceId=self._scalable_target_id(cluster, service),
                ScalableDimension='ecs:service:DesiredCount',
                SuspendedState={
                    'DynamicScalingInSuspended': previous.get(
                        'DynamicScalingInSuspended', False),
                    'DynamicScalingOutSuspended': previous.get(
                        'DynamicScalingOutSuspended', False),
                    'ScheduledScalingSuspended': previous.get(
                        'ScheduledScalingSuspended', False)})
            self.say('autoscaling resumed')
        except Exception as exc:                       # noqa: BLE001
            # Loud, because a platform left with autoscaling suspended will not
            # scale under load and nothing else will ever mention it.
            self.say(f'WARNING: could not resume autoscaling: {exc}')
            self.say(f'  fix with: aws application-autoscaling '
                     f'register-scalable-target --service-namespace ecs '
                     f'--resource-id {self._scalable_target_id(cluster, service)} '
                     f'--scalable-dimension ecs:service:DesiredCount '
                     f'--suspended-state '
                     f'DynamicScalingInSuspended=false,'
                     f'DynamicScalingOutSuspended=false,'
                     f'ScheduledScalingSuspended=false')

    def scale_service(self, cluster: str, service: str, count: int) -> None:
        self._ecs().update_service(cluster=cluster, service=service,
                                   desiredCount=count)
        self.say(f'{service} desiredCount -> {count}')

    def current_desired_count(self, cluster: str, service: str) -> int:
        described = self._ecs().describe_services(cluster=cluster,
                                                  services=[service])
        services = described.get('services') or []
        if not services:
            raise CommandError(f'no such ECS service: {service}')
        return int(services[0].get('desiredCount', 1))

    def wait_for_idle(self, cluster: str) -> None:
        """Block until nothing but this task is running in the cluster.

        Excluding our own ARN is not a nicety. list_tasks returns every task in
        the cluster including the one asking, so a naive count waits for itself
        to exit and times out on every single run.
        """
        client = self._ecs()
        mine = restore_lock.own_task_arn()
        deadline = time.monotonic() + IDLE_TIMEOUT_SECONDS

        while time.monotonic() < deadline:
            others = []
            for desired in ('RUNNING', 'PENDING'):
                page = client.list_tasks(cluster=cluster, desiredStatus=desired)
                others.extend(a for a in page.get('taskArns', []) if a != mine)
            if not others:
                self.say('cluster is idle')
                return
            self.say(f'waiting for {len(others)} other task(s) to finish')
            time.sleep(IDLE_POLL_SECONDS)

        raise CommandError(
            'The cluster is still busy after '
            f'{IDLE_TIMEOUT_SECONDS // 60} minutes. Refusing to restore into a '
            'database something else is using — that is how the 2026-08-08 '
            'collision happened. Nothing has been changed.')

    # -- the database ------------------------------------------------------

    def restore_postgres(self, dump_path: Path) -> None:
        database_url = os.environ.get('DATABASE_URL') or ''
        if not database_url:
            raise CommandError('DATABASE_URL is not set; cannot restore Postgres.')

        parsed = urlparse(database_url)
        dbname = parsed.path.lstrip('/')
        # You cannot drop the database you are connected to, so administrative
        # statements go to `postgres`. Rebuilt with urlunparse rather than
        # string surgery: the password is percent-encoded and naive replacement
        # corrupts it.
        admin_dsn = urlunparse(parsed._replace(path='/postgres'))

        # Django's own pool must let go first, or DROP has one more backend to
        # terminate and any lazy reconnect lands in the wrong database.
        connections.close_all()

        self.say(f'dropping and recreating {dbname}')
        # WITH (FORCE) terminates lingering backends. The service is at zero by
        # now, but RDS can hold a session briefly after.
        self._psql(admin_dsn, f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE);')
        self._psql(admin_dsn, f'CREATE DATABASE "{dbname}";')

        # BEFORE pg_restore, not after. curriculum_curriculumchunk holds a
        # 384-d vector column with an HNSW index; without the extension the
        # restore of that table fails, and it fails quietly enough that the app
        # looks healthy until a knowledge-base search silently returns nothing.
        self.say('creating the vector extension')
        self._psql(database_url, 'CREATE EXTENSION IF NOT EXISTS vector;')

        self.say('pg_restore')
        result = subprocess.run(
            ['pg_restore', '--no-owner', '--no-acl', '-j', '4',
             '-d', database_url, str(dump_path)],
            capture_output=True, text=True)
        stderr = (result.stderr or '').strip()

        ignored = IGNORED_ERRORS.search(stderr)
        if result.returncode != 0 and not ignored:
            raise CommandError(f'pg_restore failed: {stderr[:2000]}')
        if ignored:
            count = int(ignored.group(1))
            self.say(f'pg_restore ignored {count} error(s)')
            if count > MAX_IGNORED_ERRORS:
                raise CommandError(
                    f'pg_restore ignored {count} errors, which is past the point '
                    f'where that means harmless ownership noise. The dump may '
                    f'not have landed. Stderr: {stderr[:1500]}')

    def _psql(self, dsn: str, sql: str) -> str:
        result = subprocess.run(
            ['psql', dsn, '-v', 'ON_ERROR_STOP=1', '-t', '-A', '-c', sql],
            capture_output=True, text=True)
        if result.returncode != 0:
            raise CommandError(f'psql failed on {sql[:60]!r}: '
                               f'{(result.stderr or "").strip()[:800]}')
        return (result.stdout or '').strip()

    def restore_sqlite(self, dump_path: Path) -> None:
        """Dev, and Path A on one server.

        Through SQLite's own backup API in reverse — archived file as source,
        live file as target — rather than swapping the file. The dev server may
        still hold the database open, and NAME can be a
        `file:...?mode=memory&cache=shared` URI, where "replace the file" means
        nothing at all.
        """
        import sqlite3
        name = connections['default'].settings_dict['NAME']
        connections.close_all()

        uri = isinstance(name, str) and str(name).startswith('file:')
        source = sqlite3.connect(str(dump_path))
        target = sqlite3.connect(str(name), uri=uri, timeout=30)
        try:
            with target:
                source.backup(target)
        finally:
            target.close()
            source.close()
        self.say('sqlite database replaced from the archive')

    # -- the archive -------------------------------------------------------

    def fetch_archive(self, key: str, scratch: Path) -> Path:
        """Bring the archive to local disk, once.

        download_file rather than streaming: it is multipart, parallel and
        retries per part. A gzip stream cannot be resumed from an offset, so one
        timeout twenty minutes into a 9.3 GB read — after the database has
        already been dropped — would mean starting over from zero. Having it on
        disk also makes the tar seekable, which collapses what would otherwise
        be two full passes into one.
        """
        bucket = backup_service.backup_bucket()
        if not bucket or key.startswith('/'):
            local = Path(key)
            if not local.is_file():
                raise CommandError(f'archive not found: {key}')
            return local

        import boto3
        client = boto3.client('s3', region_name=getattr(settings, 'AWS_REGION', None)
                              or getattr(settings, 'AWS_MEDIA_REGION', None))
        local = scratch / Path(key).name
        self.say(f'downloading s3://{bucket}/{key}')
        client.download_file(bucket, key, str(local))
        self.say(f'  {local.stat().st_size / 1_048_576:.1f} MB')
        return local

    def extract_dump(self, archive: Path, manifest: dict, scratch: Path) -> Path:
        import hashlib
        dump_name = (manifest.get('database') or {}).get('dump_file') or 'db.dump'
        with tarfile.open(archive) as tar:
            try:
                member = tar.getmember(dump_name)
            except KeyError:
                raise CommandError(
                    f'the archive does not contain {dump_name!r}, which its own '
                    f'manifest says it should') from None
            dump_path = scratch / dump_name
            with tar.extractfile(member) as src, dump_path.open('wb') as dst:
                digest = hashlib.sha256()
                for chunk in iter(lambda: src.read(1024 * 1024), b''):
                    digest.update(chunk)
                    dst.write(chunk)

        expected = (manifest.get('database') or {}).get('dump_sha256')
        if expected:
            if digest.hexdigest() != expected:
                raise CommandError(
                    'The dump inside this archive does not match the checksum '
                    'recorded when it was taken. It is corrupt or truncated. '
                    'NOTHING HAS BEEN CHANGED.')
            self.say('dump checksum verified')
        else:
            self.say('WARNING: this archive records no checksum; cannot verify it')
        return dump_path

    def restore_media(self, job, archive: Path, manifest: dict) -> int:
        """Upload every media/ member back into the media store.

        Orphans are left in place rather than deleted. An extra file is
        harmless; a wrongly deleted one is not, and the safety copy is
        database-only.
        """
        dump_name = (manifest.get('database') or {}).get('dump_file') or 'db.dump'
        bucket = getattr(settings, 'AWS_MEDIA_BUCKET', '')
        use_s3 = bool(getattr(settings, 'USE_S3_MEDIA', False) and bucket)
        client = None
        if use_s3:
            import boto3
            client = boto3.client(
                's3', region_name=getattr(settings, 'AWS_MEDIA_REGION', None))

        written = 0
        with tarfile.open(archive) as tar:
            members = [m for m in tar.getmembers()
                       if m.name not in (dump_name, 'manifest.json',
                                         'manifest-final.json')]
            total = max(len(members), 1)
            for member in members:
                # Raises on anything that is not a regular file under media/.
                key = restore_service.media_key_for(member, dump_name)
                if key is None:
                    continue
                source = tar.extractfile(member)
                if use_s3:
                    client.upload_fileobj(
                        source, bucket, key,
                        ExtraArgs={'ServerSideEncryption': 'AES256'})
                else:
                    destination = Path(settings.MEDIA_ROOT) / key
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with destination.open('wb') as out:
                        for chunk in iter(lambda: source.read(1024 * 1024), b''):
                            out.write(chunk)
                written += 1
                if written % 50 == 0:
                    self.step(job, f'media {written}/{total}',
                              80 + int(15 * written / total))
        return written

    # -- after ------------------------------------------------------------

    def catch_the_schema_up(self) -> None:
        """migrate_and_seed.sh, not a bare migrate.

        The archive may predate the deployed code, and the seed chain rebuilds
        derived data the restored rows may not match. Reusing the script keeps
        this in step with what CI runs. Running it HERE — single, serialised,
        service at zero — is the safe place for build_help_index, which is the
        command that collided on 2026-08-08.
        """
        script = Path(settings.BASE_DIR) / 'ops' / 'migrate_and_seed.sh'
        if script.is_file():
            self.say('ops/migrate_and_seed.sh')
            result = subprocess.run(['sh', str(script)], cwd=str(settings.BASE_DIR),
                                    capture_output=True, text=True)
            if result.returncode != 0:
                raise CommandError(
                    'migrate/seed failed after the restore. The database holds '
                    'the archive\'s schema but not this code\'s: '
                    f'{(result.stderr or "")[-2000:]}')
            return

        # Packaged installs have no ops/ directory. Migrate is the part that
        # must happen; the seeds are derived data.
        self.say('migrate (no ops/migrate_and_seed.sh in this install)')
        from django.core.management import call_command
        call_command('migrate', interactive=False, verbosity=1)

    def verify(self, manifest: dict) -> list[str]:
        """Row-count floors from the archive's own manifest.

        Not the 2026-07-30 drill constants ops/restore_inner.py carries: those
        describe one moment of one database and go stale. The archive states
        what it contained, so the archive is what to check against.

        Half, because the counts were taken before the dump ran and a busy
        platform moves between the two. A number wildly below is the signal —
        it means the restore only half happened, which otherwise looks exactly
        like success.
        """
        expected = (manifest.get('database') or {}).get('row_counts') or {}
        live = backup_service._database_inventory().get('row_counts', {})
        problems = []
        for table, count in sorted(expected.items()):
            if count <= 0:
                continue
            actual = live.get(table)
            if actual is None:
                problems.append(f'{table} is missing entirely')
            elif actual < count * 0.5:
                problems.append(f'{table}={actual}, archive held {count}')
        return problems

    def record_the_restore(self, job, manifest: dict, safety_key: str,
                           snapshot_id: str) -> None:
        """Write the account of this restore into the database it just restored.

        The row that started this was dropped along with everything else, and
        what came back is the archive's version of the table — which knows
        nothing about any of it. Without this, a successful restore leaves no
        trace that it happened.
        """
        from ai_tutor.apps.safety.models import SafetyAuditLog

        # Rows the archive resurrected mid-flight. build() marks a BackupJob
        # RUNNING before it dumps, so any archive taken less than STALE_AFTER
        # ago contains its own unfinished row — which then holds the
        # one-active-backup constraint shut and parks the settings page on "a
        # backup is already running" for six hours.
        # One of those rows is special: the archive's own. A dump taken by
        # build() always contains the very row describing the backup that was
        # running when it was taken — and that backup demonstrably finished,
        # because we just restored from what it produced. Marking it "failed"
        # alongside the others would put a red Failed row in the list for the
        # exact archive the admin successfully restored from, which reads as
        # though the restore went wrong. It is identified by primary key: the
        # dump carries the row under the same pk the source BackupJob has here.
        if job.source_backup_id:
            recovered = BackupJob.objects.filter(
                pk=job.source_backup_id,
                status__in=(BackupJob.Status.PENDING, BackupJob.Status.RUNNING),
            ).update(
                status=BackupJob.Status.DONE, stage='done', progress=100,
                storage_key=job.source_key,
                size_bytes=getattr(self, '_source_bytes', 0),
                summary=manifest, finished_at=timezone.now())
            if recovered:
                self.say('restored the source archive\'s own record')

        for model in (BackupJob, RestoreJob):
            stuck = model.objects.filter(
                status__in=(model.Status.PENDING, model.Status.RUNNING))
            cleared = stuck.update(
                status=model.Status.FAILED, stage='interrupted',
                error='Interrupted by a platform restore.',
                finished_at=timezone.now())
            if cleared:
                self.say(f'cleared {cleared} interrupted {model.__name__} row(s)')

        restored = RestoreJob.objects.create(
            status=RestoreJob.Status.DONE,
            source=job.source, source_key=job.source_key,
            include_media=job.include_media,
            manifest=manifest, preflight=job.preflight,
            safety_backup_key=safety_key, rds_snapshot_id=snapshot_id,
            task_arn=job.task_arn,
            started_at=job.started_at, finished_at=timezone.now(),
            stage='done', progress=100,
        )

        # And put the safety copy back in the list, so it is downloadable from
        # the settings page instead of being an S3 key somebody has to presign
        # by hand on the worst day of their week.
        if safety_key:
            safety_bytes = getattr(self, '_safety_bytes', 0)
            if not safety_bytes:
                try:
                    safety_bytes = Path(safety_key).stat().st_size
                except OSError:
                    safety_bytes = 0      # an S3 key, not a path
            # Its REAL manifest, not a stub. The safety copy is the archive
            # somebody reaches for when a restore turned out to be the wrong
            # one, and a row without a manifest cannot be preflighted — which
            # would make it the one archive they cannot put back.
            summary = dict(getattr(self, '_safety_summary', None) or
                           {'scope': 'platform'})
            summary['note'] = (f'Taken automatically before restore '
                               f'{restored.pk}.')
            BackupJob.objects.create(
                status=BackupJob.Status.DONE, storage_key=safety_key,
                size_bytes=safety_bytes,
                include_media=False, stage='pre-restore safety copy',
                progress=100, finished_at=timezone.now(),
                summary=summary)

        SafetyAuditLog.objects.create(
            event_type=SafetyAuditLog.EventType.DATA_EXPORT,
            user_id=None, severity='critical',
            details={'action': 'platform_restore',
                     'restore_job': restored.pk,
                     'source_key': job.source_key,
                     'safety_backup_key': safety_key,
                     'rds_snapshot_id': snapshot_id},
        )

    # -- the whole thing ---------------------------------------------------

    def handle(self, *args, **options):
        job = RestoreJob.objects.filter(pk=options['job']).first()
        if job is None:
            raise CommandError(f'no RestoreJob {options["job"]}')
        if job.is_finished:
            raise CommandError(f'restore {job.pk} already {job.status}')

        # Checked here as well as in dispatch(), because this command can be
        # reached directly — a hand-run task, a retry, someone on a bastion —
        # and `on_ecs` silently reading False is the difference between stopping
        # the platform first and dropping the database out from under the tasks
        # still serving it.
        missing = restore_service.required_settings_missing()
        if missing:
            raise CommandError(
                'This platform runs on ECS but the restore is not configured: '
                + ', '.join(missing) + ' unset. Refusing to continue: without '
                'these the platform cannot be stopped first, and the database '
                'would be dropped while tasks are still using it. NOTHING HAS '
                'BEEN CHANGED.')

        cluster = os.getenv('ECS_CLUSTER', '')
        service = os.getenv('ECS_SERVICE', '')
        on_ecs = bool(cluster and service)

        job.status = RestoreJob.Status.RUNNING
        job.started_at = timezone.now()
        job.task_arn = job.task_arn or (restore_lock.own_task_arn() or '')
        job.save(update_fields=['status', 'started_at', 'task_arn'])

        try:
            with restore_lock.exclusive(job):
                self._run(job, cluster, service, on_ecs)
        except restore_lock.RestoreLocked as exc:
            self._fail(job, f'Refused: {exc}', destructive=False)
            raise CommandError(str(exc)) from None
        except Exception as exc:                       # noqa: BLE001
            self._fail(job, str(exc), destructive=True)
            raise

    def _run(self, job, cluster: str, service: str, on_ecs: bool) -> None:
        manifest, _ = restore_service.manifest_for(
            backup=job.source_backup, key=job.source_key)

        desired = self.current_desired_count(cluster, service) if on_ecs else 0
        suspended_state = None

        # 1. Record where everything is BEFORE anything is destroyed, including
        #    the count needed to put the service back. A task that dies after
        #    this point is recoverable with one command; one that dies before
        #    writing it is a puzzle.
        restore_service.write_status(
            job, state='starting', stage='preparing', progress=0,
            source_key=job.source_key, original_desired_count=desired,
            recovery=(f'aws ecs update-service --cluster {cluster} '
                      f'--service {service} --desired-count {desired}'
                      if on_ecs else ''))

        # 2. Two safety nets, both before the first destructive act.
        snapshot_id = self.take_rds_snapshot(job) if on_ecs else ''
        safety_key = self.take_safety_backup(job)

        try:
            if on_ecs:
                # 3. Autoscaling first, THEN scale down. The other order leaves
                #    a window where the floor puts tasks straight back.
                self.step(job, 'suspending autoscaling', 20)
                suspended_state = self.suspend_autoscaling(cluster, service)
                self.step(job, 'stopping the platform', 25,
                          original_desired_count=desired)
                self.scale_service(cluster, service, 0)
                self.wait_for_idle(cluster)

            with tempfile.TemporaryDirectory() as scratch_dir:
                scratch = Path(scratch_dir)
                self.step(job, 'fetching the archive', 35)
                archive = self.fetch_archive(job.source_key, scratch)
                # Kept for the row re-inserted afterwards: the dump contains
                # this archive's own BackupJob record as it was mid-backup,
                # before build() wrote the size onto it, so without this it
                # comes back showing "—" in the list.
                self._source_bytes = archive.stat().st_size

                self.step(job, 'verifying the dump', 45)
                dump_path = self.extract_dump(archive, manifest, scratch)

                # 4. Last chance to notice something else woke up. A deploy that
                #    started after the idle wait would be writing right now.
                if on_ecs:
                    self.wait_for_idle(cluster)

                self.step(job, 'restoring the database', 55)
                if connection.vendor == 'postgresql':
                    self.restore_postgres(dump_path)
                elif connection.vendor == 'sqlite':
                    self.restore_sqlite(dump_path)
                else:
                    raise CommandError(
                        f'no restore strategy for {connection.vendor!r}')

                self.step(job, 'catching the schema up', 70)
                self.catch_the_schema_up()

                if job.include_media and manifest.get('media', {}).get('included'):
                    self.step(job, 'restoring uploaded files', 80)
                    count = self.restore_media(job, archive, manifest)
                    self.say(f'{count} media file(s) restored')

            self.step(job, 'verifying', 95)
            problems = self.verify(manifest)
            if problems:
                raise CommandError(
                    'The restore finished but the result does not look like the '
                    'archive: ' + '; '.join(problems) + '. The database is NOT '
                    'being served — see the safety copy on the status object.')

            self.record_the_restore(job, manifest, safety_key, snapshot_id)

            restore_service.write_status(
                job, state='done', stage='done', progress=100,
                safety_backup_key=safety_key, rds_snapshot_id=snapshot_id,
                finished_at=timezone.now().isoformat())
            self.say('restore complete')

            # 5. Only now. See _fail() for why this is not in a finally.
            if on_ecs:
                self.scale_service(cluster, service, desired)

        finally:
            if on_ecs:
                # Autoscaling IS un-suspended unconditionally: leaving it
                # suspended breaks the platform's ability to cope with load
                # whether or not the restore worked, and un-suspending a
                # service sitting at zero puts nothing back by itself.
                self.resume_autoscaling(cluster, service, suspended_state)

    def take_rds_snapshot(self, job) -> str:
        """The safety net that cannot half-succeed.

        Atomic, minutes rather than the half-hour a dump of this size takes, and
        it does not depend on the application being healthy. The archive below
        is the portable copy; this is the one to actually roll back to.
        """
        instance = os.getenv('RDS_INSTANCE_IDENTIFIER', '')
        if not instance:
            self.say('no RDS_INSTANCE_IDENTIFIER; skipping the snapshot')
            return ''
        snapshot_id = (f'{instance}-pre-restore-{job.pk}-'
                       f'{timezone.now():%Y%m%d%H%M%S}')
        self.step(job, 'snapshotting the database', 5, rds_snapshot_id=snapshot_id)
        import boto3
        client = boto3.client('rds', region_name=os.getenv('AWS_REGION', 'us-east-1'))
        client.create_db_snapshot(DBInstanceIdentifier=instance,
                                  DBSnapshotIdentifier=snapshot_id)
        client.get_waiter('db_snapshot_available').wait(
            DBSnapshotIdentifier=snapshot_id,
            WaiterConfig={'Delay': 15, 'MaxAttempts': 80})
        self.say(f'snapshot {snapshot_id} available')
        job.rds_snapshot_id = snapshot_id
        job.save(update_fields=['rds_snapshot_id'])
        return snapshot_id

    def take_safety_backup(self, job) -> str:
        """A portable copy of what is about to be destroyed.

        build() NEVER raises — it records failure on its row and returns — so a
        blocking call returning is not evidence the archive exists. The status
        has to be read back, and a failure here has to stop everything. This is
        the check whose absence would turn "we restored the wrong archive" from
        recoverable into permanent.
        """
        self.step(job, 'taking a safety copy', 10)
        backup_service.reap_stale()
        try:
            safety = BackupJob.objects.create(include_media=False)
        except Exception as exc:                       # noqa: BLE001
            raise CommandError(
                f'could not start the safety backup ({exc}). Refusing to '
                f'restore without one.') from None

        backup_service.build(
            safety, prefix=f'{backup_service.OPS_PREFIX}/pre-restore/{job.pk}')
        safety.refresh_from_db()
        if safety.status != BackupJob.Status.DONE or not safety.storage_key:
            raise CommandError(
                f'the safety backup failed ({safety.error or "no reason given"}). '
                f'NOTHING HAS BEEN CHANGED — refusing to destroy the current '
                f'data without a copy of it.')

        self.say(f'safety copy at {safety.storage_key}')
        job.safety_backup_key = safety.storage_key
        job.save(update_fields=['safety_backup_key'])
        restore_service.write_status(job, state='running',
                                     stage='safety copy taken', progress=15,
                                     safety_backup_key=safety.storage_key)
        # Its manifest and size are kept so the row re-inserted after the
        # restore is a real backup record rather than a stub. A stub cannot be
        # preflighted, which would make the one archive somebody reaches for in
        # a hurry the one they cannot restore.
        self._safety_summary = safety.summary
        self._safety_bytes = safety.size_bytes
        return safety.storage_key

    def _fail(self, job, message: str, *, destructive: bool) -> None:
        """Record a failure, and deliberately leave the service where it is.

        Scaling back up after a failed restore is not a kindness. If pg_restore
        failed the tasks boot, /health/ returns 503 because the database is
        unreachable, and ECS kills and replaces them forever. Worse is the
        partial case: migrate succeeded and pg_restore did not, so there is a
        valid EMPTY schema, gunicorn reports healthy, and students and staff
        start writing rows into an empty platform — which a second restore
        attempt then destroys too. A clean 503 is recoverable. A healthy empty
        platform is not.
        """
        self.say(f'FAILED: {message}')
        if destructive:
            self.say('the platform has been left stopped ON PURPOSE: a half-'
                     'restored database that reports healthy is worse than one '
                     'that is plainly down. Check the status object for the '
                     'safety copy and the snapshot before retrying.')
        restore_service.write_status(
            job, state='failed', stage='failed', error=message,
            safety_backup_key=job.safety_backup_key,
            rds_snapshot_id=job.rds_snapshot_id,
            finished_at=timezone.now().isoformat())
        try:
            job.status = RestoreJob.Status.FAILED
            job.error = message[:2000]
            job.stage = 'failed'
            job.finished_at = timezone.now()
            job.save(update_fields=['status', 'error', 'stage', 'finished_at'])
        except Exception:                              # noqa: BLE001
            # If the database is what went away, this cannot work. The status
            # object already has it, which is the whole reason it exists.
            self.say('could not record the failure in the database')
