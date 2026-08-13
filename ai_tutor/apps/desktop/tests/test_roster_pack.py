"""The roster part of the content pack: build, import, and re-import.

The roster is what makes sync possible at all — it carries the SERVER's user id
onto a device that has never been online, so work done offline can be attributed
to a student the cloud already knows. Without it, every device invents its own
integer ids and nothing that syncs up can be matched to a person.

The property most worth pinning is the re-import one. A teacher importing a
newer pack must not unlink students who have already claimed their accounts;
that would strand a term's work mid-lesson, and it is the kind of break that
only shows up in a classroom.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from ai_tutor.apps.accounts.models import Institution, Membership
from ai_tutor.apps.desktop.models import RosterEntry
from ai_tutor.apps.desktop.packs import ROSTER_NAME, _import_roster, build_roster


@pytest.fixture
def institution(db):
    return Institution.objects.create(name='Test School', slug='test-school')


def _student(institution, username, first='', last=''):
    user = User.objects.create_user(username=username, first_name=first, last_name=last)
    Membership.objects.create(user=user, institution=institution, role='student', is_active=True)
    return user


@pytest.mark.django_db
class TestBuildRoster:
    def test_includes_active_students_with_their_server_id(self, institution):
        alice = _student(institution, 'alice', 'Alice', 'Adams')
        roster = build_roster(institution.id)
        assert len(roster) == 1
        assert roster[0]['server_user_id'] == alice.id
        assert roster[0]['display_name'] == 'Alice Adams'

    def test_falls_back_to_username_when_no_real_name(self, institution):
        _student(institution, 'bob')
        assert build_roster(institution.id)[0]['display_name'] == 'bob'

    def test_excludes_inactive_memberships_and_non_students(self, institution):
        gone = _student(institution, 'left')
        Membership.objects.filter(user=gone).update(is_active=False)
        teacher = User.objects.create_user(username='teacher')
        Membership.objects.create(user=teacher, institution=institution,
                                  role='teacher', is_active=True)
        assert build_roster(institution.id) == []

    def test_excludes_other_institutions(self, institution):
        other = Institution.objects.create(name='Other', slug='other')
        _student(other, 'elsewhere')
        assert build_roster(institution.id) == []

    def test_carries_no_credentials(self, institution):
        """A pack travels on a USB stick between schools."""
        _student(institution, 'carol', 'Carol', 'Chen')
        entry = build_roster(institution.id)[0]
        assert set(entry) == {'server_user_id', 'username', 'display_name', 'grade_level'}
        blob = json.dumps(entry)
        assert 'password' not in blob and 'email' not in blob


@pytest.mark.django_db
class TestImportRoster:
    def _write(self, tmpdir, entries):
        path = Path(tmpdir) / ROSTER_NAME
        path.write_text(json.dumps(entries))
        return path

    def test_creates_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, [
                {'server_user_id': 501, 'username': 'alice',
                 'display_name': 'Alice Adams', 'grade_level': 'S3'},
            ])
            assert _import_roster(path, pack_version=1) == 1
        e = RosterEntry.objects.get(server_user_id=501)
        assert e.display_name == 'Alice Adams'
        assert e.local_user is None      # nobody has claimed it yet

    def test_a_missing_roster_is_not_an_error(self):
        """Packs built before roster support must still import."""
        with tempfile.TemporaryDirectory() as tmp:
            assert _import_roster(Path(tmp) / ROSTER_NAME, pack_version=1) == 0

    def test_reimport_updates_in_place_and_keeps_the_claim(self):
        """The property that matters: a newer pack must not unlink students."""
        with tempfile.TemporaryDirectory() as tmp:
            self._write(tmp, [{'server_user_id': 501, 'username': 'alice',
                               'display_name': 'Alice Adams', 'grade_level': 'S3'}])
            _import_roster(Path(tmp) / ROSTER_NAME, pack_version=1)

            local = User.objects.create_user(username='alice-local')
            entry = RosterEntry.objects.get(server_user_id=501)
            entry.local_user = local
            entry.claimed_at = timezone.now()
            entry.save()

            # A later pack: her surname was corrected on the server.
            self._write(tmp, [{'server_user_id': 501, 'username': 'alice',
                               'display_name': 'Alice Anderson', 'grade_level': 'S4'}])
            _import_roster(Path(tmp) / ROSTER_NAME, pack_version=2)

        entry.refresh_from_db()
        assert entry.display_name == 'Alice Anderson'   # updated
        assert entry.grade_level == 'S4'
        assert entry.pack_version == 2
        assert entry.local_user_id == local.id          # claim survived
        assert RosterEntry.objects.count() == 1         # updated, not duplicated

    def test_a_student_dropped_from_a_later_pack_is_kept(self):
        """Their work is still on this device and still needs an identity."""
        with tempfile.TemporaryDirectory() as tmp:
            self._write(tmp, [
                {'server_user_id': 501, 'username': 'alice', 'display_name': 'Alice'},
                {'server_user_id': 502, 'username': 'bob', 'display_name': 'Bob'},
            ])
            _import_roster(Path(tmp) / ROSTER_NAME, pack_version=1)
            self._write(tmp, [
                {'server_user_id': 501, 'username': 'alice', 'display_name': 'Alice'},
            ])
            _import_roster(Path(tmp) / ROSTER_NAME, pack_version=2)
        assert RosterEntry.objects.filter(server_user_id=502).exists()

    def test_server_user_id_is_unique(self):
        RosterEntry.objects.create(server_user_id=777, username='x', display_name='X')
        with pytest.raises(Exception):
            RosterEntry.objects.create(server_user_id=777, username='y', display_name='Y')
