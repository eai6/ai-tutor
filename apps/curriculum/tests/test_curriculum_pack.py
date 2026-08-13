"""Curriculum packs, and the wall between them and desktop content packs.

The load-bearing tests here are the refusals. A curriculum pack exists so a
ministry can seed a new deployment with real teaching content; a desktop pack
exists so a classroom laptop can bind a student's local login to the server
identity their work syncs under, and it carries a roster of real children to do
that.

Handing the second to the first is a transfer of identifiable minors' data to
another organisation. Nothing about the two files looks different to whoever is
copying them, so the importers refuse each other rather than trusting the
person holding the file.

Plan: memory/self_hosting_manual_plan.md
"""
from __future__ import annotations

import json
import tarfile
from pathlib import Path

import pytest

from apps.accounts.models import Institution, Membership
from apps.curriculum import curriculum_pack as CP
from apps.curriculum.models import Course, Lesson, Unit
from apps.desktop.packs import PackError


@pytest.fixture
def school(db):
    return Institution.objects.create(name='Source School', slug='source-school')


@pytest.fixture
def content(school):
    course = Course.objects.create(title='Geography', institution=school,
                                   subject_type='geography')
    unit = Unit.objects.create(course=course, title='Maps', order_index=1)
    Lesson.objects.create(unit=unit, title='Reading Maps', objective='Read a map',
                          order_index=1, is_published=True)
    return course


@pytest.fixture
def student_in_roster(school):
    from django.contrib.auth.models import User
    user = User.objects.create_user(username='pupil.real',
                                    first_name='Aisha', last_name='Rahman')
    Membership.objects.create(user=user, institution=school, role='student',
                              is_active=True)
    return user


@pytest.mark.django_db
class TestTheCurriculumPackCarriesNoUserData:
    def test_the_archive_has_no_roster_or_institution(
            self, tmp_path, school, content, student_in_roster):
        manifest = CP.build_curriculum_pack(school.pk, tmp_path,
                                            include_media=False)

        with tarfile.open(manifest['archive'], 'r:gz') as tar:
            names = set(tar.getnames())

        assert 'roster.json' not in names
        assert 'institution.json' not in names
        assert 'curriculum.json' in names

    def test_the_students_name_appears_nowhere_in_the_bytes(
            self, tmp_path, school, content, student_in_roster):
        """The decisive check. A member list can be right while the content of
        a member is wrong, so this reads the whole archive."""
        manifest = CP.build_curriculum_pack(school.pk, tmp_path,
                                            include_media=False)

        with tarfile.open(manifest['archive'], 'r:gz') as tar:
            blob = b''.join(
                tar.extractfile(m).read()
                for m in tar.getmembers() if m.isfile()
            )

        assert b'Aisha' not in blob
        assert b'Rahman' not in blob
        assert b'pupil.real' not in blob

    def test_it_declares_its_kind(self, tmp_path, school, content):
        manifest = CP.build_curriculum_pack(school.pk, tmp_path,
                                            include_media=False)
        assert manifest['pack_kind'] == 'curriculum'

    def test_it_still_carries_the_teaching_content(self, tmp_path, school,
                                                   content):
        manifest = CP.build_curriculum_pack(school.pk, tmp_path,
                                            include_media=False)
        counts = manifest['counts']
        assert counts['courses'] == 1
        assert counts['units'] == 1
        assert counts['lessons'] == 1


@pytest.mark.django_db
class TestTheImportersRefuseEachOther:
    """The wall. Both directions, because both directions are a real mistake."""

    def _desktop_pack(self, tmp_path, school):
        from apps.desktop.packs import build_pack
        return Path(build_pack(school.pk, tmp_path, include_media=False)['archive'])

    def test_a_desktop_pack_cannot_seed_a_deployment(
            self, tmp_path, school, content, student_in_roster):
        """THE test. A desktop pack contains a roster of real children; loading
        it onto another organisation's server hands them that list."""
        archive = self._desktop_pack(tmp_path, school)

        # Sanity: the desktop pack really does carry the roster, or this test
        # would pass for the wrong reason.
        with tarfile.open(archive, 'r:gz') as tar:
            roster = json.load(tar.extractfile('roster.json'))
        assert any(r['username'] == 'pupil.real' for r in roster)

        with pytest.raises(PackError) as exc:
            CP.import_curriculum_pack(archive)
        assert 'not a curriculum pack' in str(exc.value).lower()

    def test_a_curriculum_pack_cannot_provision_a_device(
            self, tmp_path, school, content):
        """The other direction. A device that accepted one would set up fine
        and then strand every student at an empty 'pick your name' screen — in
        a classroom, not here."""
        from apps.desktop.packs import import_pack

        archive = Path(CP.build_curriculum_pack(
            school.pk, tmp_path, include_media=False)['archive'])

        with pytest.raises(PackError) as exc:
            import_pack(archive)
        assert 'roster' in str(exc.value).lower()

    def test_a_hand_edited_manifest_is_still_caught(self, tmp_path, school,
                                                    content, student_in_roster):
        """The declared kind is what a well-formed pack says about itself. The
        member list is what it actually contains. Only the second catches a
        file whose manifest was edited to get past the first."""
        archive = self._desktop_pack(tmp_path, school)
        forged = tmp_path / 'forged.tar.gz'

        with tarfile.open(archive, 'r:gz') as src:
            src.extractall(tmp_path / 'x', filter='data')
        manifest_path = tmp_path / 'x' / 'manifest.json'
        m = json.loads(manifest_path.read_text())
        m['pack_kind'] = 'curriculum'          # the forgery
        manifest_path.write_text(json.dumps(m))
        with tarfile.open(forged, 'w:gz') as tar:
            for item in sorted((tmp_path / 'x').iterdir()):
                tar.add(item, arcname=item.name)

        with pytest.raises(PackError) as exc:
            CP.import_curriculum_pack(forged)
        assert 'roster.json' in str(exc.value)

    def test_a_legacy_pack_with_no_kind_still_provisions_a_device(
            self, tmp_path, school, content, student_in_roster):
        """Packs built before pack_kind existed have no kind and ARE desktop
        packs. Treating a missing value as 'desktop' keeps them importable
        rather than bricking every device in the field on upgrade."""
        from apps.desktop.packs import import_pack

        archive = self._desktop_pack(tmp_path, school)
        legacy = tmp_path / 'legacy.tar.gz'
        with tarfile.open(archive, 'r:gz') as src:
            src.extractall(tmp_path / 'y', filter='data')
        manifest_path = tmp_path / 'y' / 'manifest.json'
        m = json.loads(manifest_path.read_text())
        del m['pack_kind']
        manifest_path.write_text(json.dumps(m))
        with tarfile.open(legacy, 'w:gz') as tar:
            for item in sorted((tmp_path / 'y').iterdir()):
                tar.add(item, arcname=item.name)

        # Not asserting a successful import — that needs device state. Only
        # that it is not rejected for being the wrong KIND.
        try:
            import_pack(legacy)
        except PackError as exc:
            assert 'pack' not in str(exc).lower() or 'roster' not in str(exc).lower()


@pytest.mark.django_db
class TestImportBehaviour:
    def test_it_seeds_an_empty_deployment(self, tmp_path, school, content):
        archive = Path(CP.build_curriculum_pack(
            school.pk, tmp_path, include_media=False)['archive'])

        Course.objects.all().delete()
        Institution.objects.all().delete()
        assert not Course.objects.exists()

        CP.import_curriculum_pack(archive)

        assert Course.objects.count() == 1
        assert Lesson.objects.count() == 1

    def test_imported_content_is_platform_wide(self, tmp_path, school, content):
        """The source institution's id means nothing here. Following it would
        leave every course owned by a school that does not exist."""
        archive = Path(CP.build_curriculum_pack(
            school.pk, tmp_path, include_media=False)['archive'])
        Course.objects.all().delete()
        Institution.objects.all().delete()

        CP.import_curriculum_pack(archive)

        assert Course.objects.get().institution is None

    def test_it_refuses_to_overwrite_an_existing_deployment(
            self, tmp_path, school, content):
        """Loading over existing courses replaces them by primary key — on a
        live server that is one school's content silently becoming another's."""
        archive = Path(CP.build_curriculum_pack(
            school.pk, tmp_path, include_media=False)['archive'])

        with pytest.raises(PackError) as exc:
            CP.import_curriculum_pack(archive)
        assert 'already has' in str(exc.value)

    def test_force_overrides_that(self, tmp_path, school, content):
        archive = Path(CP.build_curriculum_pack(
            school.pk, tmp_path, include_media=False)['archive'])
        CP.import_curriculum_pack(archive, force=True)
        assert Course.objects.exists()

    def test_a_corrupt_pack_is_refused(self, tmp_path, school, content):
        archive = Path(CP.build_curriculum_pack(
            school.pk, tmp_path, include_media=False)['archive'])
        Course.objects.all().delete()

        # Rewrite one member, leaving the manifest checksum stale.
        with tarfile.open(archive, 'r:gz') as src:
            src.extractall(tmp_path / 'z', filter='data')
        (tmp_path / 'z' / 'tickets.json').write_text('[]')
        broken = tmp_path / 'broken.tar.gz'
        with tarfile.open(broken, 'w:gz') as tar:
            for item in sorted((tmp_path / 'z').iterdir()):
                tar.add(item, arcname=item.name)

        with pytest.raises(PackError) as exc:
            CP.import_curriculum_pack(broken)
        assert 'corrupt' in str(exc.value).lower()
