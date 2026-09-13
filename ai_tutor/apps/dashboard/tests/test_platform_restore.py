"""Restoring an archive: the bookkeeping, and the guarantee that only one runs.

test_platform_backup.py asserts a backup contains everything. This file starts
from the opposite obligation — a restore destroys everything currently there —
so the things worth testing first are the ones that stop it happening twice, and
the ones that make a restore traceable after the database that recorded it has
been dropped.
"""
from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest
from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.utils import timezone

from ai_tutor.apps.dashboard import restore_lock
from ai_tutor.apps.dashboard.models import BackupJob, RestoreJob


@pytest.fixture
def superadmin(db):
    return User.objects.create_user('root', 'root@example.com', 'pw', is_staff=True)


@pytest.fixture
def lock_dir(db, tmp_path, settings, monkeypatch):
    """Send the file lock somewhere disposable, and keep ECS out of it."""
    settings.BACKUP_ROOT = tmp_path / 'backups'
    settings.AWS_BACKUP_BUCKET = ''
    for var in ('ECS_CLUSTER', 'ECS_RESTORE_FAMILY', 'ECS_MIGRATE_TASK_DEFINITION'):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


@pytest.mark.django_db
class TestOnlyOneRestoreIsDispatched:
    """The half of the guarantee the database can keep — while it still exists."""

    def test_a_second_restore_cannot_be_created_while_one_is_pending(self, db):
        RestoreJob.objects.create(status=RestoreJob.Status.PENDING)
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                RestoreJob.objects.create(status=RestoreJob.Status.PENDING)

    def test_a_running_restore_also_blocks(self, db):
        RestoreJob.objects.create(status=RestoreJob.Status.RUNNING)
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                RestoreJob.objects.create(status=RestoreJob.Status.PENDING)

    def test_a_finished_restore_does_not_block_the_next(self, db):
        RestoreJob.objects.create(status=RestoreJob.Status.DONE)
        RestoreJob.objects.create(status=RestoreJob.Status.FAILED)
        RestoreJob.objects.create(status=RestoreJob.Status.PENDING)
        assert RestoreJob.objects.count() == 3


@pytest.mark.django_db
class TestTheLockOutlivesTheDatabase:
    """The half the database cannot keep.

    A restore drops the table holding the index above. From that moment until a
    row is written back there is no constraint, because there is no table — so
    the guarantee has to be held somewhere else for that window.
    """

    def test_one_restore_holds_it_against_another(self, lock_dir):
        first = RestoreJob.objects.create()
        second = RestoreJob.objects.create(status=RestoreJob.Status.DONE)

        with restore_lock.exclusive(first):
            with pytest.raises(restore_lock.RestoreLocked):
                with restore_lock.exclusive(second):
                    pass

    def test_it_is_released_on_the_way_out(self, lock_dir):
        job = RestoreJob.objects.create()
        with restore_lock.exclusive(job):
            pass
        # A second restore can now take it.
        with restore_lock.exclusive(job):
            pass

    def test_it_is_released_even_when_the_restore_fails(self, lock_dir):
        job = RestoreJob.objects.create()
        with pytest.raises(ValueError):
            with restore_lock.exclusive(job):
                raise ValueError('pg_restore fell over')
        with restore_lock.exclusive(job):
            pass

    def test_a_crashed_holder_does_not_wedge_it_forever(self, lock_dir):
        """The file lock is bounded whether or not anything releases it — a
        SIGKILLed task runs no cleanup, and 'no restores, ever again' is not an
        acceptable resting state."""
        job = RestoreJob.objects.create()
        path = Path(lock_dir) / 'backups' / restore_lock.LOCK_FILENAME
        path.parent.mkdir(parents=True, exist_ok=True)
        stale = timezone.now() - timedelta(hours=1)
        path.write_text(json.dumps({
            'job_id': 999, 'pid': 1,
            'taken_at': stale.isoformat(), 'expires_at': stale.isoformat(),
        }))

        with restore_lock.exclusive(job):
            held = json.loads(path.read_text())
        assert held['job_id'] == job.pk

    def test_an_unexpired_lock_is_respected(self, lock_dir):
        job = RestoreJob.objects.create()
        path = Path(lock_dir) / 'backups' / restore_lock.LOCK_FILENAME
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            'job_id': 999, 'pid': 1,
            'taken_at': timezone.now().isoformat(),
            'expires_at': (timezone.now() + timedelta(hours=5)).isoformat(),
        }))
        with pytest.raises(restore_lock.RestoreLocked):
            with restore_lock.exclusive(job):
                pass

    def test_a_corrupt_lock_is_treated_as_expired(self, lock_dir):
        """Half a lock file is the fingerprint of a process killed mid-write —
        exactly the holder that is never coming back to release it."""
        job = RestoreJob.objects.create()
        path = Path(lock_dir) / 'backups' / restore_lock.LOCK_FILENAME
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"job_id": 999, "expi')

        with restore_lock.exclusive(job):
            pass


@pytest.mark.django_db
class TestTheClusterLock:
    """On AWS the lock is the task's own existence, which needs no cleanup and
    survives a SIGKILL that no explicit release would."""

    def test_it_excludes_this_task_from_its_own_check(self, db, monkeypatch):
        """The failure this guards against stalls every restore, every time:
        list_tasks by family returns the asking task too, so a naive count can
        never reach zero and the restore waits for itself."""
        monkeypatch.setenv('ECS_CLUSTER', 'aitutor-dev-cluster')
        monkeypatch.setenv('ECS_RESTORE_FAMILY', 'aitutor-dev-migrate')
        mine = 'arn:aws:ecs:us-east-1:1:task/aitutor-dev-cluster/self'
        monkeypatch.setattr(restore_lock, 'own_task_arn', lambda: mine)
        monkeypatch.setattr(restore_lock, '_sibling_restore_tasks',
                            lambda cluster, family: [])

        job = RestoreJob.objects.create()
        with restore_lock.exclusive(job):
            pass

    def test_a_sibling_task_blocks(self, db, monkeypatch):
        monkeypatch.setenv('ECS_CLUSTER', 'aitutor-dev-cluster')
        monkeypatch.setenv('ECS_RESTORE_FAMILY', 'aitutor-dev-migrate')
        monkeypatch.setattr(
            restore_lock, '_sibling_restore_tasks',
            lambda cluster, family: [
                'arn:aws:ecs:us-east-1:1:task/aitutor-dev-cluster/other'],
        )
        job = RestoreJob.objects.create()
        with pytest.raises(restore_lock.RestoreLocked, match='already running'):
            with restore_lock.exclusive(job):
                pass

    def test_it_fails_closed_when_the_cluster_cannot_be_asked(self, db, monkeypatch):
        """Throttled, denied, or offline all mean 'do not know whether another
        restore is running', and that must not resolve to 'go ahead'."""
        monkeypatch.setenv('ECS_CLUSTER', 'aitutor-dev-cluster')
        monkeypatch.setenv('ECS_RESTORE_FAMILY', 'aitutor-dev-migrate')

        def boom(cluster, family):
            raise RuntimeError('ThrottlingException')

        monkeypatch.setattr(restore_lock, '_sibling_restore_tasks', boom)
        job = RestoreJob.objects.create()
        with pytest.raises(RuntimeError):
            with restore_lock.exclusive(job):
                pytest.fail('the restore must not proceed')

    def test_a_task_definition_revision_is_reduced_to_its_family(self, monkeypatch):
        """ListTasks matches nothing when handed family:revision, and silently —
        which would read as 'no other restore is running'."""
        monkeypatch.setenv('ECS_CLUSTER', 'c')
        monkeypatch.setenv('ECS_MIGRATE_TASK_DEFINITION',
                           'arn:aws:ecs:us-east-1:1:task-definition/aitutor-dev-migrate:42')
        assert restore_lock._ecs_settings() == ('c', 'aitutor-dev-migrate')

    def test_off_ecs_it_uses_the_file_backend(self, monkeypatch):
        for var in ('ECS_CLUSTER', 'ECS_RESTORE_FAMILY',
                    'ECS_MIGRATE_TASK_DEFINITION'):
            monkeypatch.delenv(var, raising=False)
        assert restore_lock._ecs_settings() is None


@pytest.mark.django_db
class TestTheRowSurvivesWhatItRecords:
    """A restore drops the table this row lives in. What it points at has to
    still be findable afterwards."""

    def test_the_safety_copy_is_named_by_key_not_by_relation(self, db):
        """The BackupJob row for the safety archive is dropped with everything
        else. The S3 object is not. A ForeignKey here would restore as NULL and
        lose the only pointer to the copy taken moments before."""
        field = RestoreJob._meta.get_field('safety_backup_key')
        from django.db import models as django_models
        assert isinstance(field, django_models.CharField)
        assert not field.is_relation

    def test_it_records_both_safety_nets_before_anything_is_destroyed(self, db):
        job = RestoreJob.objects.create(
            safety_backup_key='backups/pre-restore/7/aitutor-backup-db.tar.gz',
            rds_snapshot_id='aitutor-dev-db-pre-restore-7',
        )
        job.refresh_from_db()
        assert job.safety_backup_key and job.rds_snapshot_id

    def test_an_expired_source_archive_does_not_take_the_record_with_it(self, db):
        """The bucket expires archives at 90 days; the account of what was
        restored should outlive the file it was restored from."""
        backup = BackupJob.objects.create(status=BackupJob.Status.DONE,
                                          storage_key='backups/old.tar.gz')
        job = RestoreJob.objects.create(source_backup=backup,
                                        source_key='backups/old.tar.gz',
                                        status=RestoreJob.Status.DONE)
        backup.delete()

        job.refresh_from_db()
        assert job.source_backup is None
        assert job.source_key == 'backups/old.tar.gz'
        assert job.status == RestoreJob.Status.DONE

    def test_the_task_arn_is_kept_as_the_liveness_signal(self, db):
        """Wall-clock cannot tell a slow restore from a dead one, and the web
        process is scaled to zero for most of a real one."""
        job = RestoreJob.objects.create(
            task_arn='arn:aws:ecs:us-east-1:1:task/aitutor-dev-cluster/abc',
            status=RestoreJob.Status.RUNNING)
        job.refresh_from_db()
        assert job.task_arn.endswith('/abc')
        assert not job.is_finished


@pytest.mark.django_db(transaction=True)
class TestTheLockUnderAnActualRace:
    """Sequential tests cannot tell an O_EXCL create from a check-then-write.

    Both pass when nothing overlaps. The difference only shows when two holders
    arrive inside the same window, so the window has to be made real — a barrier
    to line them up, and a sleep inside the critical section so the first holder
    is still inside it when the second tries.
    """

    def test_two_racing_restores_produce_exactly_one_holder(self, lock_dir):
        import threading
        import time

        held, refused, broken = [], [], []
        barrier = threading.Barrier(4)
        jobs = [RestoreJob.objects.create(status=RestoreJob.Status.DONE)
                for _ in range(4)]

        def attempt(job):
            barrier.wait()
            try:
                with restore_lock.exclusive(job):
                    held.append(job.pk)
                    # Stay inside long enough that every rival overlaps with
                    # this holder rather than tidily following it.
                    time.sleep(0.05)
            except restore_lock.RestoreLocked:
                refused.append(job.pk)
            except Exception as exc:                   # noqa: BLE001
                broken.append(repr(exc))

        threads = [threading.Thread(target=attempt, args=(j,)) for j in jobs]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert broken == [], broken
        # Exactly one, not "at least one": two holders is the bug, and four
        # refusals would mean the lock never lets anybody through at all.
        assert len(held) == 1, f'{len(held)} restores held the lock at once'
        assert len(refused) == 3

    def test_the_lock_is_free_again_once_the_race_is_over(self, lock_dir):
        """A refused rival must not leave the lock in a state nobody can take —
        the failure that turns one bad restore into no restores ever again."""
        import threading

        barrier = threading.Barrier(2)

        def attempt(job):
            barrier.wait()
            try:
                with restore_lock.exclusive(job):
                    pass
            except restore_lock.RestoreLocked:
                pass

        jobs = [RestoreJob.objects.create(status=RestoreJob.Status.DONE)
                for _ in range(2)]
        threads = [threading.Thread(target=attempt, args=(j,)) for j in jobs]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        later = RestoreJob.objects.create()
        with restore_lock.exclusive(later):
            pass


# ---------------------------------------------------------------------------
# Preflight — the checks that run before anything is destroyed
# ---------------------------------------------------------------------------

def _manifest(**over):
    """A manifest of the shape build() writes today."""
    from ai_tutor.apps.dashboard import restore
    base = {
        'archive_format_version': 2,
        'scope': 'platform',
        'created_at': '2026-09-12T07:01:00+00:00',
        'created_by': 'admin',
        'database': {
            'engine': 'sqlite',
            'dump_format': 'sqlite-file',
            'dump_file': 'db.sqlite3',
            'dump_sha256': 'a' * 64,
            'row_counts': {'auth_user': 389, 'tutoring_sessionturn': 36109},
            'migration_heads': dict(
                (app, max(names)) for app, names in restore.code_migrations().items()
            ),
        },
        'media': {'store': 'local', 'files': 3, 'bytes': 10, 'included': True,
                  'files_archived': 3},
        'archive_sha256': 'b' * 64,
    }
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            base[key] = {**base[key], **value}
        else:
            base[key] = value
    return base


@pytest.mark.django_db
class TestPreflightRefusesWhatCannotWork:
    """These are refusals, not warnings, because there is no recovery once the
    live database has been dropped for an archive that was never restorable."""

    def _report(self, manifest, lock_dir):
        from ai_tutor.apps.dashboard import restore
        backup = BackupJob.objects.create(status=BackupJob.Status.DONE,
                                          summary=manifest,
                                          storage_key='backups/x.tar.gz')
        return restore.preflight(backup=backup)

    def test_a_good_archive_passes(self, lock_dir):
        report = self._report(_manifest(), lock_dir)
        assert report['ok'], report['blocking']
        assert report['manifest_source'] == 'backup record'

    def test_a_postgres_dump_is_refused_on_sqlite(self, lock_dir):
        """No conversion path exists, and a half-applied attempt leaves the
        database in a state neither engine understands."""
        report = self._report(
            _manifest(database={'dump_format': 'pg_dump-custom'}), lock_dir)
        assert not report['ok']
        assert any('cannot be restored into the other' in b
                   for b in report['blocking']), report['blocking']

    def test_an_archive_from_a_newer_platform_is_refused(self, lock_dir):
        """The dump carries tables this code has no migration to produce and
        none to remove. There is no migrating backwards."""
        heads = _manifest()['database']['migration_heads']
        report = self._report(
            _manifest(database={'migration_heads': {
                **heads, 'dashboard': '0099_from_the_future'}}), lock_dir)
        assert not report['ok']
        assert any('NEWER version' in b for b in report['blocking'])

    def test_an_app_this_code_does_not_have_is_refused(self, lock_dir):
        heads = _manifest()['database']['migration_heads']
        report = self._report(
            _manifest(database={'migration_heads': {
                **heads, 'timetabling': '0001_initial'}}), lock_dir)
        assert not report['ok']
        assert any('timetabling' in b for b in report['blocking'])

    def test_something_that_is_not_a_platform_backup_is_refused(self, lock_dir):
        report = self._report(_manifest(scope='one-course-export'), lock_dir)
        assert not report['ok']
        assert any('not a whole platform' in b for b in report['blocking'])

    def test_an_unrecognised_dump_format_is_refused(self, lock_dir):
        report = self._report(_manifest(database={'dump_format': 'mysqldump'}),
                              lock_dir)
        assert not report['ok']


@pytest.mark.django_db
class TestPreflightWarnsWithoutRefusing:
    """Recoverable, so the superadmin decides — but they have to be told."""

    def _report(self, manifest):
        from ai_tutor.apps.dashboard import restore
        backup = BackupJob.objects.create(status=BackupJob.Status.DONE,
                                          summary=manifest,
                                          storage_key='backups/x.tar.gz')
        return restore.preflight(backup=backup)

    def _check(self, report, name):
        return next(c for c in report['checks'] if c['name'] == name)

    def test_an_older_archive_is_allowed_and_says_migrations_will_run(self, lock_dir):
        heads = dict(_manifest()['database']['migration_heads'])
        heads['dashboard'] = '0023_add_backup_job'
        report = self._report(_manifest(database={'migration_heads': heads}))
        assert report['ok']
        assert self._check(report, 'Schema version')['status'] == 'warn'

    def test_a_database_only_archive_warns_about_broken_figures(self, lock_dir):
        report = self._report(_manifest(media={'included': False}))
        assert report['ok']
        assert self._check(report, 'Uploaded files')['status'] == 'warn'
        assert not report['include_media']

    def test_a_missing_checksum_is_unknown_and_never_pass(self, lock_dir):
        """The whole point of the distinction. An archive that predates
        checksums has not been verified, and reporting it as verified is how
        someone comes to trust a truncated file."""
        m = _manifest(archive_format_version=1)
        del m['database']['dump_sha256']
        report = self._report(m)
        assert report['ok']
        assert self._check(report, 'Integrity')['status'] == 'unknown'

    def test_a_missing_schema_record_is_unknown_not_a_pass(self, lock_dir):
        m = _manifest()
        del m['database']['migration_heads']
        report = self._report(m)
        assert self._check(report, 'Schema version')['status'] == 'unknown'


@pytest.mark.django_db
class TestPreflightShowsWhatWouldChange:

    def test_the_row_diff_puts_the_biggest_loss_first(self, lock_dir, django_user_model):
        """The question being answered is 'what am I about to destroy', so the
        largest losses have to be the ones read first."""
        from ai_tutor.apps.dashboard import restore
        django_user_model.objects.create_user('someone', 'a@b.c', 'pw')

        backup = BackupJob.objects.create(
            status=BackupJob.Status.DONE, storage_key='backups/x.tar.gz',
            summary=_manifest(database={'row_counts': {
                'auth_user': 0, 'tutoring_sessionturn': 5}}))
        report = restore.preflight(backup=backup)

        diff = {r['table']: r for r in report['row_diff']}
        assert diff['auth_user']['archive'] == 0
        assert diff['auth_user']['live'] >= 1
        assert diff['auth_user']['delta'] < 0
        losses = [r['delta'] for r in report['row_diff'] if r['delta'] is not None]
        assert losses == sorted(losses)

    def test_it_reads_the_manifest_without_touching_the_archive(self, lock_dir):
        """A 9.3 GB archive still listed on the settings page costs a database
        read and no S3 bytes at all."""
        from ai_tutor.apps.dashboard import restore
        backup = BackupJob.objects.create(status=BackupJob.Status.DONE,
                                          summary=_manifest(),
                                          storage_key='backups/absent.tar.gz')
        report = restore.preflight(backup=backup)
        assert report['manifest_source'] == 'backup record'
        assert report['ok']

    def test_it_falls_back_to_the_sidecar_when_there_is_no_row(self, lock_dir):
        """The uploaded archive, and the pre-restore safety copy whose row the
        restore destroys."""
        from ai_tutor.apps.dashboard import restore
        root = Path(lock_dir) / 'backups'
        root.mkdir(parents=True, exist_ok=True)
        archive = root / 'orphan.tar.gz'
        archive.write_bytes(b'not read by preflight')
        archive.with_name(archive.name + '.manifest.json').write_text(
            json.dumps(_manifest()))

        report = restore.preflight(key=str(archive))
        assert report['manifest_source'] == 'sidecar manifest'
        assert report['ok']

    def test_a_file_that_describes_nothing_is_rejected_outright(self, lock_dir):
        from ai_tutor.apps.dashboard import restore
        root = Path(lock_dir) / 'backups'
        root.mkdir(parents=True, exist_ok=True)
        junk = root / 'holiday-photos.tar.gz'
        junk.write_bytes(b'definitely not a backup')

        with pytest.raises(restore.PreflightFailed, match='does not describe itself'):
            restore.preflight(key=str(junk))


class TestArchiveMembersAreNotTrusted:
    """An uploaded archive's member names are supplied by whoever made it."""

    def _member(self, name, **kw):
        import tarfile
        m = tarfile.TarInfo(name)
        m.type = kw.get('type', tarfile.REGTYPE)
        m.size = kw.get('size', 10)
        return m

    def test_ordinary_media_maps_to_its_key(self):
        from ai_tutor.apps.dashboard import restore
        assert restore.media_key_for(
            self._member('media/figures/volcano.png'), 'db.dump'
        ) == 'figures/volcano.png'

    def test_bookkeeping_members_are_not_media(self):
        from ai_tutor.apps.dashboard import restore
        for name in ('db.dump', 'manifest.json', 'manifest-final.json'):
            assert restore.media_key_for(self._member(name), 'db.dump') is None

    @pytest.mark.parametrize('name', [
        'media/../../etc/passwd',
        'media//etc/passwd',
        'media/a/../../../outside',
        'media/',
    ])
    def test_a_member_that_escapes_the_media_store_aborts(self, name):
        from ai_tutor.apps.dashboard import restore
        with pytest.raises(restore.PreflightFailed):
            restore.media_key_for(self._member(name), 'db.dump')

    def test_a_symlink_is_never_restored(self):
        import tarfile
        from ai_tutor.apps.dashboard import restore
        with pytest.raises(restore.PreflightFailed, match='not a regular file'):
            restore.media_key_for(
                self._member('media/evil', type=tarfile.SYMTYPE), 'db.dump')

    def test_an_unexpected_member_aborts_rather_than_being_ignored(self):
        """Silently skipping it would mean restoring an archive that is not the
        archive that was approved."""
        from ai_tutor.apps.dashboard import restore
        with pytest.raises(restore.PreflightFailed, match='Unexpected archive member'):
            restore.media_key_for(self._member('payload.sh'), 'db.dump')


@pytest.mark.django_db
class TestProgressSurvivesTheDatabase:

    def test_status_is_written_and_read_back_without_the_row(self, lock_dir):
        from ai_tutor.apps.dashboard import restore
        job = RestoreJob.objects.create()
        restore.write_status(job, state='running', stage='pg_restore', progress=60,
                             safety_backup_key='backups/pre-restore/1/x.tar.gz')

        held = restore.read_status(job)
        assert held['stage'] == 'pg_restore'
        assert held['safety_backup_key'].startswith('backups/pre-restore/')

    def test_writing_status_never_breaks_the_restore(self, lock_dir, monkeypatch):
        """A restore that died because it could not describe itself would be the
        worst possible trade."""
        from ai_tutor.apps.dashboard import restore

        def unwritable():
            raise OSError('no space left on device')

        monkeypatch.setattr(restore.backup_service, 'backup_root', unwritable)
        job = RestoreJob.objects.create()
        restore.write_status(job, state='running')      # must not raise
        assert restore.read_status(job) is None


@pytest.mark.django_db
class TestTheReaperUsesTheClusterNotTheClock:

    def test_a_job_whose_task_has_stopped_is_failed(self, lock_dir, monkeypatch):
        from ai_tutor.apps.dashboard import restore
        monkeypatch.setattr(restore, '_task_is_alive', lambda arn: False)
        job = RestoreJob.objects.create(status=RestoreJob.Status.RUNNING,
                                        task_arn='arn:aws:ecs:::task/c/gone')
        assert restore.reap_stale() == 1
        job.refresh_from_db()
        assert job.status == RestoreJob.Status.FAILED
        assert 'safety copy' in job.error

    def test_a_slow_but_live_restore_is_left_alone(self, lock_dir, monkeypatch):
        """Killing a running restore's row invites someone to start a second one
        on top of a database the first is still writing to."""
        from ai_tutor.apps.dashboard import restore
        monkeypatch.setattr(restore, '_task_is_alive', lambda arn: True)
        job = RestoreJob.objects.create(status=RestoreJob.Status.RUNNING,
                                        task_arn='arn:aws:ecs:::task/c/busy')
        RestoreJob.objects.filter(pk=job.pk).update(
            created_at=timezone.now() - timedelta(days=2))
        assert restore.reap_stale() == 0
        job.refresh_from_db()
        assert job.status == RestoreJob.Status.RUNNING

    def test_a_recent_job_is_not_reaped_when_the_cluster_cannot_be_asked(
            self, lock_dir, monkeypatch):
        from ai_tutor.apps.dashboard import restore
        monkeypatch.setattr(restore, '_task_is_alive', lambda arn: None)
        RestoreJob.objects.create(status=RestoreJob.Status.RUNNING, task_arn='pid:1')
        assert restore.reap_stale() == 0

    def test_an_ancient_job_is_reaped_when_the_cluster_cannot_be_asked(
            self, lock_dir, monkeypatch):
        from ai_tutor.apps.dashboard import restore
        monkeypatch.setattr(restore, '_task_is_alive', lambda arn: None)
        job = RestoreJob.objects.create(status=RestoreJob.Status.RUNNING,
                                        task_arn='pid:1')
        RestoreJob.objects.filter(pk=job.pk).update(
            created_at=timezone.now() - restore.STALE_AFTER - timedelta(hours=1))
        assert restore.reap_stale() == 1


@pytest.mark.django_db
class TestDispatch:

    def test_it_passes_only_the_job_id(self, lock_dir, monkeypatch):
        """Everything the restore acts on is read from the row, so a tampered
        container override cannot point it at a different archive."""
        from ai_tutor.apps.dashboard import restore
        seen = {}

        def fake_run_task(job, cluster, td, subnets, sgs, container):
            seen['argv'] = ['python', 'manage.py', 'restore_backup',
                            '--job', str(job.pk)]
            return 'arn:aws:ecs:::task/c/new'

        monkeypatch.setenv('ECS_CLUSTER', 'c')
        monkeypatch.setenv('ECS_MIGRATE_TASK_DEFINITION', 'aitutor-dev-migrate')
        monkeypatch.setenv('ECS_SUBNETS', 'subnet-1,subnet-2')
        monkeypatch.setattr(restore, '_dispatch_via_ecs', fake_run_task)

        job = RestoreJob.objects.create()
        assert restore.dispatch(job).endswith('/new')
        assert seen['argv'] == ['python', 'manage.py', 'restore_backup',
                                '--job', str(job.pk)]

    def test_it_defaults_to_the_migrate_task_definition(self, monkeypatch):
        """A family Pulumi creates but CI never re-registers would be frozen at
        whatever image the last `pulumi up` pinned — and this task runs
        migrations."""
        from ai_tutor.apps.dashboard import restore
        monkeypatch.setenv('ECS_CLUSTER', 'c')
        monkeypatch.setenv('ECS_SUBNETS', 'subnet-1')
        monkeypatch.delenv('ECS_RESTORE_TASK_DEFINITION', raising=False)
        monkeypatch.setenv('ECS_MIGRATE_TASK_DEFINITION', 'aitutor-dev-migrate')
        assert restore._ecs_settings()[1] == 'aitutor-dev-migrate'

    def test_off_ecs_it_falls_back_to_a_subprocess(self, monkeypatch):
        from ai_tutor.apps.dashboard import restore
        for var in ('ECS_CLUSTER', 'ECS_SUBNETS', 'ECS_RESTORE_TASK_DEFINITION',
                    'ECS_MIGRATE_TASK_DEFINITION'):
            monkeypatch.delenv(var, raising=False)
        assert restore._ecs_settings() is None
