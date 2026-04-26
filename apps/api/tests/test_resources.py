"""Tests for resource list / detail endpoints + institution scoping."""

from django.contrib.auth.models import User
from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.models import Institution, Membership
from apps.curriculum.models import Course, Unit, Lesson, LessonStep
from apps.tutoring.models import StudentLessonProgress


class ResourceEndpointsTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.alice_inst = Institution.objects.create(name='Alice School', slug='alice-school')
        cls.bob_inst = Institution.objects.create(name='Bob School', slug='bob-school')
        cls.alice = User.objects.create_user(username='alice', password='pw-123456')
        Membership.objects.create(user=cls.alice, institution=cls.alice_inst, role='student')
        cls.bob = User.objects.create_user(username='bob', password='pw-123456')
        Membership.objects.create(user=cls.bob, institution=cls.bob_inst, role='student')

        # Alice's course
        cls.course_alice = Course.objects.create(
            institution=cls.alice_inst, title='Geography S3', grade_level='S3', is_published=True,
        )
        unit_a = Unit.objects.create(course=cls.course_alice, title='U', order_index=0)
        cls.lesson_alice = Lesson.objects.create(
            unit=unit_a, title='Development indicators', objective='HDI / GNP', order_index=0, is_published=True,
        )

        # Bob's course (scoped to bob_inst — alice should not see it)
        cls.course_bob = Course.objects.create(
            institution=cls.bob_inst, title='Maths S3', grade_level='S3', is_published=True,
        )

        # Platform-wide course (institution=None, both should see)
        cls.course_global = Course.objects.create(
            institution=None, title='Platform Demo', grade_level='S3', is_published=True,
        )

    def _login(self, user):
        client = APIClient()
        resp = client.post(
            '/api/v1/auth/login/',
            {'username': user.username, 'password': 'pw-123456'},
            format='json',
        )
        token = resp.json()['access']
        client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')
        return client

    def test_courses_scoped_to_institution_plus_global(self):
        client = self._login(self.alice)
        resp = client.get('/api/v1/courses/')
        self.assertEqual(resp.status_code, 200)
        titles = {c['title'] for c in resp.json()['results']}
        self.assertIn('Geography S3', titles)
        self.assertIn('Platform Demo', titles)
        self.assertNotIn('Maths S3', titles)

    def test_lesson_detail_cross_school_404(self):
        client = self._login(self.bob)
        resp = client.get(f'/api/v1/lessons/{self.lesson_alice.id}/')
        self.assertEqual(resp.status_code, 404)

    def test_lesson_detail_visible_to_owner(self):
        client = self._login(self.alice)
        resp = client.get(f'/api/v1/lessons/{self.lesson_alice.id}/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['title'], 'Development indicators')

    def test_progress_scoped_to_user(self):
        StudentLessonProgress.objects.create(
            student=self.alice, lesson=self.lesson_alice,
            institution=self.alice_inst, mastery_level='mastered',
            best_score=0.85, attempts_count=2,
        )
        client = self._login(self.alice)
        resp = client.get('/api/v1/progress/')
        self.assertEqual(resp.status_code, 200)
        rows = resp.json()['results']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['lesson_id'], self.lesson_alice.id)

        bob_client = self._login(self.bob)
        bob_resp = bob_client.get('/api/v1/progress/')
        self.assertEqual(len(bob_resp.json()['results']), 0)
