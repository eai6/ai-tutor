"""The inherited-materials filter must never be a filter that returns nothing.

`_build_global_filter` preferred an exact `upload_id IN (...)` match over the
legacy subject-string filter, and returned it early. But chunks indexed before
`upload_id` was threaded through `index_teaching_material` carry NULL, so that
filter matched zero rows — and because it returned early, the subject fallback
never ran.

The effect was inverted: the BETTER the subject+grade match got, the LESS the
tutor retrieved. A course matching platform materials got an empty filter,
while a course matching none fell through and retrieved something. Every
matching fix made this path more likely to fire.
"""

import pytest

from ai_tutor.apps.curriculum.knowledge_base import CurriculumKnowledgeBase
from ai_tutor.apps.curriculum.models import Course, CurriculumChunk
from ai_tutor.apps.accounts.models import Institution
from ai_tutor.apps.dashboard.models import TeachingMaterialUpload


@pytest.fixture
def school(db):
    return Institution.objects.create(name='Belonie', slug='belonie')


@pytest.fixture
def setup(db, school):
    platform = Course.objects.create(
        title='Platform Maths', institution=None,
        subject_code='mathematics', grade_level='S3')
    material = TeachingMaterialUpload.objects.create(
        course=platform, institution=None, title='Maths Textbook',
        original_filename='t.pdf', file_path='/tmp/t.pdf')
    course = Course.objects.create(
        title='Mathematics S3', institution=school,
        subject_code='mathematics', grade_level='S3')
    return course, material


def _chunk(upload_id, content='x'):
    return CurriculumChunk.objects.create(
        content=content, content_hash=f'h{upload_id}-{content}',
        institution_id=0, subject='Mathematics', grade_level='S3',
        upload_id=upload_id, embedding=[0.0] * 8,
    )


def test_the_match_is_used_when_chunks_carry_the_upload_id(setup):
    course, material = setup
    _chunk(material.id)

    kb = CurriculumKnowledgeBase(institution_id=1)
    where = kb._build_global_filter({'subject': 'Mathematics'}, course=course)
    assert where == {'upload_id': {'$in': [material.id]}}


def test_it_falls_back_when_no_chunk_carries_the_upload_id(setup):
    """The production state: the material matches, but its chunks predate
    upload_id and carry NULL. Returning the precise filter here retrieves
    nothing at all, which is worse than the imprecise one."""
    course, _material = setup
    _chunk(None, content='orphaned')       # indexed before upload_id existed

    kb = CurriculumKnowledgeBase(institution_id=1)
    where = kb._build_global_filter({'subject': 'Mathematics'}, course=course)
    assert where == {'subject': 'Mathematics'}, (
        'should fall through to the subject filter, not hand back an empty one')


def test_a_chunk_for_a_different_upload_does_not_count(setup):
    course, material = setup
    _chunk(material.id + 999)

    kb = CurriculumKnowledgeBase(institution_id=1)
    where = kb._build_global_filter({'subject': 'Mathematics'}, course=course)
    assert where == {'subject': 'Mathematics'}
