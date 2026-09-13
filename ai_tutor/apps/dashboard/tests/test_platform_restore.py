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
        # Required: dispatch refuses a partly configured cluster rather than
        # falling back into the web container. See TestAPartlyConfiguredEcsRefuses.
        monkeypatch.setenv('ECS_SERVICE', 'aitutor-dev-service')
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


# ---------------------------------------------------------------------------
# The destructive command
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
class TestTheRoundTripActuallyWorks:
    """The claim the whole feature rests on: what went in comes back out.

    Everything else here tests a safeguard. This tests that the thing works —
    that an archive taken before a change, restored after it, removes the
    change and brings back what was there.
    """

    def _archive(self, tmp_path, settings):
        settings.BACKUP_ROOT = tmp_path / 'backups'
        settings.AWS_BACKUP_BUCKET = ''
        job = BackupJob.objects.create(include_media=False)
        from ai_tutor.apps.dashboard import backup as backup_service
        backup_service.build(job)
        job.refresh_from_db()
        assert job.status == BackupJob.Status.DONE, job.error
        return job

    def test_a_change_made_after_the_backup_is_gone_after_the_restore(
            self, db, tmp_path, settings, monkeypatch, django_user_model):
        from django.core.management import call_command

        marker = 'gone-by-restore'
        assert not django_user_model.objects.filter(username=marker).exists()
        backup = self._archive(tmp_path, settings)

        # The world moves on.
        django_user_model.objects.create_user(marker, 'x@y.z', 'pw')
        assert django_user_model.objects.filter(username=marker).exists()

        job = RestoreJob.objects.create(source_backup=backup,
                                        source_key=backup.storage_key)
        # No ECS here: no service to scale, no cluster to wait for.
        for var in ('ECS_CLUSTER', 'ECS_SERVICE'):
            monkeypatch.delenv(var, raising=False)
        call_command('restore_backup', job=job.pk)

        assert not django_user_model.objects.filter(username=marker).exists(), \
            'the restore did not undo the change'

    def test_a_row_that_existed_before_the_backup_survives(
            self, db, tmp_path, settings, monkeypatch, django_user_model):
        """The other half. A restore that merely emptied the database would
        pass the test above."""
        from django.core.management import call_command

        keeper = 'present-before-the-backup'
        django_user_model.objects.create_user(keeper, 'k@y.z', 'pw')
        backup = self._archive(tmp_path, settings)

        django_user_model.objects.create_user('later', 'l@y.z', 'pw')
        job = RestoreJob.objects.create(source_backup=backup,
                                        source_key=backup.storage_key)
        for var in ('ECS_CLUSTER', 'ECS_SERVICE'):
            monkeypatch.delenv(var, raising=False)
        call_command('restore_backup', job=job.pk)

        assert django_user_model.objects.filter(username=keeper).exists()
        assert not django_user_model.objects.filter(username='later').exists()

    def test_the_restore_records_itself_in_the_database_it_restored(
            self, db, tmp_path, settings, monkeypatch):
        """The row that started it was dropped with everything else. Without a
        re-insert, a successful restore leaves no trace it happened."""
        from django.core.management import call_command

        backup = self._archive(tmp_path, settings)
        job = RestoreJob.objects.create(source_backup=backup,
                                        source_key=backup.storage_key)
        for var in ('ECS_CLUSTER', 'ECS_SERVICE'):
            monkeypatch.delenv(var, raising=False)
        call_command('restore_backup', job=job.pk)

        done = RestoreJob.objects.filter(status=RestoreJob.Status.DONE)
        assert done.exists(), 'no record of the restore survived it'
        assert done.first().safety_backup_key, 'the safety copy was not recorded'

    def test_the_safety_copy_is_downloadable_afterwards(
            self, db, tmp_path, settings, monkeypatch):
        """An S3 key nobody can reach from the UI is not a safety net."""
        from django.core.management import call_command

        backup = self._archive(tmp_path, settings)
        job = RestoreJob.objects.create(source_backup=backup,
                                        source_key=backup.storage_key)
        for var in ('ECS_CLUSTER', 'ECS_SERVICE'):
            monkeypatch.delenv(var, raising=False)
        call_command('restore_backup', job=job.pk)

        safety = BackupJob.objects.filter(stage='pre-restore safety copy').first()
        assert safety is not None, 'the safety copy is not in the backup list'
        assert safety.status == BackupJob.Status.DONE
        assert Path(safety.storage_key).is_file()

    def test_interrupted_rows_the_archive_brought_back_are_cleared(
            self, db, tmp_path, settings, monkeypatch):
        """build() marks a BackupJob RUNNING before it dumps, so every archive
        contains its own unfinished row. Restored as-is it holds the
        one-active-backup constraint shut and parks the settings page on 'a
        backup is already running' for six hours."""
        from django.core.management import call_command

        backup = self._archive(tmp_path, settings)
        # The archive contains this very job, and build() saved it as RUNNING
        # before dumping — so the dump holds a running row.
        job = RestoreJob.objects.create(source_backup=backup,
                                        source_key=backup.storage_key)
        for var in ('ECS_CLUSTER', 'ECS_SERVICE'):
            monkeypatch.delenv(var, raising=False)
        call_command('restore_backup', job=job.pk)

        unfinished = BackupJob.objects.filter(
            status__in=(BackupJob.Status.PENDING, BackupJob.Status.RUNNING))
        assert not unfinished.exists(), \
            'a resurrected running backup would block the next one for 6 hours'
        # And a new backup can therefore be started.
        BackupJob.objects.create()


@pytest.mark.django_db(transaction=True)
class TestItRefusesRatherThanRiskIt:

    def _backup(self, tmp_path, settings):
        settings.BACKUP_ROOT = tmp_path / 'backups'
        settings.AWS_BACKUP_BUCKET = ''
        from ai_tutor.apps.dashboard import backup as backup_service
        job = BackupJob.objects.create(include_media=False)
        backup_service.build(job)
        job.refresh_from_db()
        return job

    def test_a_failed_safety_backup_stops_everything(
            self, db, tmp_path, settings, monkeypatch, django_user_model):
        """build() never raises — it records failure and returns — so the status
        has to be read back. Without that check this is the step that turns
        'we restored the wrong archive' from recoverable into permanent."""
        from django.core.management import call_command
        from django.core.management.base import CommandError
        from ai_tutor.apps.dashboard import backup as backup_service

        backup = self._backup(tmp_path, settings)
        canary = django_user_model.objects.create_user('canary', 'c@y.z', 'pw')

        def fail_the_safety_backup(safety_job, **kwargs):
            safety_job.status = BackupJob.Status.FAILED
            safety_job.error = 'pg_dump: connection refused'
            safety_job.save()

        monkeypatch.setattr(backup_service, 'build', fail_the_safety_backup)
        for var in ('ECS_CLUSTER', 'ECS_SERVICE'):
            monkeypatch.delenv(var, raising=False)

        job = RestoreJob.objects.create(source_backup=backup,
                                        source_key=backup.storage_key)
        with pytest.raises(CommandError, match='NOTHING HAS BEEN CHANGED'):
            call_command('restore_backup', job=job.pk)

        # The live database is untouched.
        assert django_user_model.objects.filter(pk=canary.pk).exists()

    def test_a_corrupted_archive_is_caught_before_the_database_is_dropped(
            self, db, tmp_path, settings, monkeypatch, django_user_model):
        """The entire reason checksums were added in the first place."""
        from django.core.management import call_command
        from django.core.management.base import CommandError

        backup = self._backup(tmp_path, settings)
        canary = django_user_model.objects.create_user('canary2', 'c2@y.z', 'pw')

        # Corrupt the recorded checksum so the archive no longer matches it.
        backup.summary['database']['dump_sha256'] = 'f' * 64
        backup.save(update_fields=['summary'])

        for var in ('ECS_CLUSTER', 'ECS_SERVICE'):
            monkeypatch.delenv(var, raising=False)
        job = RestoreJob.objects.create(source_backup=backup,
                                        source_key=backup.storage_key)
        with pytest.raises(CommandError, match='NOTHING HAS BEEN CHANGED'):
            call_command('restore_backup', job=job.pk)

        assert django_user_model.objects.filter(pk=canary.pk).exists()

    def test_it_will_not_run_while_another_restore_holds_the_lock(
            self, db, tmp_path, settings, monkeypatch):
        from django.core.management import call_command
        from django.core.management.base import CommandError

        backup = self._backup(tmp_path, settings)
        for var in ('ECS_CLUSTER', 'ECS_SERVICE'):
            monkeypatch.delenv(var, raising=False)

        blocker = RestoreJob.objects.create(status=RestoreJob.Status.DONE)
        job = RestoreJob.objects.create(source_backup=backup,
                                        source_key=backup.storage_key)
        with restore_lock.exclusive(blocker):
            with pytest.raises(CommandError):
                call_command('restore_backup', job=job.pk)

        job.refresh_from_db()
        assert job.status == RestoreJob.Status.FAILED
        assert 'Refused' in job.error


# ---------------------------------------------------------------------------
# The views — who may reach this, and what stops a mis-click
# ---------------------------------------------------------------------------

@pytest.fixture
def superuser(db):
    return User.objects.create_superuser('root2', 'root2@example.com', 'pw')


@pytest.fixture
def staff_only(db):
    """is_staff but NOT is_superuser — the account a superadmin can create from
    the staff list with one toggle."""
    return User.objects.create_user('staffer', 's@example.com', 'pw', is_staff=True)


@pytest.mark.django_db
class TestWhoCanReachRestore:

    @pytest.mark.parametrize('route', [
        'restore_preflight', 'restore_start', 'restore_upload'])
    def test_an_anonymous_visitor_is_not_told_it_exists(self, client, route):
        from django.urls import reverse
        response = client.post(reverse(f'dashboard:{route}'))
        assert response.status_code in (302, 404)
        assert 'Restore' not in response.content.decode(errors='ignore')

    @pytest.mark.parametrize('route', [
        'restore_preflight', 'restore_start', 'restore_upload'])
    def test_staff_without_superuser_gets_nothing(self, client, staff_only, route):
        """is_staff is one toggle away for any account, and gates taking a copy.
        Destroying everything is a different privilege."""
        from django.urls import reverse
        client.force_login(staff_only)
        assert client.post(reverse(f'dashboard:{route}')).status_code == 404

    def test_a_superuser_reaches_the_confirmation_page(self, client, superuser,
                                                       lock_dir):
        from django.urls import reverse
        backup = BackupJob.objects.create(status=BackupJob.Status.DONE,
                                          summary=_manifest(),
                                          storage_key='backups/x.tar.gz')
        client.force_login(superuser)
        response = client.post(reverse('dashboard:restore_preflight'),
                               {'backup_id': backup.pk})
        assert response.status_code == 200
        body = response.content.decode()
        assert 'replaces the whole platform' in body
        assert 'to confirm' in body


@pytest.mark.django_db
class TestTheConfirmationActuallyGuards:

    def _confirm_page(self, client, superuser, lock_dir):
        from django.urls import reverse
        backup = BackupJob.objects.create(status=BackupJob.Status.DONE,
                                          summary=_manifest(),
                                          storage_key='backups/x.tar.gz')
        client.force_login(superuser)
        response = client.post(reverse('dashboard:restore_preflight'),
                               {'backup_id': backup.pk})
        return response.context['token'], backup

    def test_the_wrong_platform_name_starts_nothing(self, client, superuser,
                                                    lock_dir):
        from django.urls import reverse
        token, _ = self._confirm_page(client, superuser, lock_dir)
        client.post(reverse('dashboard:restore_start'),
                    {'token': token, 'confirm_name': 'something else'})
        assert not RestoreJob.objects.exists()

    def test_a_token_cannot_be_used_twice(self, client, superuser, lock_dir,
                                          monkeypatch):
        """Spent server-side, so a re-POST of the confirmation page — a refresh,
        a back button, a forged repeat — cannot start a second restore."""
        from django.urls import reverse
        from ai_tutor.apps.dashboard import restore as restore_service
        monkeypatch.setattr(restore_service, 'dispatch', lambda job: 'pid:0')

        token, _ = self._confirm_page(client, superuser, lock_dir)
        name = 'AI Tutor'
        from ai_tutor.apps.accounts.models import PlatformConfig
        name = PlatformConfig.load().platform_name or name

        client.post(reverse('dashboard:restore_start'),
                    {'token': token, 'confirm_name': name})
        assert RestoreJob.objects.count() == 1

        client.post(reverse('dashboard:restore_start'),
                    {'token': token, 'confirm_name': name})
        assert RestoreJob.objects.count() == 1, 'the token was accepted twice'

    def test_a_token_that_was_never_issued_is_refused(self, client, superuser,
                                                      lock_dir):
        from django.urls import reverse
        client.force_login(superuser)
        client.post(reverse('dashboard:restore_start'),
                    {'token': 'deadbeef' * 4, 'confirm_name': 'AI Tutor'})
        assert not RestoreJob.objects.exists()

    def test_an_unrestorable_archive_offers_no_confirmation_at_all(
            self, client, superuser, lock_dir):
        """Not merely warned about — there must be no form to submit."""
        from django.urls import reverse
        backup = BackupJob.objects.create(
            status=BackupJob.Status.DONE, storage_key='backups/x.tar.gz',
            summary=_manifest(database={'dump_format': 'pg_dump-custom'}))
        client.force_login(superuser)
        response = client.post(reverse('dashboard:restore_preflight'),
                               {'backup_id': backup.pk})
        body = response.content.decode()
        assert 'cannot be restored' in body
        assert 'restore/start' not in body

    def test_it_is_all_on_the_record(self, client, superuser, lock_dir):
        from django.urls import reverse
        from ai_tutor.apps.safety.models import SafetyAuditLog
        token, _ = self._confirm_page(client, superuser, lock_dir)
        actions = list(SafetyAuditLog.objects
                       .values_list('details__action', flat=True))
        assert 'restore_preflight' in actions
        entry = SafetyAuditLog.objects.first()
        assert entry.severity == 'critical', 'a restore is not a warning'


@pytest.mark.django_db
class TestAFailedRestoreCanBeDiagnosed:
    """A restore that half-works must leave an explanation somewhere.

    The row that would record the failure is in the database the restore was
    busy replacing, so off ECS — where there is no CloudWatch collecting the
    task's stdout — the log file is the only account there is. This was learned
    by sending it to DEVNULL and then having to reconstruct what happened.
    """

    def test_the_subprocess_backend_keeps_its_output(self, lock_dir, monkeypatch):
        from ai_tutor.apps.dashboard import restore as restore_service

        started = {}

        class FakePopen:
            def __init__(self, argv, stdout=None, stderr=None, **kw):
                started['stdout'] = stdout
                started['stderr'] = stderr
                self.pid = 4242

        monkeypatch.setattr(restore_service.subprocess, 'Popen', FakePopen)
        job = RestoreJob.objects.create()
        restore_service._dispatch_via_subprocess(job)

        assert started['stdout'] is not None, 'restore output was discarded'
        assert started['stdout'] != restore_service.subprocess.DEVNULL
        # And it is a real file on disk, not a pipe nobody is reading.
        assert hasattr(started['stdout'], 'name')
        assert f'restore-{job.pk}.log' in str(started['stdout'].name)
        assert started['stderr'] == restore_service.subprocess.STDOUT

    def test_the_log_lands_beside_the_archives(self, lock_dir, monkeypatch):
        from ai_tutor.apps.dashboard import restore as restore_service
        from ai_tutor.apps.dashboard import backup as backup_service

        class FakePopen:
            def __init__(self, *a, **kw):
                self.pid = 1

        monkeypatch.setattr(restore_service.subprocess, 'Popen', FakePopen)
        job = RestoreJob.objects.create()
        restore_service._dispatch_via_subprocess(job)
        assert (backup_service.backup_root() / f'restore-{job.pk}.log').is_file()


@pytest.mark.django_db
class TestAPartlyConfiguredEcsRefuses:
    """The subprocess fallback is safe off ECS and catastrophic on it.

    In-container means no scale-to-zero, no autoscaling suspension and no idle
    wait — the restore would drop the production database while the other web
    tasks are still serving from it. This is the exact environment the AWS stack
    was in before the infrastructure change landed: ECS_CLUSTER and ECS_SUBNETS
    set, everything else absent.
    """

    def _partly(self, monkeypatch):
        monkeypatch.setenv('ECS_CLUSTER', 'aitutor-dev-cluster')
        monkeypatch.setenv('ECS_SUBNETS', 'subnet-1,subnet-2')
        for var in ('ECS_SERVICE', 'ECS_RESTORE_TASK_DEFINITION',
                    'ECS_MIGRATE_TASK_DEFINITION'):
            monkeypatch.delenv(var, raising=False)

    def test_dispatch_refuses_instead_of_running_in_the_web_container(
            self, lock_dir, monkeypatch):
        from ai_tutor.apps.dashboard import restore as restore_service
        self._partly(monkeypatch)

        def must_not_run(job):
            pytest.fail('fell back to a subprocess on ECS')

        monkeypatch.setattr(restore_service, '_dispatch_via_subprocess', must_not_run)
        job = RestoreJob.objects.create()
        with pytest.raises(RuntimeError, match='not configured'):
            restore_service.dispatch(job)

    def test_it_names_what_is_missing(self, lock_dir, monkeypatch):
        from ai_tutor.apps.dashboard import restore as restore_service
        self._partly(monkeypatch)
        missing = restore_service.required_settings_missing()
        assert 'ECS_SERVICE' in missing
        assert 'ECS_MIGRATE_TASK_DEFINITION' in missing

    def test_the_command_refuses_too(self, db, tmp_path, settings, monkeypatch):
        """Reachable directly — a hand-run task, a retry, someone on a bastion."""
        from django.core.management import call_command
        from django.core.management.base import CommandError
        from ai_tutor.apps.dashboard import backup as backup_service

        settings.BACKUP_ROOT = tmp_path / 'backups'
        settings.AWS_BACKUP_BUCKET = ''
        backup = BackupJob.objects.create(status=BackupJob.Status.DONE,
                                          summary=_manifest(),
                                          storage_key='backups/x.tar.gz')
        job = RestoreJob.objects.create(source_backup=backup,
                                        source_key=backup.storage_key)
        self._partly(monkeypatch)

        def must_not_run(*a, **kw):
            pytest.fail('a safety backup was started on a refused restore')

        monkeypatch.setattr(backup_service, 'build', must_not_run)
        with pytest.raises(CommandError, match='NOTHING HAS BEEN CHANGED'):
            call_command('restore_backup', job=job.pk)

    def test_a_fully_configured_ecs_is_allowed(self, lock_dir, monkeypatch):
        from ai_tutor.apps.dashboard import restore as restore_service
        self._partly(monkeypatch)
        monkeypatch.setenv('ECS_SERVICE', 'aitutor-dev-service')
        monkeypatch.setenv('ECS_MIGRATE_TASK_DEFINITION', 'aitutor-dev-migrate')
        assert restore_service.required_settings_missing() == []

    def test_off_ecs_the_subprocess_fallback_is_still_fine(self, lock_dir,
                                                           monkeypatch):
        """Dev and single-server installs have no cluster to be confused about."""
        from ai_tutor.apps.dashboard import restore as restore_service
        for var in ('ECS_CLUSTER', 'ECS_SUBNETS', 'ECS_SERVICE',
                    'ECS_RESTORE_TASK_DEFINITION', 'ECS_MIGRATE_TASK_DEFINITION'):
            monkeypatch.delenv(var, raising=False)
        assert restore_service.required_settings_missing() == []


@pytest.mark.django_db(transaction=True)
class TestTheSourceArchiveIsNotReportedAsFailed:
    """Every dump contains the row describing the backup that was running when
    it was taken — build() saves RUNNING before it dumps. That backup plainly
    finished, because the archive it produced is what we just restored from.
    Sweeping it to 'failed' with the rest puts a red Failed row in the list for
    the exact archive the admin succeeded with."""

    def test_the_archive_own_record_comes_back_as_done(
            self, db, tmp_path, settings, monkeypatch):
        from django.core.management import call_command
        from ai_tutor.apps.dashboard import backup as backup_service

        settings.BACKUP_ROOT = tmp_path / 'backups'
        settings.AWS_BACKUP_BUCKET = ''
        source = BackupJob.objects.create(include_media=False)
        backup_service.build(source)
        source.refresh_from_db()
        assert source.status == BackupJob.Status.DONE, source.error

        job = RestoreJob.objects.create(source_backup=source,
                                        source_key=source.storage_key)
        for var in ('ECS_CLUSTER', 'ECS_SERVICE'):
            monkeypatch.delenv(var, raising=False)
        call_command('restore_backup', job=job.pk)

        source.refresh_from_db()
        assert source.status == BackupJob.Status.DONE, \
            'the archive we restored from is listed as failed'
        assert source.storage_key, 'its key was lost, so it cannot be downloaded'
        # The dump holds this row as it was MID-backup, before build() wrote
        # the size and manifest onto it — so both have to be put back, or the
        # archive shows "—" in the list and cannot be preflighted again.
        assert source.size_bytes > 0, 'shows "—" in the list'
        assert (source.summary or {}).get('database'), 'cannot be restored again'

    def test_other_unfinished_rows_are_still_swept(
            self, db, tmp_path, settings, monkeypatch):
        """The sweep still has to happen — a resurrected running row holds the
        one-active-backup constraint shut for six hours."""
        from django.core.management import call_command
        from ai_tutor.apps.dashboard import backup as backup_service

        settings.BACKUP_ROOT = tmp_path / 'backups'
        settings.AWS_BACKUP_BUCKET = ''
        source = BackupJob.objects.create(include_media=False)
        backup_service.build(source)
        source.refresh_from_db()

        job = RestoreJob.objects.create(source_backup=source,
                                        source_key=source.storage_key)
        for var in ('ECS_CLUSTER', 'ECS_SERVICE'):
            monkeypatch.delenv(var, raising=False)
        call_command('restore_backup', job=job.pk)

        assert not BackupJob.objects.filter(
            status__in=(BackupJob.Status.PENDING,
                        BackupJob.Status.RUNNING)).exists()
        # And a new backup can start, which is the point of the sweep.
        BackupJob.objects.create()


@pytest.mark.django_db(transaction=True)
class TestTheSafetyCopyCanItselfBeRestored:
    """It is the archive somebody reaches for when a restore turned out to be
    the wrong one. A record without a manifest cannot be preflighted, which
    would make it the one archive they cannot put back."""

    def test_it_carries_a_real_manifest_not_a_stub(
            self, db, tmp_path, settings, monkeypatch):
        from django.core.management import call_command
        from ai_tutor.apps.dashboard import backup as backup_service
        from ai_tutor.apps.dashboard import restore as restore_service

        settings.BACKUP_ROOT = tmp_path / 'backups'
        settings.AWS_BACKUP_BUCKET = ''
        source = BackupJob.objects.create(include_media=False)
        backup_service.build(source)
        source.refresh_from_db()

        job = RestoreJob.objects.create(source_backup=source,
                                        source_key=source.storage_key)
        for var in ('ECS_CLUSTER', 'ECS_SERVICE'):
            monkeypatch.delenv(var, raising=False)
        call_command('restore_backup', job=job.pk)

        safety = BackupJob.objects.filter(stage='pre-restore safety copy').first()
        assert safety is not None
        assert safety.size_bytes > 0, 'shows "—" in the list'
        # The thing that matters: it can be checked and restored in turn.
        report = restore_service.preflight(backup=safety)
        assert report['ok'], report['blocking']
        assert report['manifest_source'] == 'backup record'


@pytest.mark.django_db
class TestAStubSummaryIsNotMistakenForAManifest:

    def test_it_falls_through_to_the_sidecar(self, lock_dir):
        """A row can carry a note rather than a manifest. Taking that as the
        manifest yields 'unrecognised dump format None' instead of the answer
        the sidecar beside the archive would have given."""
        from ai_tutor.apps.dashboard import restore as restore_service

        root = Path(lock_dir) / 'backups'
        root.mkdir(parents=True, exist_ok=True)
        archive = root / 'stubbed.tar.gz'
        archive.write_bytes(b'not read by preflight')
        archive.with_name(archive.name + '.manifest.json').write_text(
            json.dumps(_manifest()))

        backup = BackupJob.objects.create(
            status=BackupJob.Status.DONE, storage_key=str(archive),
            summary={'scope': 'platform', 'note': 'where this came from'})

        report = restore_service.preflight(backup=backup)
        assert report['manifest_source'] == 'sidecar manifest'
        assert report['ok'], report['blocking']
