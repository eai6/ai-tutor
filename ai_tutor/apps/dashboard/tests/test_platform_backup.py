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
class TestDatabaseOnly:
    """Media is nearly all the bytes, so a database-only archive is the one
    someone takes often. The risk is that it looks identical to a full one."""

    def test_it_leaves_the_media_out(self, local_backup, student_with_transcript):
        job = BackupJob.objects.create(include_media=False)
        backup_service.build(job)
        job.refresh_from_db()

        assert job.status == BackupJob.Status.DONE, job.error
        with tarfile.open(job.storage_key) as tar:
            names = tar.getnames()
        assert not any(n.startswith('media/') for n in names)
        assert 'db.sqlite3' in names and 'manifest.json' in names

    def test_the_database_is_still_whole(self, local_backup,
                                         student_with_transcript, tmp_path):
        """Skipping media must not mean skipping anything else."""
        job = BackupJob.objects.create(include_media=False)
        backup_service.build(job)
        job.refresh_from_db()

        out = tmp_path / 'restored-db-only'
        with tarfile.open(job.storage_key) as tar:
            tar.extract(tar.getmember('db.sqlite3'), path=out, filter='data')
        import sqlite3
        con = sqlite3.connect(out / 'db.sqlite3')
        names = [r[0] for r in con.execute('SELECT first_name FROM auth_user')]
        assert 'Amara' in names
        con.close()

    def test_the_archive_says_so_in_its_name_and_its_manifest(
            self, local_backup, student_with_transcript):
        """The file outlives this page. Someone holding it later must be able
        to tell which kind it is without guessing."""
        job = BackupJob.objects.create(include_media=False)
        backup_service.build(job)
        job.refresh_from_db()

        assert job.storage_key.endswith('-db.tar.gz')
        with tarfile.open(job.storage_key) as tar:
            manifest = json.loads(tar.extractfile('manifest.json').read())
        assert manifest['media']['included'] is False
        assert manifest['media']['files_archived'] == 0
        assert 'NO MEDIA' in manifest['restore']

    def test_a_full_archive_is_named_and_marked_differently(
            self, local_backup, student_with_transcript):
        job = BackupJob.objects.create(include_media=True)
        backup_service.build(job)
        job.refresh_from_db()

        assert job.storage_key.endswith('-full.tar.gz')
        with tarfile.open(job.storage_key) as tar:
            manifest = json.loads(tar.extractfile('manifest.json').read())
        assert manifest['media']['included'] is True
        assert 'NO MEDIA' not in manifest['restore']

    def test_the_button_default_is_the_complete_archive(self, client, superadmin,
                                                        local_backup, monkeypatch):
        """A backup that quietly left out most of the data because a parameter
        was missing is the wrong way round to fail."""
        # Don't let a real thread outlive the test: it would be torn down
        # mid-build and die inside its own error handler.
        monkeypatch.setattr(backup_service, 'start', lambda job: None)
        client.force_login(superadmin)
        client.post(reverse('dashboard:backup_create'))      # no include_media
        assert BackupJob.objects.latest('id').include_media is True


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


class TestAStuckJobDoesNotBlockForever:
    """The one-active-backup constraint is what makes a dead job dangerous: a
    row left in RUNNING by a process that was replaced mid-backup would hold
    the door shut for good."""

    def test_an_abandoned_job_is_reaped(self, db):
        from django.utils import timezone as tz
        job = BackupJob.objects.create(status=BackupJob.Status.RUNNING)
        BackupJob.objects.filter(pk=job.pk).update(
            created_at=tz.now() - backup_service.STALE_AFTER * 2)

        assert backup_service.reap_stale() == 1
        job.refresh_from_db()
        assert job.status == BackupJob.Status.FAILED
        assert 'take another' in job.error
        BackupJob.objects.create()          # the door is open again

    def test_a_backup_still_running_is_left_alone(self, db):
        BackupJob.objects.create(status=BackupJob.Status.RUNNING)
        assert backup_service.reap_stale() == 0

    def test_the_view_clears_a_stuck_job_and_starts(self, client, superadmin,
                                                    local_backup, monkeypatch):
        from django.utils import timezone as tz
        monkeypatch.setattr(backup_service, 'start', lambda job: None)
        stuck = BackupJob.objects.create(status=BackupJob.Status.RUNNING)
        BackupJob.objects.filter(pk=stuck.pk).update(
            created_at=tz.now() - backup_service.STALE_AFTER * 2)

        client.force_login(superadmin)
        client.post(reverse('dashboard:backup_create'))

        stuck.refresh_from_db()
        assert stuck.status == BackupJob.Status.FAILED
        assert BackupJob.objects.filter(status=BackupJob.Status.PENDING).count() == 1


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


@pytest.mark.django_db(transaction=True)
class TestTheArchiveCanBeCheckedBeforeItIsTrusted:
    """What a restore needs to know before it drops the live database.

    An archive that cannot be verified is one you find out about at the worst
    possible moment. These are the fields that make "is this file good, and does
    its schema match this code?" answerable without restoring it to find out.
    """

    def _build(self):
        job = BackupJob.objects.create()
        backup_service.build(job)
        job.refresh_from_db()
        assert job.status == BackupJob.Status.DONE, job.error
        return job

    def test_the_dump_checksum_is_of_the_dump(self, local_backup,
                                              student_with_transcript, tmp_path):
        """Not of the archive, and not a stand-in for the row counts."""
        import hashlib
        job = self._build()
        recorded = job.summary['database']['dump_sha256']

        with tarfile.open(job.storage_key) as tar:
            name = job.summary['database']['dump_file']
            actual = hashlib.sha256(tar.extractfile(name).read()).hexdigest()
        assert actual == recorded

    def test_a_tampered_archive_stops_matching_its_checksum(self, local_backup,
                                                           student_with_transcript):
        """The whole point: a truncated or altered copy is detectable."""
        import hashlib
        job = self._build()
        path = Path(job.storage_key)
        assert hashlib.sha256(path.read_bytes()).hexdigest() == job.archive_sha256

        path.write_bytes(path.read_bytes()[:-2048])
        assert hashlib.sha256(path.read_bytes()).hexdigest() != job.archive_sha256

    def test_the_manifest_records_which_migrations_were_applied(
            self, local_backup, student_with_transcript):
        """The schema version. `app_version` cannot answer this — VERSION is a
        constant that has never been bumped."""
        from django.db import connection
        job = self._build()
        heads = job.summary['database']['migration_heads']

        with connection.cursor() as cur:
            cur.execute("SELECT app, MAX(name) FROM django_migrations GROUP BY app")
            expected = {app: name for app, name in cur.fetchall()}
        assert heads == expected
        assert 'dashboard' in heads

    def test_the_manifest_is_readable_without_opening_the_archive(
            self, local_backup, student_with_transcript):
        """job.summary IS the manifest. This is what lets the settings page
        preflight a 9.3 GB archive for the cost of a database read."""
        job = self._build()
        with tarfile.open(job.storage_key) as tar:
            inside = json.loads(tar.extractfile('manifest.json').read())

        for field in ('scope', 'database', 'media', 'restore'):
            assert job.summary[field] == inside[field]
        assert job.summary['scope'] == 'platform'

    def test_a_sidecar_manifest_sits_beside_the_archive(self, local_backup,
                                                       student_with_transcript):
        """For the archive whose row is gone — uploaded from a laptop, or the
        pre-restore safety copy whose row the restore itself destroys."""
        job = self._build()
        sidecar = Path(job.storage_key + '.manifest.json')
        assert sidecar.is_file()

        described = json.loads(sidecar.read_text())
        assert described['scope'] == 'platform'
        assert described['database']['dump_sha256'] == job.summary['database']['dump_sha256']
        # The sidecar can carry the archive's own hash; the copy inside the tar
        # cannot, because it is sealed before the hash exists.
        assert described['archive_sha256'] == job.archive_sha256

    def test_the_format_version_is_stamped(self, local_backup,
                                           student_with_transcript):
        """So a restore meeting an older archive says 'cannot verify' rather
        than reading fields that are not there."""
        job = self._build()
        assert job.summary['archive_format_version'] == backup_service.ARCHIVE_FORMAT_VERSION
        assert backup_service.ARCHIVE_FORMAT_VERSION >= 2

    def test_row_counts_are_labelled_as_taken_before_the_dump(
            self, local_backup, student_with_transcript):
        """They are a sanity signal, not an integrity check, and the two must
        not be presented as interchangeable."""
        job = self._build()
        assert job.summary['database']['row_counts_taken'] == 'at backup start'


@pytest.mark.django_db(transaction=True)
class TestCountingIsNotDoneOnEveryPageView:
    """Listing 10,521 media objects to render two numbers, on every settings
    page load, was the cost before this."""

    def test_the_inventory_is_cached(self, local_backup):
        from django.core.cache import cache
        cache.delete(backup_service.INVENTORY_CACHE_KEY)

        calls = []
        original = backup_service._media_inventory

        def counting():
            calls.append(1)
            return original()

        backup_service._media_inventory = counting
        try:
            backup_service.inventory()
            backup_service.inventory()
            backup_service.inventory()
            assert len(calls) == 1
        finally:
            backup_service._media_inventory = original

    def test_a_backup_always_counts_afresh(self, local_backup):
        """A 15-minute-old number is fine for the card and not for the manifest."""
        from django.core.cache import cache
        cache.set(backup_service.INVENTORY_CACHE_KEY,
                  {'database': {'stale': True}, 'media': {'stale': True}}, 900)

        counts = backup_service.inventory(fresh=True)
        assert 'stale' not in counts['database']
        assert counts['database']['engine'] in ('sqlite', 'postgresql')


@pytest.mark.django_db(transaction=True)
class TestTwoBackupsInTheSameSecond:
    """The restore flow guarantees this: it takes a safety copy moments before
    reading another archive. Sharing a filename means the safety copy can
    overwrite the very archive being restored."""

    def test_they_do_not_share_a_filename(self, local_backup):
        first = BackupJob.objects.create(include_media=False)
        backup_service.build(first)
        second = BackupJob.objects.create(include_media=False)
        backup_service.build(second)
        first.refresh_from_db()
        second.refresh_from_db()

        assert first.status == BackupJob.Status.DONE, first.error
        assert second.status == BackupJob.Status.DONE, second.error
        assert first.storage_key != second.storage_key
        assert Path(first.storage_key).is_file(), 'the first archive was overwritten'
        assert Path(second.storage_key).is_file()

    def test_each_still_says_what_it_holds_in_its_name(self, local_backup):
        job = BackupJob.objects.create(include_media=False)
        backup_service.build(job)
        job.refresh_from_db()
        # The kind stays last: someone holding the file a year from now should
        # not have to open it to find out the figures are missing.
        assert job.storage_key.endswith('-db.tar.gz')
        assert f'-j{job.pk}-' in job.storage_key

    def test_a_prefixed_copy_lands_apart_from_ordinary_backups(self, local_backup):
        """The pre-restore safety copy, which must be findable at the moment
        somebody badly needs it."""
        job = BackupJob.objects.create(include_media=False)
        backup_service.build(job, prefix=f'{backup_service.OPS_PREFIX}/pre-restore/7')
        job.refresh_from_db()
        assert job.status == BackupJob.Status.DONE, job.error
        assert Path(job.storage_key).parent.name == '7'
        assert 'pre-restore' in job.storage_key

    def test_an_ordinary_backup_is_not_nested_under_a_stray_directory(self, local_backup):
        job = BackupJob.objects.create(include_media=False)
        backup_service.build(job)
        job.refresh_from_db()
        assert Path(job.storage_key).parent == backup_service.backup_root()
