"""The subject and grade a teacher picks on the upload form reach the Course.

Step 5 of memory/subject_grade_unification_plan.md — "the upload form is the
only place either is set" is only true if what it collects actually arrives.

There are TWO live routes from an upload to a Course, and they were not
equivalent:

* ``pipeline.complete_curriculum_upload`` — used by the re-upload / replace
  path (dashboard.views.course_reupload). Fixed in 63c3483.
* ``curriculum_parser.complete_curriculum_upload`` — used by
  ``dashboard.views.curriculum_approve``, the button a teacher presses after
  reviewing a parse. It hands off to the archive's
  ``create_curriculum_from_structure``, which knows nothing about subject_code
  and never set it.

The second is the primary route, so the reported bug — "I indicated subject
and grade on the upload page, that should have been it" — survived its own
fix on the path most uploads take.
"""

from django.test import TestCase

from ai_tutor.apps.accounts.models import Institution
from ai_tutor.apps.curriculum.models import Course
from ai_tutor.apps.dashboard.models import CurriculumUpload


def _upload(institution, **overrides):
    kwargs = dict(
        institution=institution,
        subject_name='Mathematics',
        subject_code='mathematics',
        grade_level='S3',
        original_filename='syllabus.pdf',
        file_path='/tmp/syllabus.pdf',
        status='review',
        parsed_data={
            'subject': 'Mathematics',
            'grade_levels': ['S3'],
            'description': '',
            'units': [{
                'title': 'Unit 1',
                'grade_level': 'S3',
                'lessons': [{'title': 'Lesson 1', 'objective': 'o'}],
            }],
        },
    )
    kwargs.update(overrides)
    return CurriculumUpload.objects.create(**kwargs)


class ApproveRouteCarriesTheSubjectTest(TestCase):
    def setUp(self):
        self.institution = Institution.objects.create(name='T', slug='t')

    def _approve(self, upload):
        from ai_tutor.apps.curriculum.curriculum_parser import (
            complete_curriculum_upload,
        )
        return complete_curriculum_upload(upload.id)

    def test_the_subject_picked_at_upload_lands_on_the_course(self):
        upload = _upload(self.institution)
        self._approve(upload)

        course = Course.objects.get(institution=self.institution)
        self.assertEqual(course.subject_code, 'mathematics')
        # …and therefore the derived fields, which is the whole point: without
        # the code, is_math falls back to scanning the title.
        self.assertEqual(course.subject_type, 'math')
        self.assertTrue(course.is_math)

    def test_a_maths_course_whose_title_says_nothing_is_still_maths(self):
        """The failure the title heuristic hides. With subject_code dropped,
        this course is only is_math if its computed title happens to contain a
        keyword — so a subject whose name does not is silently not maths."""
        upload = _upload(
            self.institution,
            subject_name='Further Pure',
            parsed_data={
                'subject': 'Further Pure',
                'grade_levels': ['S3'],
                'description': '',
                'units': [{
                    'title': 'Unit 1',
                    'grade_level': 'S3',
                    'lessons': [{'title': 'Lesson 1', 'objective': 'o'}],
                }],
            },
        )
        self._approve(upload)

        course = Course.objects.get(institution=self.institution)
        self.assertNotIn('math', (course.title or '').lower())
        self.assertEqual(course.subject_code, 'mathematics')
        self.assertTrue(course.is_math)

    def test_an_upload_with_no_subject_leaves_the_course_unclassified(self):
        """No guessing. A blank subject stays blank rather than being inferred
        from the title — CLAUDE.md rules that heuristic out, and the remedy is
        the upload form requiring a subject."""
        upload = _upload(self.institution, subject_code='')
        self._approve(upload)

        course = Course.objects.get(institution=self.institution)
        self.assertEqual(course.subject_code, '')


class TheUploadFormRequiresASubjectTest(TestCase):
    """The dropdown is the only thing that classifies the course an upload
    creates, so the server has to require it — not just the form's `required`
    attribute, which is client-side and absent from any programmatic POST.

    Before this, the free-text "Display name" satisfied the "please select a
    subject" check on its own, so an upload could create a course with no
    subject_code at all.
    """

    def setUp(self):
        from django.contrib.auth.models import User
        from ai_tutor.apps.accounts.models import Membership

        self.institution = Institution.objects.create(name='T', slug='t')
        self.teacher = User.objects.create_user(
            'teach', 't@example.com', 'pw', is_staff=True)
        Membership.objects.create(
            user=self.teacher, institution=self.institution,
            role=Membership.Role.STAFF)

    def _post(self, **extra):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from django.urls import reverse

        self.client.force_login(self.teacher)
        data = {
            'subject_name': 'Further Pure',
            'grade_level': 'S3',
            'locale': 'en-us',
            'curriculum_file': SimpleUploadedFile(
                'syllabus.txt', b'x', content_type='text/plain'),
        }
        data.update(extra)
        return self.client.post(
            reverse('dashboard:curriculum_upload'), data, follow=True)

    def test_a_display_name_alone_is_not_a_subject(self):
        response = self._post()   # no subject_code
        self.assertContains(response, 'Please select a subject')
        self.assertEqual(CurriculumUpload.objects.count(), 0)

    def test_an_unknown_subject_code_is_refused(self):
        response = self._post(subject_code='astrology')
        self.assertContains(response, 'Invalid subject')
        self.assertEqual(CurriculumUpload.objects.count(), 0)
