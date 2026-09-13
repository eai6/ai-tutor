"""Ticking a grade must not lose the platform-wide materials.

Reported from production: the upload form showed "25 platform-wide materials
will be available to this course automatically — from Mathematics S3", and
selecting Secondary 3 made them disappear.

The cause is two spellings of one grade. A platform-wide course created from
a syllabus carries whatever label the parser read out of the document
('Secondary 3'); the tick-boxes post the configured code ('S3'). With no
grade ticked the match is unconstrained and everything of that subject
matches — so the materials show. Tick the grade and the intersection of
{'S3'} and {'Secondary 3'} is empty, and they vanish. Choosing the right
grade made things strictly worse, which is the opposite of what a filter
should do.
"""

import pytest

from ai_tutor.apps.accounts.models import Institution, PlatformConfig
from ai_tutor.apps.curriculum.knowledge_base import CurriculumKnowledgeBase
from ai_tutor.apps.curriculum.models import Course
from ai_tutor.apps.dashboard.models import TeachingMaterialUpload
from ai_tutor.apps.dashboard.views import _inherited_materials_summary


@pytest.fixture
def school(db):
    return Institution.objects.create(name='Belonie', slug='belonie')


@pytest.fixture
def platform_maths(db):
    """The platform-wide course, holding its grade as the parser wrote it."""
    pc = Course.objects.create(
        title='Mathematics S3', institution=None,
        subject_code='mathematics', grade_level='Secondary 3')
    TeachingMaterialUpload.objects.create(
        course=pc, institution=None, title='Maths Textbook',
        original_filename='t.pdf', file_path='/tmp/t.pdf')
    return pc


def _school_course(school, grade_level):
    return Course.objects.create(
        title='Mathematics S3', institution=school,
        subject_code='mathematics', grade_level=grade_level)


def test_the_vocabulary_maps_a_label_to_its_code(db):
    assert PlatformConfig.normalize_grades(['Secondary 3']) == {'S3'}
    assert PlatformConfig.normalize_grades(['S3']) == {'S3'}
    assert PlatformConfig.normalize_grades(['  secondary 3 ']) == {'S3'}


def test_an_unknown_grade_is_kept_not_dropped(db):
    """Preserved, so it still has to match its own spelling on the other side
    — the pre-existing behaviour for a grade the platform does not define."""
    assert PlatformConfig.normalize_grades(['Adult']) == {'Adult'}


def test_no_grade_ticked_matches(school, platform_maths):
    """The state the report started from: unconstrained, so it matched."""
    summary = _inherited_materials_summary(_school_course(school, ''))
    assert summary['status'] == 'matched'
    assert summary['material_count'] == 1


def test_ticking_the_grade_keeps_the_materials(school, platform_maths):
    """The bug. 'S3' on this side, 'Secondary 3' on the platform course."""
    summary = _inherited_materials_summary(_school_course(school, 'S3'))
    assert summary['status'] == 'matched', (
        'ticking the correct grade lost the materials')
    assert summary['material_count'] == 1


def test_it_holds_the_other_way_round(school):
    """The school course carries the label and the platform course the code —
    the same clash mirrored, which happens when the syllabus is uploaded the
    other way round."""
    Course.objects.create(
        title='Platform Maths', institution=None,
        subject_code='mathematics', grade_level='S3')
    TeachingMaterialUpload.objects.create(
        course=Course.objects.get(title='Platform Maths'), institution=None,
        title='Book', original_filename='t.pdf', file_path='/tmp/t.pdf')

    summary = _inherited_materials_summary(
        _school_course(school, 'Secondary 3'))
    assert summary['status'] == 'matched'


def test_a_genuinely_different_grade_still_does_not_match(school, platform_maths):
    """Normalising must not make everything match — S5 is not S3."""
    summary = _inherited_materials_summary(_school_course(school, 'S5'))
    assert summary['status'] == 'grade_mismatch'


def test_the_engine_agrees_with_the_page(school, platform_maths):
    """The badge and retrieval must not disagree about what a course sees."""
    course = _school_course(school, 'S3')
    ids = CurriculumKnowledgeBase._global_upload_ids_matching_course(course)
    assert len(ids) == 1


# ---------------------------------------------------------------------------
# Ranges. The third spelling, and the one that actually bit production:
# `seed_seychelles` wrote 'S1-S3' and 'S1-S5', and grade_levels splits on
# commas — so a range read back as ONE opaque grade called "S1-S5" that
# matched nothing at all.
# ---------------------------------------------------------------------------

def test_a_range_expands_over_the_configured_order(db):
    assert PlatformConfig.normalize_grades(['S1-S3']) == {'S1', 'S2', 'S3'}
    assert PlatformConfig.normalize_grades(['S1-S5']) == {'S1', 'S2', 'S3', 'S4', 'S5'}


def test_a_backwards_range_is_read_the_same_way(db):
    assert PlatformConfig.normalize_grades(['S3-S1']) == {'S1', 'S2', 'S3'}


def test_a_range_written_with_labels_expands_too(db):
    assert PlatformConfig.normalize_grades(
        ['Secondary 1-Secondary 3']) == {'S1', 'S2', 'S3'}


def test_a_range_ending_outside_the_vocabulary_is_left_alone(db):
    """No guessing past what the platform defines — S9 is not a grade here."""
    assert PlatformConfig.normalize_grades(['S1-S9']) == {'S1-S9'}


def test_a_ranged_platform_course_shares_with_one_grade(school):
    """The production shape: the platform course spans S1-S5, the school
    course is S3, and ticking S3 must not empty the shelf."""
    pc = Course.objects.create(
        title='Mathematics', institution=None,
        subject_code='mathematics', grade_level='S1-S5')
    TeachingMaterialUpload.objects.create(
        course=pc, institution=None, title='Maths Textbook',
        original_filename='t.pdf', file_path='/tmp/t.pdf')

    summary = _inherited_materials_summary(_school_course(school, 'S3'))
    assert summary['status'] == 'matched'
    assert summary['material_count'] == 1


def test_a_ranged_course_still_excludes_a_grade_outside_it(school):
    pc = Course.objects.create(
        title='Mathematics', institution=None,
        subject_code='mathematics', grade_level='S1-S2')
    TeachingMaterialUpload.objects.create(
        course=pc, institution=None, title='Book',
        original_filename='t.pdf', file_path='/tmp/t.pdf')

    summary = _inherited_materials_summary(_school_course(school, 'S5'))
    assert summary['status'] == 'grade_mismatch'


def test_the_mismatch_message_names_the_grades_it_found(school):
    """"They cover other grades" is not a diagnosis. The stored value is the
    only thing that identifies an unexpected spelling, so it has to be shown."""
    Course.objects.create(
        title='Mathematics Lower', institution=None,
        subject_code='mathematics', grade_level='S1-S2')

    summary = _inherited_materials_summary(_school_course(school, 'S5'))
    assert summary['status'] == 'grade_mismatch'
    assert [(c.title, c.grade_level) for c in summary['subject_courses']] == [
        ('Mathematics Lower', 'S1-S2')]
