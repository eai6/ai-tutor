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
