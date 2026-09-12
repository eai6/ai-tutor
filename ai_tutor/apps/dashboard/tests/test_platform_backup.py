"""The platform backup: one archive that must actually restore, reachable by
nobody but a superadmin.

The obligations here are the mirror image of test_aggregate_export.py. That
file asserts a export reveals nobody; this one asserts a backup reveals
everything — a dump missing the transcripts is not a backup, it is a file that
looks like one until the day someone needs it.

So these tests open the archive and read what is inside, rather than trusting
that a 143 MB file with the right name is the right file.
"""
from __future__ import annotations

import json
import tarfile
import threading
from pathlib import Path

import pytest
from django.contrib.auth.models import User
from django.db import IntegrityError, connections
from django.test import Client
from django.urls import reverse

from ai_tutor.apps.accounts.models import Institution, Membership
from ai_tutor.apps.curriculum.models import Course, Lesson, Unit
from ai_tutor.apps.dashboard import backup as backup_service
from ai_tutor.apps.dashboard.models import BackupJob
from ai_tutor.apps.safety.models import SafetyAuditLog
from ai_tutor.apps.tutoring.models import SessionTurn, TutorSession


@pytest.fixture
def school(db):
    return Institution.objects.create(name='Test School', slug='test-school')


@pytest.fixture
def superadmin(db):
    return User.objects.create_user('root', 'root@example.com', 'pw', is_staff=True)


@pytest.fixture
def teacher(db, school):
    user = User.objects.create_user('teach', 'teach@example.com', 'pw')
    Membership.objects.create(user=user, institution=school,
                              role=Membership.Role.STAFF)
    return user


@pytest.fixture
def student_with_transcript(db, school):
    student = User.objects.create_user('amara', 'amara@example.com', 'pw',
                                       first_name='Amara')
    Membership.objects.create(user=student, institution=school,
                              role=Membership.Role.STUDENT)
    course = Course.objects.create(title='Geography', institution=school)
    unit = Unit.objects.create(course=course, title='Maps', order_index=1)
    lesson = Lesson.objects.create(unit=unit, title='Maps', objective='Read a map',
                                   order_index=1, is_published=True)
    session = TutorSession.objects.create(student=student, lesson=lesson,
                                          institution=school)
    SessionTurn.objects.create(session=session, role='student',
                               content='I think the scale is 1:50000')
    return student


@pytest.fixture
def local_backup(db, tmp_path, settings):
    """Send archives to a temp directory instead of a bucket."""
    settings.BACKUP_ROOT = tmp_path / 'backups'
    settings.AWS_BACKUP_BUCKET = ''
    settings.MEDIA_ROOT = tmp_path / 'media'
    (tmp_path / 'media' / 'figures').mkdir(parents=True)
    (tmp_path / 'media' / 'figures' / 'volcano.png').write_bytes(b'not really a png')
    return tmp_path


@pytest.mark.django_db(transaction=True)
class TestArchiveContents:
    """What a restore would actually get."""

    def test_carries_the_database_the_media_and_a_manifest(
            self, local_backup, student_with_transcript):
        job = BackupJob.objects.create()
        backup_service.build(job)
        job.refresh_from_db()

        assert job.status == BackupJob.Status.DONE, job.error
        with tarfile.open(job.storage_key) as tar:
            names = tar.getnames()
        assert 'manifest.json' in names
        assert any(n.startswith('media/') and n.endswith('volcano.png') for n in names)
        assert any(n in ('db.dump', 'db.sqlite3') for n in names)

    def test_the_dump_still_holds_the_student_and_the_transcript(
            self, local_backup, student_with_transcript, tmp_path):
        """The failure this guards against is a backup that restores to an
        empty-looking database — the file exists, the rows do not."""
        job = BackupJob.objects.create()
        backup_service.build(job)
        job.refresh_from_db()

        out = tmp_path / 'restored'
        with tarfile.open(job.storage_key) as tar:
            member = next(m for m in tar.getmembers() if m.name == 'db.sqlite3')
            tar.extract(member, path=out, filter='data')

        import sqlite3
        con = sqlite3.connect(out / 'db.sqlite3')
        assert con.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        names = [r[0] for r in con.execute('SELECT first_name FROM auth_user')]
        assert 'Amara' in names
        text = [r[0] for r in con.execute('SELECT content FROM tutoring_sessionturn')]
        assert 'I think the scale is 1:50000' in text
        con.close()

    def test_the_manifest_counts_what_went_in(self, local_backup,
                                              student_with_transcript):
        """Counts are how a restore is checked: 2 users in, 2 users back."""
        job = BackupJob.objects.create()
        backup_service.build(job)
        job.refresh_from_db()

        with tarfile.open(job.storage_key) as tar:
            manifest = json.loads(tar.extractfile('manifest.json').read())

        assert manifest['scope'] == 'platform'
        assert manifest['database']['row_counts']['auth_user'] == User.objects.count()
        assert manifest['database']['row_counts']['tutoring_sessionturn'] == 1
        assert manifest['media']['files_archived'] == 1
        # Whoever opens this years from now needs to know how to put it back.
        assert 'media/' in manifest['restore']

    def test_restore_instructions_match_the_dump_format(self):
        """A pg_restore line above a SQLite file is worse than no line."""
        assert 'pg_restore' in backup_service._restore_instructions(
            'pg_dump-custom', 'db.dump')
        assert 'pg_restore' not in backup_service._restore_instructions(
            'sqlite-file', 'db.sqlite3')


@pytest.mark.django_db(transaction=True)
class TestFailureIsVisible:

    def test_a_failed_dump_records_why_on_the_row(self, local_backup, monkeypatch):
        """Fail-soft that only stores the exception type sends the next person
        to the logs, which may have rotated. The message is the artefact."""
        def explode(*args, **kwargs):
            raise RuntimeError('pg_dump: server version mismatch')

        monkeypatch.setattr(backup_service, '_dump_database', explode)
        job = BackupJob.objects.create()
        backup_service.build(job)
        job.refresh_from_db()

        assert job.status == BackupJob.Status.FAILED
        assert 'server version mismatch' in job.error
        assert job.finished_at is not None
        assert not job.storage_key


class TestOneAtATime:

    def test_a_second_job_cannot_start_while_one_runs(self, db):
        BackupJob.objects.create(status=BackupJob.Status.RUNNING)
        with pytest.raises(IntegrityError):
            BackupJob.objects.create()

    def test_a_finished_job_does_not_block_the_next(self, db):
        BackupJob.objects.create(status=BackupJob.Status.DONE)
        BackupJob.objects.create(status=BackupJob.Status.FAILED)
        BackupJob.objects.create()          # must not raise

    @pytest.mark.django_db(transaction=True)
    def test_two_simultaneous_requests_start_only_one(self, local_backup):
        """The race the view's own check cannot close.

        Both threads read "nothing is running" before either writes. Mocked-out
        work returns in microseconds and the two never overlap; the sleep is
        what makes the window real, the same way the judges concurrency test
        does it.
        """
        started, errors = [], []
        barrier = threading.Barrier(2)

        def attempt():
            barrier.wait()               # line both threads up on the window
            try:
                job = BackupJob.objects.create()
                started.append(job.pk)
            except IntegrityError:
                errors.append('rejected')
            finally:
                connections.close_all()

        threads = [threading.Thread(target=attempt) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert len(started) == 1, f'{len(started)} backups started concurrently'
        assert errors == ['rejected']


class TestWhoCanReachIt:
    """One file with every student's name and every transcript in it."""

    def _job(self, tmp_path):
        return BackupJob.objects.create(
            status=BackupJob.Status.DONE,
            storage_key=str(tmp_path),
            size_bytes=10,
        )

    def test_a_teacher_gets_nothing(self, client, teacher, local_backup):
        job = self._job(local_backup)
        client.force_login(teacher)
        assert client.get(reverse('dashboard:backup_status')).status_code == 404
        assert client.post(reverse('dashboard:backup_create')).status_code == 404
        assert client.get(
            reverse('dashboard:backup_download', args=[job.pk])).status_code == 404

    def test_an_anonymous_visitor_is_not_told_it_exists(self, client, local_backup):
        job = self._job(local_backup)
        for url in (reverse('dashboard:backup_status'),
                    reverse('dashboard:backup_download', args=[job.pk])):
            assert client.get(url).status_code in (302, 404)

    @pytest.mark.django_db(transaction=True)
    def test_a_superadmin_can_take_and_fetch_one(self, client, superadmin,
                                                 local_backup, student_with_transcript):
        job = BackupJob.objects.create(created_by=superadmin)
        backup_service.build(job)
        job.refresh_from_db()
        assert job.status == BackupJob.Status.DONE, job.error

        client.force_login(superadmin)
        response = client.get(reverse('dashboard:backup_download', args=[job.pk]))
        assert response.status_code == 200
        body = b''.join(response.streaming_content)
        assert len(body) == job.size_bytes

    def test_the_download_refuses_a_path_outside_the_backup_directory(
            self, client, superadmin, local_backup):
        """storage_key is written by build(), never by a request — but this is
        where a database value becomes a filesystem read, so it is where the
        check belongs."""
        outside = Path(local_backup) / 'secrets.env'
        outside.write_text('SECRET_KEY=hunter2')
        job = BackupJob.objects.create(status=BackupJob.Status.DONE,
                                       storage_key=str(outside), size_bytes=1)
        client.force_login(superadmin)
        assert client.get(
            reverse('dashboard:backup_download', args=[job.pk])).status_code == 404


@pytest.mark.django_db(transaction=True)
class TestItIsOnTheRecord:

    def test_taking_and_downloading_are_both_logged(self, client, superadmin,
                                                    local_backup, student_with_transcript):
        """Custody of student data means knowing who took a copy, and when."""
        client.force_login(superadmin)
        client.post(reverse('dashboard:backup_create'))

        job = BackupJob.objects.latest('id')
        # The thread is real; wait for it rather than asserting into a race.
        for _ in range(200):
            job.refresh_from_db()
            if job.is_finished:
                break
            import time
            time.sleep(0.05)
        assert job.status == BackupJob.Status.DONE, job.error

        client.get(reverse('dashboard:backup_download', args=[job.pk]))

        actions = list(
            SafetyAuditLog.objects
            .filter(event_type=SafetyAuditLog.EventType.DATA_EXPORT)
            .values_list('details__action', flat=True)
        )
        assert 'create' in actions and 'download' in actions
        logged = SafetyAuditLog.objects.filter(
            event_type=SafetyAuditLog.EventType.DATA_EXPORT).first()
        assert logged.user_id == superadmin.id
